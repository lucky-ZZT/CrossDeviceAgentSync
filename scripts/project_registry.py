#!/usr/bin/env python3
"""Offline Codex project registration and conflict-safe project ID merging."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterable

import codex_compat
import content_manager
import migration_bundle


GLOBAL_STATE_FILE_NAME = ".codex-global-state.json"


def _read_state(codex_home: Path) -> tuple[Path, dict[str, Any], bytes | None]:
    state_path = codex_home / GLOBAL_STATE_FILE_NAME
    if not state_path.is_file():
        return state_path, {}, None
    raw = state_path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Codex global project state is not a JSON object")
    projects = payload.get("local-projects")
    if projects is not None and not isinstance(projects, dict):
        raise ValueError("Codex local project registry has an unsupported format")
    return state_path, payload, raw


def _normal_path(value: Path | str) -> str:
    normalized, _kind = content_manager._normalized_project_path(value)
    if normalized is None:
        raise ValueError(f"Project path is not an absolute Windows path: {value}")
    return normalized


def _path_identity(value: Path | str) -> str:
    identity = content_manager._project_path_identity(value)
    if identity is None:
        raise ValueError(f"Project path cannot be normalized: {value}")
    return identity


def inspect_project_conflicts(
    codex_home: Path,
    proposed_path: Path,
    project_name: str,
) -> dict[str, Any]:
    """Read project registry state without creating the Codex home or project path."""
    codex_home = codex_home.expanduser().resolve()
    state_path, payload, raw = _read_state(codex_home)
    projects = payload.get("local-projects") or {}
    proposed_normal = _normal_path(proposed_path)
    proposed_identity = _path_identity(proposed_normal)
    records = []
    same_path = []
    same_name = []
    for project_id, project in projects.items():
        if not isinstance(project, dict):
            continue
        roots = project.get("rootPaths")
        if not isinstance(roots, list) or not roots:
            continue
        identities = [content_manager._project_path_identity(root) for root in roots]
        record = {
            "project_id": str(project_id),
            "project_name": str(project.get("name") or project_id),
            "root_paths": [str(root) for root in roots],
            "normal_paths": [content_manager._normalized_project_path(root)[0] for root in roots],
            "has_extended_path": any(
                content_manager._normalized_extended_rollout_path(root)[1] is not None
                for root in roots
            ),
            "task_assignments": content_manager._project_assignment_count(payload, str(project_id)),
        }
        records.append(record)
        if proposed_identity in identities:
            same_path.append(record)
        if record["project_name"].casefold() == project_name.casefold():
            same_name.append(record)

    if same_path:
        conflict = "same_path_duplicate" if len(same_path) > 1 else "same_path_existing"
        recommended = "merge_registration" if len(same_path) > 1 else "reuse_existing"
    elif same_name:
        conflict = "same_name_different_path"
        recommended = "import_renamed"
    else:
        conflict = "none"
        recommended = "create_project"
    environment = "empty" if not records else ("overlap" if same_path or same_name else "no_overlap")
    return {
        "codex_home": str(codex_home),
        "global_state": str(state_path),
        "global_state_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "project_name": project_name,
        "proposed_path": proposed_normal,
        "environment": environment,
        "conflict": conflict,
        "recommended_action": recommended,
        "same_path_projects": same_path,
        "same_name_projects": same_name,
        "registered_projects": len(records),
    }


def _ensure_project_lists(payload: dict[str, Any], project_id: str) -> None:
    for field in ("project-order", "electron-saved-workspace-roots"):
        value = payload.get(field)
        if value is None:
            payload[field] = [project_id]
        elif isinstance(value, list):
            if project_id not in value:
                value.append(project_id)
        elif isinstance(value, str):
            if value != project_id:
                payload[field] = [value, project_id]
        else:
            raise ValueError(f"Codex project field has an unsupported format: {field}")


def _unknown_references(payload: dict[str, Any], project_ids: set[str]) -> dict[str, list[str]]:
    references = content_manager._project_reference_paths(payload, project_ids)
    return {
        project_id: [
            path for path in paths
            if not content_manager._known_project_reference(path, project_id)
        ]
        for project_id, paths in references.items()
        if any(
            not content_manager._known_project_reference(path, project_id)
            for path in paths
        )
    }


def register_project_offline(
    codex_home: Path,
    project_path: Path,
    project_name: str,
    action: str,
    task_ids: Iterable[str] = (),
    keeper_id: str | None = None,
    expected_state_sha256: str | None = None,
    require_codex_closed: bool = True,
) -> dict[str, Any]:
    """Create, reuse, or merge a project registration while Codex is closed."""
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before registering an imported project")
    if action not in {"create_project", "reuse_existing", "merge_registration"}:
        raise ValueError(f"Unsupported project registration action: {action}")

    codex_home = codex_home.expanduser().resolve()
    codex_compat.require_operation_supported(
        codex_home, "project_registry", "注册导入项目"
    )
    normal_path = _normal_path(project_path)
    if not Path(normal_path).is_dir():
        raise ValueError(f"Imported project directory does not exist: {normal_path}")
    state_path, payload, original = _read_state(codex_home)
    current_sha = hashlib.sha256(original).hexdigest() if original is not None else None
    if expected_state_sha256 != current_sha:
        raise ValueError("Codex project state changed after preview. Check the import again")
    projects = payload.setdefault("local-projects", {})
    if not isinstance(projects, dict):
        raise ValueError("Codex local project registry has an unsupported format")

    matching = [
        str(project_id)
        for project_id, project in projects.items()
        if isinstance(project, dict)
        and _path_identity(normal_path) in (content_manager._project_roots_identity(project) or ())
    ]
    created = False
    merged_ids: list[str] = []
    if action == "create_project":
        if matching:
            raise ValueError("A project for the selected directory already exists. Check conflicts again")
        project_id = str(uuid.uuid4())
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        projects[project_id] = {
            "id": project_id,
            "name": project_name,
            "rootPaths": [normal_path],
            "createdAt": now_ms,
            "updatedAt": now_ms,
        }
        _ensure_project_lists(payload, project_id)
        created = True
    else:
        if not matching:
            raise ValueError("The project selected during preview no longer points to this directory")
        if keeper_id is None:
            ordinary = [
                project_id for project_id in matching
                if not any(
                    content_manager._normalized_extended_rollout_path(root)[1]
                    for root in projects[project_id].get("rootPaths", [])
                )
            ]
            keeper_id = ordinary[0] if ordinary else matching[0]
        if keeper_id not in matching:
            raise ValueError("The selected keeper project no longer matches the import directory")
        project_id = keeper_id
        if action == "merge_registration" and len(matching) > 1:
            unknown = _unknown_references(payload, set(matching))
            if unknown:
                raise ValueError(f"Unknown project references block merging: {unknown}")
            merged_ids = [item for item in matching if item != keeper_id]
            group = {
                "keeper_id": keeper_id,
                "keeper_name": str(projects[keeper_id].get("name") or keeper_id),
                "remove_ids": merged_ids,
            }
            content_manager._apply_project_registry_repairs(payload, [group], [], [])
        roots = projects[project_id].get("rootPaths")
        if not isinstance(roots, list) or not roots:
            raise ValueError("The keeper project has no usable root path")
        projects[project_id]["rootPaths"] = [normal_path]
        _ensure_project_lists(payload, project_id)

    assignments = payload.setdefault("thread-project-assignments", {})
    if not isinstance(assignments, dict):
        raise ValueError("Codex task-to-project assignments have an unsupported format")
    assigned = []
    for task_id in dict.fromkeys(str(item) for item in task_ids if str(item)):
        assignments[task_id] = {"projectKind": "local", "projectId": project_id}
        assigned.append(task_id)

    codex_home.mkdir(parents=True, exist_ok=True)
    descriptor, lock_path = migration_bundle.acquire_lock(codex_home)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_root = migration_bundle.backup_root_for(codex_home) / f"{stamp}-project-registration"
    backup_root.mkdir(parents=True, exist_ok=False)
    backup_path = backup_root / "files" / GLOBAL_STATE_FILE_NAME
    created_state = original is None
    try:
        if original is not None:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_bytes(original)
        transaction = {
            "status": "in_progress",
            "operation": "offline_project_registration",
            "project_id": project_id,
            "project_path": normal_path,
            "action": action,
            "merged_project_ids": merged_ids,
            "assigned_task_ids": assigned,
            "created_state_file": created_state,
        }
        migration_bundle.atomic_write(
            backup_root / "transaction.json",
            json.dumps(transaction, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        migration_bundle.atomic_write(
            state_path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        verified = json.loads(state_path.read_text(encoding="utf-8"))
        verified_project = (verified.get("local-projects") or {}).get(project_id)
        if not isinstance(verified_project, dict):
            raise ValueError("Offline project registration verification failed")
        if verified_project.get("rootPaths") != [normal_path]:
            raise ValueError("Offline project path verification failed")
        remaining = content_manager._project_reference_paths(verified, set(merged_ids))
        if any(remaining.values()):
            raise ValueError("Merged project IDs still have references after registration")
        for task_id in assigned:
            assignment = (verified.get("thread-project-assignments") or {}).get(task_id)
            if not isinstance(assignment, dict) or assignment.get("projectId") != project_id:
                raise ValueError(f"Task project assignment verification failed: {task_id}")
        transaction["status"] = "committed"
        transaction["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        migration_bundle.atomic_write(
            backup_root / "transaction.json",
            json.dumps(transaction, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return {
            "project_id": project_id,
            "project_path": normal_path,
            "created": created,
            "merged_project_ids": merged_ids,
            "assigned_task_ids": assigned,
            "backup_path": str(backup_root),
        }
    except Exception:
        if original is None:
            state_path.unlink(missing_ok=True)
        else:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, state_path)
        raise
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)

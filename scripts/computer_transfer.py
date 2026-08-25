#!/usr/bin/env python3
"""Combined old-computer package for project files and related Codex conversations."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import content_manager
import migration_bundle
import project_import
import project_registry
import session_merge_planner as planner


KIND = "cross-device-agent-sync-computer-transfer"
SCHEMA_VERSION = 1
PROJECT_COMPONENT = "components/project.cdas.zip"
CONVERSATION_COMPONENT = "components/conversations.cdas.zip"


def _related_conversation_ids(codex_home: Path, project_path: Path) -> set[str]:
    database = migration_bundle.find_state_db(codex_home)
    if database is None:
        return set()
    project_identity = content_manager._project_path_identity(project_path)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in connection.execute("pragma table_info(threads)")}
        if not {"id", "cwd"}.issubset(columns):
            return set()
        result = set()
        for task_id, cwd in connection.execute("select id, cwd from threads"):
            cwd_identity = content_manager._project_path_identity(cwd)
            if cwd_identity and (
                cwd_identity == project_identity
                or cwd_identity.startswith(project_identity.rstrip("\\") + "\\")
            ):
                result.add(str(task_id))
        return result
    finally:
        connection.close()


def _create_conversation_component(
    codex_home: Path,
    project_path: Path,
    output_path: Path,
) -> dict[str, Any] | None:
    inventory = planner.inventory(codex_home, "old-computer")
    related_ids = _related_conversation_ids(codex_home, project_path)
    inventory["conversations"] = [
        item for item in inventory["conversations"] if item["task_id"] in related_ids
    ]
    if not inventory["conversations"]:
        return None
    inventory["inventory_hash"] = planner.sha256_bytes(planner.canonical_json({
        "device_id": inventory["device_id"],
        "conversations": inventory["conversations"],
    }))
    right = {
        "schema_version": 1,
        "kind": "cross-device-agent-sync-inventory",
        "device_id": "new-computer",
        "codex_home": "",
        "generated_at": "",
        "conversations": [],
    }
    right["inventory_hash"] = planner.sha256_bytes(planner.canonical_json({
        "device_id": right["device_id"], "conversations": []
    }))
    plan = planner.compare_inventories(inventory, right, "left-to-right", set(), set())
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        inventory_path, plan_path = root / "inventory.json", root / "plan.json"
        planner.write_json(inventory_path, inventory)
        planner.write_json(plan_path, plan)
        result = migration_bundle.create_bundle(inventory_path, plan_path, "left", output_path)
    result["conversations"] = [
        {
            "task_id": item["task_id"],
            "title": item["title"],
            "updated_at": item["updated_at"],
            "size_bytes": item["size_bytes"],
        }
        for item in inventory["conversations"]
    ]
    return result


def create_computer_bundle(
    codex_home: Path,
    project_path: Path,
    output_path: Path,
    include_project_files: bool = True,
    include_conversations: bool = True,
    include_git: bool = True,
    include_sensitive: bool = False,
) -> dict[str, Any]:
    if not include_project_files and not include_conversations:
        raise ValueError("Select project files, conversations, or both")
    codex_home = codex_home.expanduser().resolve()
    project_path = project_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    components: dict[str, bytes] = {}
    conversations = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        if include_project_files:
            project_bundle = root / "project.cdas.zip"
            project_import.create_project_bundle(
                project_path,
                project_bundle,
                include_git=include_git,
                include_sensitive=include_sensitive,
            )
            components[PROJECT_COMPONENT] = project_bundle.read_bytes()
        if include_conversations:
            conversation_bundle = root / "conversations.cdas.zip"
            conversation_result = _create_conversation_component(
                codex_home, project_path, conversation_bundle
            )
            if conversation_result is not None:
                components[CONVERSATION_COMPONENT] = conversation_bundle.read_bytes()
                conversations = conversation_result["conversations"]
    if not components:
        raise ValueError("No project files or related conversations were found")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "project_name": project_import.safe_project_name(project_path.name),
        "source_project_path": str(project_path),
        "has_project_files": PROJECT_COMPONENT in components,
        "conversations": conversations,
        "component_checksums": {
            name: hashlib.sha256(data).hexdigest() for name, data in components.items()
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(f".{output_path.name}.tmp")
    try:
        with zipfile.ZipFile(temporary_output, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in components.items():
                archive.writestr(name, data)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        temporary_output.replace(output_path)
    finally:
        temporary_output.unlink(missing_ok=True)
    return {
        "bundle_path": str(output_path),
        "project_name": manifest["project_name"],
        "has_project_files": manifest["has_project_files"],
        "conversation_count": len(conversations),
        "bytes": output_path.stat().st_size,
    }


def inspect_computer_bundle(bundle_path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    with zipfile.ZipFile(bundle_path, "r") as archive:
        names = set(archive.namelist())
        if "manifest.json" not in names:
            raise ValueError("Combined transfer package has no manifest")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("kind") != KIND or manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported combined transfer package")
        expected = manifest.get("component_checksums")
        if not isinstance(expected, dict) or names != {"manifest.json", *expected}:
            raise ValueError("Combined transfer package entries do not match its manifest")
        components = {name: archive.read(name) for name in expected}
    for name, checksum in expected.items():
        if hashlib.sha256(components[name]).hexdigest() != checksum:
            raise ValueError(f"Combined transfer component checksum failed: {name}")
    return manifest, components


def _write_components(root: Path, components: dict[str, bytes]) -> dict[str, Path]:
    paths = {}
    for name, data in components.items():
        path = root / Path(name).name
        path.write_bytes(data)
        paths[name] = path
    return paths


def prepare_computer_import(
    bundle_path: Path,
    codex_home: Path,
    projects_root: Path,
) -> dict[str, Any]:
    manifest, components = inspect_computer_bundle(bundle_path)
    with tempfile.TemporaryDirectory() as temporary:
        paths = _write_components(Path(temporary), components)
        if PROJECT_COMPONENT in paths:
            project_preview = project_import.prepare_registered_project_import(
                paths[PROJECT_COMPONENT], projects_root, codex_home
            )
            target_root = project_preview["target_root"]
            registration = project_preview["registration"]
            recommended_action = project_preview["recommended_action"]
        else:
            direct = projects_root.expanduser().resolve() / manifest["project_name"]
            registration = project_registry.inspect_project_conflicts(
                codex_home, direct, manifest["project_name"]
            )
            target_root = direct
            recommended_action = registration["recommended_action"]
            project_preview = None
        conversation_operations = []
        if CONVERSATION_COMPONENT in paths:
            conversation_operations = migration_bundle.prepare_restore(
                paths[CONVERSATION_COMPONENT],
                codex_home,
                path_mapping={manifest["source_project_path"]: str(target_root)},
            )["operations"]
    return {
        "manifest": manifest,
        "codex_home": str(codex_home.expanduser().resolve()),
        "projects_root": str(projects_root.expanduser().resolve()),
        "target_root": str(target_root),
        "registration": registration,
        "project_preview": project_preview,
        "recommended_action": recommended_action,
        "conversation_operations": [
            {key: item[key] for key in (
                "source_task_id", "target_task_id", "title", "action", "target_path"
            )}
            for item in conversation_operations
        ],
    }


def restore_computer_bundle(
    bundle_path: Path,
    codex_home: Path,
    projects_root: Path,
    target_root: Path,
    import_project_files: bool,
    selected_task_ids: set[str],
    expected_state_sha256: str | None,
    registration_action: str = "create_project",
    keeper_id: str | None = None,
    require_codex_closed: bool = True,
) -> dict[str, Any]:
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before importing")
    manifest, components = inspect_computer_bundle(bundle_path)
    if not import_project_files and not selected_task_ids:
        raise ValueError("Select project files, conversations, or both")
    target_root = target_root.expanduser().resolve()
    projects_root = projects_root.expanduser().resolve()
    if target_root.parent != projects_root:
        raise ValueError("Imported project must be a direct child of the selected project root")
    project_result = None
    conversation_result = None
    created_empty = False
    with tempfile.TemporaryDirectory() as temporary:
        paths = _write_components(Path(temporary), components)
        try:
            if import_project_files:
                if PROJECT_COMPONENT not in paths:
                    raise ValueError("The package does not contain project files")
                if registration_action != "create_project":
                    raise ValueError("Project files cannot be merged into an existing project directory")
                project_result = project_import.restore_project_bundle(
                    paths[PROJECT_COMPONENT], projects_root, target_root
                )
            elif not target_root.exists():
                target_root.mkdir(parents=True)
                created_empty = True
            if selected_task_ids:
                if CONVERSATION_COMPONENT not in paths:
                    raise ValueError("The package does not contain conversations")
                conversation_result = migration_bundle.restore_bundle(
                    paths[CONVERSATION_COMPONENT],
                    codex_home,
                    require_codex_closed=False,
                    selected_task_ids=selected_task_ids,
                    path_mapping={manifest["source_project_path"]: str(target_root)},
                )
            imported_task_ids = {
                item["target_task_id"] for item in (conversation_result or {}).get("operations", [])
            }
            registration = project_registry.register_project_offline(
                codex_home,
                target_root,
                target_root.name,
                registration_action,
                task_ids=imported_task_ids,
                keeper_id=keeper_id,
                expected_state_sha256=expected_state_sha256,
                require_codex_closed=False,
            )
        except Exception as error:
            rollback_error = None
            if conversation_result is not None:
                try:
                    migration_bundle.restore_backup(
                        Path(conversation_result["backup_path"]), codex_home, require_codex_closed=False
                    )
                except Exception as rollback_failure:
                    rollback_error = rollback_failure
            try:
                if target_root.is_dir() and (project_result is not None or created_empty):
                    shutil.rmtree(target_root)
            except Exception as rollback_failure:
                rollback_error = rollback_error or rollback_failure
            if rollback_error is not None:
                raise RuntimeError(
                    f"Import failed ({error}); rollback also failed ({rollback_error})"
                ) from error
            raise
    return {
        "project_path": str(target_root),
        "project_id": registration["project_id"],
        "project_files_imported": bool(project_result),
        "conversations_imported": (conversation_result or {}).get("imported", 0),
        "conversations_skipped": (conversation_result or {}).get("skipped", 0),
        "project_backup_path": (project_result or {}).get("backup_path"),
        "conversation_backup_path": (conversation_result or {}).get("backup_path"),
        "registration_backup_path": registration["backup_path"],
    }

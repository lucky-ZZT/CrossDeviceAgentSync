#!/usr/bin/env python3
"""Project transfer that always imports into a new directory."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import generic_sync
import migration_bundle
import project_registry
import session_merge_planner as planner


PROJECT_EXCLUDES = (
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".tmp",
    "tmp",
    "cache",
    "caches",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".turbo",
    "coverage",
    "*.sqlite",
    "*.sqlite-*",
    "*.db",
    "*.db-*",
    "*.socket",
    "*.sock",
)
SENSITIVE_EXCLUDES = (
    ".env",
    ".env.*",
    "auth.json",
    "cookies",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
)
TRANSFER_KIND = "project-import"


def safe_project_name(value: str) -> str:
    name = Path(value).name.strip().rstrip(".")
    if name in {"", ".", ".."}:
        raise ValueError("Project name is empty or invalid")
    safe = "".join("_" if character in '<>:"/\\|?*' else character for character in name).strip()
    if not safe:
        raise ValueError("Project name is invalid")
    return safe


def project_snapshot(source: Path, include_git: bool = True, include_sensitive: bool = False) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"Project directory does not exist: {source}")
    excludes = list(PROJECT_EXCLUDES)
    if not include_git:
        excludes.append(".git")
    if not include_sensitive:
        excludes.extend(SENSITIVE_EXCLUDES)
    return generic_sync.snapshot(
        source,
        safe_project_name(source.name),
        include="**/*",
        exclude=excludes,
        base_excludes=(),
    )


def create_project_bundle(
    source: Path,
    output_path: Path,
    include_git: bool = True,
    include_sensitive: bool = False,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    left = project_snapshot(source, include_git=include_git, include_sensitive=include_sensitive)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        empty = root / "empty"
        empty.mkdir()
        right = generic_sync.snapshot(empty, "new-computer", base_excludes=())
        plan = generic_sync.compare(left, right, "left-to-right")
        left_path = root / "source.json"
        plan_path = root / "plan.json"
        planner.write_json(left_path, left)
        planner.write_json(plan_path, plan)
        result = generic_sync.create_bundle(
            left_path,
            plan_path,
            "left",
            output_path,
            metadata={
                "transfer_kind": TRANSFER_KIND,
                "project_name": safe_project_name(source.name),
                "include_git": include_git,
                "include_sensitive": include_sensitive,
                "skipped_count": left["skipped_count"],
            },
        )
    result["project_name"] = safe_project_name(source.name)
    return result


def inspect_project_bundle(bundle_path: Path) -> dict[str, Any]:
    manifest, _payloads = generic_sync.inspect_bundle(bundle_path)
    metadata = manifest.get("metadata", {})
    if metadata.get("transfer_kind") != TRANSFER_KIND:
        raise ValueError("This is not a project import bundle")
    project_name = safe_project_name(str(metadata.get("project_name", "")))
    return {**manifest, "metadata": {**metadata, "project_name": project_name}}


def next_project_destination(projects_root: Path, project_name: str) -> Path:
    projects_root = projects_root.expanduser().resolve()
    name = f"{safe_project_name(project_name)}-from-old-computer"
    candidate = projects_root / name
    attempt = 2
    while candidate.exists():
        candidate = projects_root / f"{name}-{attempt}"
        attempt += 1
    return candidate


def prepare_project_import(bundle_path: Path, projects_root: Path) -> dict[str, Any]:
    manifest = inspect_project_bundle(bundle_path)
    root = projects_root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"Project import root is not a directory: {root}")
    target = next_project_destination(root, manifest["metadata"]["project_name"])
    prepared = generic_sync.prepare_restore(bundle_path, target)
    file_count = len(prepared["operations"])
    bytes_total = sum(int(item.get("size_bytes", 0)) for item in manifest["files"])
    return {
        "manifest": manifest,
        "projects_root": root,
        "target_root": target,
        "operations": prepared["operations"],
        "file_count": file_count,
        "bytes": bytes_total,
    }


def prepare_registered_project_import(
    bundle_path: Path,
    projects_root: Path,
    codex_home: Path,
) -> dict[str, Any]:
    """Preview direct mapping and renamed fallback without writing target state."""
    manifest = inspect_project_bundle(bundle_path)
    root = projects_root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"Project import root is not a directory: {root}")
    project_name = manifest["metadata"]["project_name"]
    direct_target = root / project_name
    renamed_target = next_project_destination(root, project_name)
    direct_conflict = project_registry.inspect_project_conflicts(
        codex_home, direct_target, project_name
    )
    direct_directory_exists = direct_target.exists()
    if direct_directory_exists or direct_conflict["conflict"] != "none":
        recommended_action = "import_renamed"
        selected_target = renamed_target
        selected_registration = project_registry.inspect_project_conflicts(
            codex_home, renamed_target, renamed_target.name
        )
    else:
        recommended_action = "create_project"
        selected_target = direct_target
        selected_registration = direct_conflict
    prepared = generic_sync.prepare_restore(bundle_path, selected_target)
    return {
        "manifest": manifest,
        "projects_root": root,
        "direct_target": direct_target,
        "renamed_target": renamed_target,
        "target_root": selected_target,
        "direct_directory_exists": direct_directory_exists,
        "direct_conflict": direct_conflict,
        "registration": selected_registration,
        "recommended_action": recommended_action,
        "operations": prepared["operations"],
        "file_count": len(prepared["operations"]),
        "bytes": sum(int(item.get("size_bytes", 0)) for item in manifest["files"]),
    }


def restore_project_bundle(bundle_path: Path, projects_root: Path, target_root: Path) -> dict[str, Any]:
    projects_root = projects_root.expanduser().resolve()
    target_root = target_root.expanduser().resolve()
    if target_root.parent != projects_root:
        raise ValueError("Project target must be a direct child of the selected project import root")
    if target_root.exists():
        raise ValueError("Project target already exists. Recheck the bundle to choose a new safe directory.")
    result = generic_sync.restore_bundle(
        bundle_path,
        target_root,
        backup_parent=projects_root,
    )
    result["project_path"] = str(target_root)
    return result


def restore_registered_project_bundle(
    bundle_path: Path,
    projects_root: Path,
    codex_home: Path,
    target_root: Path,
    project_name: str,
    registration_action: str,
    expected_state_sha256: str | None,
    keeper_id: str | None = None,
    require_codex_closed: bool = True,
) -> dict[str, Any]:
    """Restore a new project directory and register its normal path offline."""
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before importing a project")
    if registration_action not in {"create_project"}:
        raise ValueError("Project file import can only create a new directory registration")
    result = restore_project_bundle(bundle_path, projects_root, target_root)
    try:
        registration = project_registry.register_project_offline(
            codex_home,
            target_root,
            project_name,
            registration_action,
            keeper_id=keeper_id,
            expected_state_sha256=expected_state_sha256,
            require_codex_closed=False,
        )
    except Exception:
        if target_root.is_dir() and target_root.parent == projects_root.expanduser().resolve():
            shutil.rmtree(target_root)
        raise
    result.update({
        "project_id": registration["project_id"],
        "registration_backup_path": registration["backup_path"],
        "registration_action": registration_action,
        "registered_normal_path": registration["project_path"],
    })
    return result

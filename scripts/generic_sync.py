#!/usr/bin/env python3
"""Generic endpoint, agent workspace, and custom-file synchronization."""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import session_merge_planner as planner


SCHEMA_VERSION = 1
KIND = "cross-device-agent-sync-generic"
ProgressCallback = Callable[[str, str], None]


def report_progress(callback: ProgressCallback | None, stage: str, detail: str) -> None:
    if callback is not None:
        callback(stage, detail)
DEFAULT_EXCLUDES = (
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".tmp",
    "tmp",
    "cache",
    "caches",
    "sessions",
    "archived_sessions",
    "session_index.jsonl",
    "state_*.sqlite",
    "memories_*.sqlite",
    "goals_*.sqlite",
    "auth.json",
    "cookies",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.socket",
    "*.sock",
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_patterns(value: str | list[str] | None) -> list[str]:
    if value is None:
        return ["**/*"]
    values = value if isinstance(value, list) else value.replace("\n", ";").split(";")
    return [item.strip().replace("\\", "/") for item in values if item.strip()]


def excluded(relative: str, patterns: list[str]) -> bool:
    parts = relative.split("/")
    for pattern in patterns:
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative, f"{pattern}/**"):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def included(relative: str, patterns: list[str]) -> bool:
    return any(
        pattern in {"*", "**", "**/*"}
        or fnmatch.fnmatch(relative, pattern)
        or fnmatch.fnmatch(relative, f"{pattern}/**")
        for pattern in patterns
    )


def snapshot(
    root: Path,
    endpoint_id: str,
    include: str | list[str] | None = None,
    exclude: str | list[str] | None = None,
    base_excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Endpoint root does not exist: {root}")
    include_patterns = split_patterns(include)
    exclude_patterns = list(base_excludes) + split_patterns(exclude or [])
    items = []
    skipped = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if not included(relative, include_patterns) or excluded(relative, exclude_patterns):
            skipped.append(relative)
            continue
        items.append({
            "path": relative,
            "size_bytes": path.stat().st_size,
            "modified_at": dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).isoformat(),
            "content_hash": file_hash(path),
        })
    core = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "endpoint_id": endpoint_id,
        "root": str(root),
        "include": include_patterns,
        "exclude": exclude_patterns,
        "items": items,
    }
    return {
        **core,
        "created_at": now_iso(),
        "snapshot_hash": planner.sha256_bytes(planner.canonical_json(core)),
        "skipped_count": len(skipped),
    }


def load_snapshot(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("kind") != KIND or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported generic endpoint snapshot: {path}")
    return data


def compare(left: dict[str, Any], right: dict[str, Any], direction: str, selected: set[str] | None = None) -> dict[str, Any]:
    left_items = {item["path"]: item for item in left["items"]}
    right_items = {item["path"]: item for item in right["items"]}
    selected = selected or set()
    entries = []
    for path in sorted(set(left_items) | set(right_items)):
        left_item = left_items.get(path)
        right_item = right_items.get(path)
        if left_item is None:
            classification = "right_only"
        elif right_item is None:
            classification = "left_only"
        elif left_item["content_hash"] == right_item["content_hash"]:
            classification = "identical"
        else:
            classification = "conflict"
        if direction == "left-to-right":
            action = {"left_only": "import_left_to_right", "right_only": "preserve_right", "identical": "skip", "conflict": "copy_left_as_conflict"}[classification]
        elif direction == "right-to-left":
            action = {"left_only": "preserve_left", "right_only": "import_right_to_left", "identical": "skip", "conflict": "copy_right_as_conflict"}[classification]
        else:
            action = {"left_only": "import_left_to_right", "right_only": "import_right_to_left", "identical": "skip", "conflict": "copy_both_as_conflicts"}[classification]
        entries.append({
            "path": path,
            "classification": classification,
            "action": action,
            "selected": path in selected if selected else action != "skip",
            "left": left_item,
            "right": right_item,
        })
    core = {
        "schema_version": SCHEMA_VERSION,
        "kind": f"{KIND}-plan",
        "direction": direction,
        "left_endpoint_id": left["endpoint_id"],
        "right_endpoint_id": right["endpoint_id"],
        "left_snapshot_hash": left["snapshot_hash"],
        "right_snapshot_hash": right["snapshot_hash"],
        "entries": entries,
    }
    return {
        **core,
        "created_at": now_iso(),
        "plan_id": str(uuid.uuid5(uuid.NAMESPACE_URL, planner.sha256_bytes(planner.canonical_json(core)))),
        "summary": {
            "total": len(entries),
            "selected": sum(entry["selected"] for entry in entries),
            "conflicts": sum(entry["classification"] == "conflict" for entry in entries),
        },
    }


def create_bundle(
    snapshot_path: Path,
    plan_path: Path,
    side: str,
    output_path: Path,
    metadata: dict[str, Any] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    report_progress(progress_callback, "metadata", "正在读取文件清单和同步计划...")
    source = load_snapshot(snapshot_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("kind") != f"{KIND}-plan":
        raise ValueError("The plan is not a generic endpoint plan")
    if source["snapshot_hash"] != plan[f"{side}_snapshot_hash"]:
        raise ValueError("The snapshot changed after the plan was created; rescan both endpoints")
    outbound = {
        "left": {"import_left_to_right", "copy_left_as_conflict", "copy_both_as_conflicts"},
        "right": {"import_right_to_left", "copy_right_as_conflict", "copy_both_as_conflicts"},
    }[side]
    source_by_path = {item["path"]: item for item in source["items"]}
    selected = [entry for entry in plan["entries"] if entry["selected"] and entry["action"] in outbound]
    if not selected:
        raise ValueError(f"No selected outbound files for {side}")
    root = Path(source["root"])
    payloads = {}
    files = []
    for index, entry in enumerate(selected, start=1):
        report_progress(progress_callback, "package", f"正在读取文件 {index}/{len(selected)}：{entry['path']}")
        item = source_by_path[entry["path"]]
        path = (root / PurePosixPath(item["path"])).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError(f"Source file is missing or outside endpoint root: {path}")
        data = path.read_bytes()
        if planner.sha256_bytes(data) != item["content_hash"]:
            raise ValueError(f"Source file changed after snapshot: {item['path']}")
        payload = f"files/{item['path']}"
        payloads[payload] = data
        files.append({
            "path": item["path"],
            "payload": payload,
            "content_hash": item["content_hash"],
            "size_bytes": item["size_bytes"],
            "classification": entry["classification"],
        })
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": f"{KIND}-bundle",
        "bundle_id": str(uuid.uuid4()),
        "created_at": now_iso(),
        "source_endpoint_id": source["endpoint_id"],
        "source_snapshot_hash": source["snapshot_hash"],
        "plan_id": plan["plan_id"],
        "files": files,
        "metadata": metadata or {},
        "payload_checksums": {name: planner.sha256_bytes(data) for name, data in payloads.items()},
    }
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            for index, (name, data) in enumerate(payloads.items(), start=1):
                report_progress(progress_callback, "package", f"正在压缩文件 {index}/{len(payloads)}：{name}")
                archive.writestr(name, data)
        report_progress(progress_callback, "finalize", "正在完成迁移包...")
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"bundle_path": str(output_path), "bundle_id": manifest["bundle_id"], "file_count": len(files), "bytes": output_path.stat().st_size}


def inspect_bundle(
    bundle_path: Path,
    progress_callback: ProgressCallback | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    report_progress(progress_callback, "validate", "正在读取文件迁移包清单...")
    with zipfile.ZipFile(bundle_path, "r") as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("kind") != f"{KIND}-bundle" or manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported generic sync bundle")
        expected = manifest.get("payload_checksums", {})
        if set(names) != {"manifest.json", *expected.keys()}:
            raise ValueError("Bundle entries do not match manifest")
        payloads = {}
        for index, (name, checksum) in enumerate(expected.items(), start=1):
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or "\\" in name:
                raise ValueError(f"Unsafe bundle path: {name}")
            report_progress(progress_callback, "validate", f"正在校验文件 {index}/{len(expected)}：{name}")
            data = archive.read(name)
            if planner.sha256_bytes(data) != checksum:
                raise ValueError(f"Checksum failed: {name}")
            payloads[name] = data
        return manifest, payloads


def conflict_path(root: Path, relative: str, endpoint: str, digest: str, attempt: int = 0) -> Path:
    source = Path(*PurePosixPath(relative).parts)
    suffix = source.suffix
    stem = source.name[:-len(suffix)] if suffix else source.name
    tag = f".from-{endpoint}-{digest[:8]}" + (f"-{attempt}" if attempt else "")
    return (root / source.parent / f"{stem}{tag}{suffix}").resolve()


def prepare_restore(
    bundle_path: Path,
    target_root: Path,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    manifest, payloads = inspect_bundle(bundle_path, progress_callback=progress_callback)
    target_root = target_root.expanduser().resolve()
    if target_root.exists() and not target_root.is_dir():
        raise ValueError(f"Target root is not a directory: {target_root}")
    operations = []
    for index, file in enumerate(manifest["files"], start=1):
        report_progress(progress_callback, "compare", f"正在比较文件 {index}/{len(manifest['files'])}：{file['path']}")
        relative = file["path"]
        target = (target_root / PurePosixPath(relative)).resolve()
        if not target.is_relative_to(target_root):
            raise ValueError(f"Target escaped endpoint root: {relative}")
        data = payloads[file["payload"]]
        if target.is_file() and planner.sha256_bytes(target.read_bytes()) == file["content_hash"]:
            operations.append({
                "path": relative,
                "source_path": relative,
                "payload": file["payload"],
                "target_path": str(target),
                "action": "skip_identical",
            })
            continue
        if target.exists():
            destination = conflict_path(target_root, relative, manifest["source_endpoint_id"], file["content_hash"])
            attempt = 0
            while destination.exists():
                if destination.is_file() and planner.sha256_bytes(destination.read_bytes()) == file["content_hash"]:
                    break
                attempt += 1
                destination = conflict_path(target_root, relative, manifest["source_endpoint_id"], file["content_hash"], attempt)
            target = destination
            action = "copy_as_conflict"
        else:
            action = "import"
        operations.append({
            "path": str(target.relative_to(target_root)).replace("\\", "/"),
            "source_path": relative,
            "payload": file["payload"],
            "target_path": str(target),
            "action": action,
        })
    return {
        "manifest": manifest,
        "payloads": payloads,
        "target_root": target_root,
        "operations": operations,
    }


def restore_bundle(
    bundle_path: Path,
    target_root: Path,
    require_empty_lock: bool = True,
    backup_parent: Path | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    report_progress(progress_callback, "preflight", "正在检查文件迁移包和目标目录...")
    prepared = prepare_restore(bundle_path, target_root, progress_callback=progress_callback)
    manifest = prepared["manifest"]
    payloads = prepared["payloads"]
    target_root = prepared["target_root"]
    target_root.mkdir(parents=True, exist_ok=True)
    lock = target_root / ".cross-device-agent-sync.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY) if require_empty_lock else None
    backup_base = (backup_parent or target_root).expanduser().resolve()
    backup_root = backup_base / ".cross-device-agent-sync-backups" / (dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + manifest["bundle_id"][:8])
    created = []
    backups = []
    operations = prepared["operations"]
    try:
        backup_root.mkdir(parents=True, exist_ok=False)
        report_progress(progress_callback, "backup", "正在创建目标目录备份...")
        for index, operation in enumerate(operations, start=1):
            if operation["action"] == "skip_identical":
                continue
            target = Path(operation["target_path"])
            data = payloads[operation["payload"]]
            if target.is_file():
                backup = backup_root / "files" / target.relative_to(target_root)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                backups.append((target, backup))
            else:
                created.append(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            report_progress(progress_callback, "write", f"正在写入文件 {index}/{len(operations)}：{operation['path']}")
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, target)
        (backup_root / "transaction.json").write_text(json.dumps({"status": "committed", "bundle_id": manifest["bundle_id"], "operations": operations}, indent=2), encoding="utf-8")
        report_progress(progress_callback, "complete", "文件导入完成。")
        return {"bundle_id": manifest["bundle_id"], "backup_path": str(backup_root), "operations": operations}
    except Exception:
        for target in created:
            if target.is_file():
                target.unlink()
        for target, backup in reversed(backups):
            shutil.copy2(backup, target)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
            lock.unlink(missing_ok=True)

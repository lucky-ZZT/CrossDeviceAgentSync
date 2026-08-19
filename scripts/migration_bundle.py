#!/usr/bin/env python3
"""Create and restore selective cross-device Codex conversation bundles."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import session_merge_planner as planner


BUNDLE_SCHEMA_VERSION = 1
BUNDLE_KIND = "cross-device-agent-sync-bundle"
BACKUP_SCHEMA_VERSION = 2
BACKUP_DIR_NAME = "cross-device-sync-backups"
BACKUP_STREAM_BYTES = 1024 * 1024
MAX_BACKUP_FIRST_LINE_BYTES = 8 * 1024 * 1024


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def find_state_db(codex_home: Path) -> Path | None:
    candidates = [path for path in codex_home.glob("state_*.sqlite") if path.is_file()]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def normalize_windows_path(value: str) -> str:
    if os.name != "nt":
        return value
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def resolve_local_path(value: Any, base: Path | None = None) -> Path:
    raw = normalize_windows_path(str(value or ""))
    if not raw:
        raise ValueError("Path is empty")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        if base is None:
            raise ValueError(f"Relative path has no base directory: {raw}")
        path = base / path
    return path.resolve()


def row_id(row: dict[str, Any]) -> str | None:
    return planner.first_uuid(row.get("id"), row.get("thread_id"), row.get("conversation_id"))


def read_session_index(codex_home: Path) -> dict[str, dict[str, Any]]:
    path = codex_home / "session_index.jsonl"
    rows: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and (task_id := row_id(row)):
            rows[task_id] = row
    return rows


def encode_db_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value).decode("ascii")}
    return value


def decode_db_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$base64"}:
        return base64.b64decode(value["$base64"])
    return value


def read_sqlite_threads(codex_home: Path, task_ids: set[str]) -> dict[str, dict[str, Any]]:
    database = find_state_db(codex_home)
    if database is None:
        return {}
    uri = f"file:{database.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = {}
        for task_id in task_ids:
            result = connection.execute("select * from threads where id=?", (task_id,)).fetchone()
            if result is not None:
                rows[task_id] = {key: encode_db_value(result[key]) for key in result.keys()}
        return rows
    finally:
        connection.close()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def outbound_for_side(action: str, side: str) -> bool:
    if side == "left":
        return action in {"import", "import_left_to_right", "import_left_as_branch", "exchange_as_branches"}
    return action in {"import", "import_right_to_left", "import_right_as_branch", "exchange_as_branches"}


def create_bundle(
    inventory_path: Path,
    plan_path: Path,
    side: str,
    output_path: Path,
) -> dict[str, Any]:
    inventory = planner.load_inventory(inventory_path)
    plan = load_json(plan_path)
    if plan.get("kind") != "cross-device-agent-sync-plan":
        raise ValueError("The selected plan is not a cross-device sync plan")
    expected_hash = plan.get(f"{side}_inventory_hash")
    if inventory.get("inventory_hash") != expected_hash:
        raise ValueError(f"The {side} inventory does not match the selected plan")
    if inventory.get("device_id") != plan.get(f"{side}_device_id"):
        raise ValueError(f"The {side} device ID does not match the selected plan")

    codex_home = Path(inventory["codex_home"])
    inventory_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in inventory["conversations"]:
        inventory_by_id.setdefault(item["task_id"], []).append(item)
    selected_entries = [
        entry for entry in plan["entries"]
        if entry.get("selected") and outbound_for_side(entry.get("safe_default_action", ""), side)
    ]
    if not selected_entries:
        raise ValueError(f"The plan has no selected outbound conversations for {side}")

    task_ids = {entry["task_id"] for entry in selected_entries}
    index_rows = read_session_index(codex_home)
    sqlite_rows = read_sqlite_threads(codex_home, task_ids)
    payloads: dict[str, bytes] = {}
    conversations = []
    for entry in selected_entries:
        task_id = entry["task_id"]
        matches = inventory_by_id.get(task_id, [])
        if len(matches) != 1:
            raise ValueError(f"Task {task_id} is missing or duplicated in the source inventory")
        item = matches[0]
        source_path = (codex_home / PurePosixPath(item["relative_path"])).resolve()
        if not source_path.is_file() or not source_path.is_relative_to(codex_home.resolve()):
            raise ValueError(f"Session source is missing or outside Codex home: {source_path}")
        data = source_path.read_bytes()
        if planner.sha256_bytes(data) != item["content_hash"]:
            raise ValueError(f"Session changed after inventory: {task_id}. Rescan before packaging")
        payload_name = f"sessions/{task_id}.jsonl"
        payloads[payload_name] = data
        branch_action = "branch" in entry["safe_default_action"] or entry["safe_default_action"] == "exchange_as_branches"
        target_task_id = entry.get(f"proposed_{side}_branch_id") if branch_action else task_id
        conversations.append({
            "source_task_id": task_id,
            "target_task_id": target_task_id,
            "title": item["title"],
            "classification": entry["classification"],
            "safe_default_action": entry["safe_default_action"],
            "source_relative_path": item["relative_path"],
            "payload": payload_name,
            "content_hash": item["content_hash"],
            "session_index_row": index_rows.get(task_id),
            "sqlite_thread_row": sqlite_rows.get(task_id),
        })

    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "kind": BUNDLE_KIND,
        "bundle_id": str(uuid.uuid4()),
        "created_at": now_iso(),
        "source_side": side,
        "source_device_id": inventory["device_id"],
        "source_inventory_hash": inventory["inventory_hash"],
        "plan_id": plan["plan_id"],
        "conversations": conversations,
        "payload_checksums": {name: planner.sha256_bytes(data) for name, data in payloads.items()},
    }
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        for name, data in payloads.items():
            archive.writestr(name, data)
    os.replace(temporary, output_path)
    return {
        "bundle_path": str(output_path),
        "bundle_id": manifest["bundle_id"],
        "conversation_count": len(conversations),
        "bytes": output_path.stat().st_size,
    }


def inspect_bundle(bundle_path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    with zipfile.ZipFile(bundle_path, "r") as archive:
        names = archive.namelist()
        if "manifest.json" not in names or len(names) > 10002:
            raise ValueError("Bundle is missing its manifest or contains too many entries")
        for name in names:
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or "\\" in name:
                raise ValueError(f"Unsafe bundle path: {name}")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("kind") != BUNDLE_KIND or manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
            raise ValueError("Unsupported migration bundle")
        expected = manifest.get("payload_checksums", {})
        if set(names) != {"manifest.json", *expected.keys()}:
            raise ValueError("Bundle entries do not match the manifest")
        payloads = {}
        for name, checksum in expected.items():
            data = archive.read(name)
            if planner.sha256_bytes(data) != checksum:
                raise ValueError(f"Bundle checksum failed: {name}")
            payloads[name] = data
        return manifest, payloads


def replace_strings(value: Any, source_id: str, target_id: str, title_suffix: str) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            rewritten = replace_strings(item, source_id, target_id, title_suffix)
            if key in {"thread_name", "title", "name"} and isinstance(rewritten, str) and title_suffix:
                rewritten = rewritten if rewritten.endswith(title_suffix) else rewritten + title_suffix
            result[key] = rewritten
        return result
    if isinstance(value, list):
        return [replace_strings(item, source_id, target_id, title_suffix) for item in value]
    if isinstance(value, str):
        return value.replace(source_id, target_id)
    return value


def rewrite_jsonl(data: bytes, source_id: str, target_id: str, title_suffix: str) -> bytes:
    output = []
    for raw_line in data.splitlines():
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line.decode("utf-8"))
            output.append(json.dumps(
                replace_strings(value, source_id, target_id, title_suffix),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            output.append(raw_line.replace(source_id.encode(), target_id.encode()))
    return b"\n".join(output) + b"\n"


def target_relative_path(source_relative: str, source_id: str, target_id: str) -> Path:
    pure = PurePosixPath(source_relative)
    if not pure.parts or pure.parts[0] not in {"sessions", "archived_sessions"} or ".." in pure.parts:
        pure = PurePosixPath("sessions") / f"{target_id}.jsonl"
    name = pure.name.replace(source_id, target_id)
    if source_id == target_id:
        name = pure.name
    elif target_id not in name:
        name = f"{target_id}.jsonl"
    return Path(*pure.parent.parts, name)


def derive_alternate_id(bundle_id: str, source_id: str, content_hash: str, attempt: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{BUNDLE_KIND}:{bundle_id}:{source_id}:{content_hash}:{attempt}"))


def prepare_restore(bundle_path: Path, codex_home: Path) -> dict[str, Any]:
    manifest, payloads = inspect_bundle(bundle_path)
    codex_home = codex_home.expanduser().resolve()
    if codex_home.exists() and not codex_home.is_dir():
        raise ValueError(f"Codex home is not a directory: {codex_home}")
    current = (
        planner.inventory(codex_home, "target-preview")
        if codex_home.is_dir()
        else {"conversations": []}
    )
    current_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in current["conversations"]:
        current_by_id.setdefault(item["task_id"], []).append(item)

    operations = []
    reserved = set(current_by_id)
    for conversation in manifest["conversations"]:
        source_id = conversation["source_task_id"]
        target_id = conversation["target_task_id"]
        data = payloads[conversation["payload"]]
        suffix = " [migrated branch]" if target_id != source_id else ""
        rewritten = rewrite_jsonl(data, source_id, target_id, suffix)
        matches = current_by_id.get(target_id, [])
        if len(matches) > 1:
            raise ValueError(f"Target has duplicate local task ID: {target_id}")
        action = "import"
        if matches and matches[0]["content_hash"] == planner.sha256_bytes(rewritten):
            action = "skip_identical"
        elif matches:
            for attempt in range(1000):
                candidate = derive_alternate_id(
                    manifest["bundle_id"], source_id, conversation["content_hash"], attempt
                )
                if candidate not in reserved:
                    target_id = candidate
                    suffix = " [migrated branch]"
                    rewritten = rewrite_jsonl(data, source_id, target_id, suffix)
                    action = "import_as_alternate_branch"
                    break
            else:
                raise ValueError(f"Could not allocate a branch ID for {source_id}")
        reserved.add(target_id)
        relative = target_relative_path(conversation["source_relative_path"], source_id, target_id)
        target_path = (codex_home / relative).resolve()
        if not target_path.is_relative_to(codex_home):
            raise ValueError(f"Restore target escaped Codex home: {target_path}")
        operations.append({
            "source_task_id": source_id,
            "target_task_id": target_id,
            "title": conversation["title"],
            "action": action,
            "target_path": str(target_path),
            "target_relative_path": relative.as_posix(),
            "expected_hash": planner.sha256_bytes(rewritten),
            "rewritten_bytes": rewritten,
            "session_index_row": conversation.get("session_index_row"),
            "sqlite_thread_row": conversation.get("sqlite_thread_row"),
        })
    return {"manifest": manifest, "operations": operations, "codex_home": codex_home}


def codex_is_running() -> bool:
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Codex.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return "codex.exe" in result.stdout.lower()


def acquire_lock(codex_home: Path) -> tuple[int, Path]:
    lock_path = codex_home / ".cross-device-agent-sync.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(descriptor, json.dumps({"pid": os.getpid(), "created_at": now_iso()}).encode("utf-8"))
    return descriptor, lock_path


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def backup_root_for(codex_home: Path) -> Path:
    return codex_home.expanduser().resolve() / BACKUP_DIR_NAME


def read_first_line_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        first_line = stream.readline(MAX_BACKUP_FIRST_LINE_BYTES + 1)
    if len(first_line) > MAX_BACKUP_FIRST_LINE_BYTES:
        raise ValueError(f"Session metadata line is too large: {path}")
    return first_line


def replace_first_line(path: Path, replacement: bytes) -> None:
    if not path.is_file():
        raise ValueError(f"Session file is missing: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.metadata.tmp")
    try:
        original_stat = path.stat()
        with path.open("rb") as reader, temporary.open("wb") as writer:
            current = reader.readline(MAX_BACKUP_FIRST_LINE_BYTES + 1)
            if len(current) > MAX_BACKUP_FIRST_LINE_BYTES:
                raise ValueError(f"Session metadata line is too large: {path}")
            writer.write(replacement)
            shutil.copyfileobj(reader, writer, length=BACKUP_STREAM_BYTES)
            writer.flush()
            os.fsync(writer.fileno())
        shutil.copystat(path, temporary)
        latest_stat = path.stat()
        if (
            latest_stat.st_size != original_stat.st_size
            or latest_stat.st_mtime_ns != original_stat.st_mtime_ns
        ):
            raise ValueError(f"Session changed while its metadata was being rewritten: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_entries(codex_home: Path, backup_root: Path, backed_up: list[tuple[Path, Path]]) -> list[dict[str, Any]]:
    return [
        {
            "target": target.relative_to(codex_home).as_posix(),
            "backup": backup.relative_to(backup_root).as_posix(),
            "sha256": planner.sha256_bytes(backup.read_bytes()),
        }
        for target, backup in backed_up
    ]


def _metadata_backup_entries(
    codex_home: Path,
    backup_root: Path,
    metadata_backed_up: list[tuple[Path, Path]],
) -> list[dict[str, Any]]:
    return [
        {
            "target": target.relative_to(codex_home).as_posix(),
            "backup": backup.relative_to(backup_root).as_posix(),
            "sha256": planner.sha256_bytes(backup.read_bytes()),
        }
        for target, backup in metadata_backed_up
    ]


def _created_entries(codex_home: Path, created_files: list[Path]) -> list[str]:
    return sorted({path.relative_to(codex_home).as_posix() for path in created_files})


def _transaction_payload(
    *,
    status: str,
    operation: str,
    codex_home: Path,
    backup_root: Path,
    backed_up: list[tuple[Path, Path]],
    created_files: list[Path],
    metadata_backed_up: list[tuple[Path, Path]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "status": status,
        "operation": operation,
        "codex_home": str(codex_home),
        "created_at": now_iso(),
        "backed_up": _backup_entries(codex_home, backup_root, backed_up),
        "metadata_backed_up": _metadata_backup_entries(
            codex_home, backup_root, metadata_backed_up or []
        ),
        "created": _created_entries(codex_home, created_files),
    }
    payload.update(extra)
    return payload


def list_backups(codex_home: Path) -> list[dict[str, Any]]:
    root = backup_root_for(codex_home)
    if not root.is_dir():
        return []
    results = []
    for directory in root.iterdir():
        transaction_path = directory / "transaction.json"
        if not directory.is_dir() or not transaction_path.is_file():
            continue
        try:
            transaction = load_json(transaction_path)
            size = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
            restorable = (
                transaction.get("schema_version") == BACKUP_SCHEMA_VERSION
                and transaction.get("status") == "committed"
                and isinstance(transaction.get("backed_up"), list)
                and isinstance(transaction.get("created"), list)
            )
            results.append({
                "path": str(directory.resolve()),
                "name": directory.name,
                "created_at": transaction.get("completed_at") or transaction.get("created_at") or "",
                "operation": transaction.get("operation", "legacy"),
                "item_count": (
                    len(transaction.get("backed_up", []))
                    + len(transaction.get("metadata_backed_up", []))
                    + len(transaction.get("created", []))
                ),
                "size": size,
                "restorable": restorable,
                "status": transaction.get("status", "unknown"),
            })
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return sorted(results, key=lambda item: (item["created_at"], item["name"]), reverse=True)


def _safe_relative(value: str) -> Path:
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError(f"Unsafe backup path: {value}")
    return Path(*pure.parts)


def restore_backup(backup_path: Path, codex_home: Path, require_codex_closed: bool = True) -> dict[str, Any]:
    if require_codex_closed and codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before restoring a backup")
    codex_home = codex_home.expanduser().resolve()
    selected = backup_path.expanduser().resolve()
    root = backup_root_for(codex_home)
    if not selected.is_relative_to(root) or selected.parent != root:
        raise ValueError("The selected backup is not inside this Codex backup directory")
    transaction = load_json(selected / "transaction.json")
    if transaction.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise ValueError("This is a legacy backup without enough recovery metadata and cannot be restored automatically")
    if transaction.get("status") != "committed":
        raise ValueError("Only a completed backup can be restored")

    backed_entries = transaction.get("backed_up")
    metadata_entries = transaction.get("metadata_backed_up", [])
    created_entries = transaction.get("created")
    if (
        not isinstance(backed_entries, list)
        or not isinstance(metadata_entries, list)
        or not isinstance(created_entries, list)
    ):
        raise ValueError("The backup recovery metadata is incomplete")

    restore_pairs: list[tuple[Path, Path, str | None]] = []
    for entry in backed_entries:
        if not isinstance(entry, dict):
            raise ValueError("The backup contains an invalid file entry")
        target = (codex_home / _safe_relative(str(entry.get("target", "")))).resolve()
        source = (selected / _safe_relative(str(entry.get("backup", "")))).resolve()
        if not target.is_relative_to(codex_home) or not source.is_relative_to(selected) or not source.is_file():
            raise ValueError("A backup file is missing or points outside the allowed directory")
        expected = entry.get("sha256")
        if expected and planner.sha256_bytes(source.read_bytes()) != expected:
            raise ValueError(f"Backup checksum failed: {entry['backup']}")
        restore_pairs.append((target, source, expected))

    metadata_restore_pairs: list[tuple[Path, Path, str | None]] = []
    for entry in metadata_entries:
        if not isinstance(entry, dict):
            raise ValueError("The backup contains an invalid metadata entry")
        target = (codex_home / _safe_relative(str(entry.get("target", "")))).resolve()
        source = (selected / _safe_relative(str(entry.get("backup", "")))).resolve()
        if not target.is_relative_to(codex_home) or not source.is_relative_to(selected) or not source.is_file():
            raise ValueError("A metadata backup is missing or points outside the allowed directory")
        expected = entry.get("sha256")
        if expected and planner.sha256_bytes(source.read_bytes()) != expected:
            raise ValueError(f"Metadata backup checksum failed: {entry['backup']}")
        metadata_restore_pairs.append((target, source, expected))

    full_restore_targets = {target for target, _, _ in restore_pairs}
    metadata_restore_targets = {target for target, _, _ in metadata_restore_pairs}
    overlap = full_restore_targets & metadata_restore_targets
    if overlap:
        raise ValueError("A backup cannot restore the same file as both a full file and metadata")

    remove_targets = []
    for value in created_entries:
        target = (codex_home / _safe_relative(str(value))).resolve()
        if not target.is_relative_to(codex_home):
            raise ValueError("A created-file entry points outside the Codex directory")
        remove_targets.append(target)

    descriptor, lock_path = acquire_lock(codex_home)
    guard_root = root / (dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-before-restore")
    guard_backed_up: list[tuple[Path, Path]] = []
    guard_metadata_backed_up: list[tuple[Path, Path]] = []
    guard_created: list[Path] = []
    affected = list(dict.fromkeys(
        [target for target, _, _ in restore_pairs]
        + remove_targets
        + [target for target, _, _ in metadata_restore_pairs]
    ))
    try:
        guard_root.mkdir(parents=True, exist_ok=False)
        for target in affected:
            if target.is_file():
                relative = target.relative_to(codex_home)
                if target in metadata_restore_targets and target not in full_restore_targets:
                    destination = guard_root / "metadata" / relative.parent / f"{relative.name}.first-line"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(read_first_line_bytes(target))
                    guard_metadata_backed_up.append((target, destination))
                else:
                    folder = "database" if target.suffix == ".sqlite" else "files"
                    destination = guard_root / folder / relative
                    if target.suffix == ".sqlite":
                        backup_database(target, destination)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, destination)
                    guard_backed_up.append((target, destination))
            else:
                guard_created.append(target)
        atomic_write(guard_root / "transaction.json", json.dumps(_transaction_payload(
            status="prepared",
            operation="restore_guard",
            codex_home=codex_home,
            backup_root=guard_root,
            backed_up=guard_backed_up,
            created_files=guard_created,
            metadata_backed_up=guard_metadata_backed_up,
            restored_backup=str(selected),
        ), indent=2, ensure_ascii=False).encode("utf-8"))

        for target, source, _ in restore_pairs:
            if target.suffix == ".sqlite":
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        for target, source, _ in metadata_restore_pairs:
            replace_first_line(target, source.read_bytes())
        restored_targets = full_restore_targets | metadata_restore_targets
        for target in remove_targets:
            if target not in restored_targets and target.is_file():
                target.unlink()
        for target, source, expected in restore_pairs:
            checksum = expected or planner.sha256_bytes(source.read_bytes())
            if not target.is_file() or planner.sha256_bytes(target.read_bytes()) != checksum:
                raise ValueError(f"Backup restore verification failed: {target}")
        for target, source, expected in metadata_restore_pairs:
            replacement = source.read_bytes()
            checksum = expected or planner.sha256_bytes(replacement)
            if not target.is_file() or planner.sha256_bytes(read_first_line_bytes(target)) != checksum:
                raise ValueError(f"Metadata backup restore verification failed: {target}")
        remaining = [str(path) for path in remove_targets if path not in restored_targets and path.exists()]
        if remaining:
            raise ValueError(f"Backup restore could not remove: {', '.join(remaining)}")

        atomic_write(guard_root / "transaction.json", json.dumps(_transaction_payload(
            status="committed",
            operation="restore_guard",
            codex_home=codex_home,
            backup_root=guard_root,
            backed_up=guard_backed_up,
            created_files=guard_created,
            metadata_backed_up=guard_metadata_backed_up,
            restored_backup=str(selected),
            completed_at=now_iso(),
        ), indent=2, ensure_ascii=False).encode("utf-8"))
        return {
            "restored_backup": str(selected),
            "safety_backup_path": str(guard_root),
            "restored": len(restore_pairs) + len(metadata_restore_pairs),
            "removed": len([path for path in remove_targets if path not in restored_targets]),
        }
    except Exception:
        for target, backup in reversed(guard_metadata_backed_up):
            if target.is_file():
                replace_first_line(target, backup.read_bytes())
        for target, backup in reversed(guard_backed_up):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        for target in guard_created:
            if target.is_file():
                target.unlink()
        raise
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _backup_transaction(backup_path: Path, codex_home: Path) -> tuple[Path, dict[str, Any]]:
    selected = backup_path.expanduser().resolve()
    root = backup_root_for(codex_home)
    if not selected.is_relative_to(root) or selected.parent != root:
        raise ValueError("The selected backup is not inside this Codex backup directory")
    transaction = load_json(selected / "transaction.json")
    if transaction.get("schema_version") != BACKUP_SCHEMA_VERSION or transaction.get("status") != "committed":
        raise ValueError("Only a completed version-2 backup can be used for selective conversation restore")
    if transaction.get("operation") != "conversation_delete":
        raise ValueError("Selective conversation restore currently supports conversation-delete backups only")
    return selected, transaction


def _backup_entry(transaction: dict[str, Any], target: str) -> dict[str, Any] | None:
    for entry in transaction.get("backed_up", []):
        if isinstance(entry, dict) and entry.get("target") == target:
            return entry
    return None


def _backup_source(selected: Path, entry: dict[str, Any]) -> Path:
    source = (selected / _safe_relative(str(entry.get("backup", "")))).resolve()
    if not source.is_relative_to(selected) or not source.is_file():
        raise ValueError(f"A backup file is missing or unsafe: {entry.get('backup')}")
    expected = entry.get("sha256")
    if expected and planner.sha256_bytes(source.read_bytes()) != expected:
        raise ValueError(f"Backup checksum failed: {entry.get('backup')}")
    return source


def _backup_thread_snapshot(selected: Path, transaction: dict[str, Any], task_id: str) -> tuple[dict[str, Any], Path, dict[str, Any] | None]:
    state_entry = next(
        (
            entry
            for entry in transaction.get("backed_up", [])
            if isinstance(entry, dict) and PurePosixPath(str(entry.get("target", ""))).name.startswith("state_")
        ),
        None,
    )
    if state_entry is None:
        raise ValueError("The backup does not contain a state database")
    state_source = _backup_source(selected, state_entry)
    connection = sqlite3.connect(f"file:{state_source.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("select * from threads where id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"The selected backup does not contain conversation: {task_id}")
        snapshot = {key: encode_db_value(row[key]) for key in row.keys()}
    finally:
        connection.close()
    rollout_name = PureWindowsPath(normalize_windows_path(str(snapshot.get("rollout_path", "")))).name
    rollout_entry = next(
        (
            entry
            for entry in transaction.get("backed_up", [])
            if isinstance(entry, dict)
            and PurePosixPath(str(entry.get("target", ""))).name == rollout_name
        ),
        None,
    )
    if rollout_entry is None:
        rollout_entry = next(
            (
                entry
                for entry in transaction.get("backed_up", [])
                if isinstance(entry, dict) and task_id in PurePosixPath(str(entry.get("target", ""))).name
            ),
            None,
        )
    if rollout_entry is None:
        raise ValueError(f"The selected backup does not contain rollout file: {task_id}")
    catalog_entry = _backup_entry(transaction, "sqlite/codex-dev.db")
    return snapshot, rollout_entry, catalog_entry


def backup_conversation_records(backup_path: Path, codex_home: Path) -> list[dict[str, Any]]:
    """List individual conversations available inside a conversation-delete backup."""
    selected, transaction = _backup_transaction(backup_path, codex_home.expanduser().resolve())
    records: list[dict[str, Any]] = []
    state_entry = next(
        (
            entry
            for entry in transaction.get("backed_up", [])
            if isinstance(entry, dict) and PurePosixPath(str(entry.get("target", ""))).name.startswith("state_")
        ),
        None,
    )
    if state_entry is None:
        return records
    state_source = _backup_source(selected, state_entry)
    catalog_source = None
    catalog_entry = _backup_entry(transaction, "sqlite/codex-dev.db")
    if catalog_entry:
        catalog_source = _backup_source(selected, catalog_entry)
    state_connection = sqlite3.connect(f"file:{state_source.as_posix()}?mode=ro", uri=True)
    state_connection.row_factory = sqlite3.Row
    catalog_connection = (
        sqlite3.connect(f"file:{catalog_source.as_posix()}?mode=ro", uri=True)
        if catalog_source
        else None
    )
    if catalog_connection:
        catalog_connection.row_factory = sqlite3.Row
        catalog_columns = {row[1] for row in catalog_connection.execute("pragma table_info(local_thread_catalog)")}
        catalog_select = [column for column in ("display_title", "cwd", "model_provider") if column in catalog_columns]
        catalog_query = (
            "select " + ",".join(catalog_select) + " from local_thread_catalog "
            "where host_id='local' and thread_id=?"
            if catalog_select
            else None
        )
    else:
        catalog_columns = set()
        catalog_query = None
    try:
        for task_id in transaction.get("task_ids", []):
            row = state_connection.execute("select * from threads where id=?", (task_id,)).fetchone()
            if row is None:
                continue
            catalog = catalog_connection.execute(catalog_query, (task_id,)).fetchone() if catalog_connection and catalog_query else None
            catalog_values = dict(catalog) if catalog else {}
            title = catalog_values.get("display_title") or row["title"]
            rollout_name = PureWindowsPath(normalize_windows_path(str(row["rollout_path"] or ""))).name
            rollout_entry = next(
                (
                    entry
                    for entry in transaction.get("backed_up", [])
                    if isinstance(entry, dict)
                    and (
                        PurePosixPath(str(entry.get("target", ""))).name == rollout_name
                        or task_id in PurePosixPath(str(entry.get("target", ""))).name
                    )
                ),
                None,
            )
            backup_rollout_path = None
            if rollout_entry:
                candidate = (selected / _safe_relative(str(rollout_entry.get("backup", "")))).resolve()
                if candidate.is_relative_to(selected) and candidate.is_file():
                    backup_rollout_path = str(candidate)
            updated_at_ms = row["updated_at_ms"] if "updated_at_ms" in row.keys() else None
            created_at_ms = row["created_at_ms"] if "created_at_ms" in row.keys() else None
            records.append({
                "task_id": task_id,
                "title": title or task_id,
                "cwd": catalog_values.get("cwd") or row["cwd"],
                "model_provider": catalog_values.get("model_provider") or row["model_provider"],
                "created_at": (created_at_ms / 1000) if created_at_ms else row["created_at"],
                "updated_at": (updated_at_ms / 1000) if updated_at_ms else row["updated_at"],
                "rollout_path": rollout_entry.get("target") if rollout_entry else row["rollout_path"],
                "backup_rollout_path": backup_rollout_path,
            })
    finally:
        state_connection.close()
        if catalog_connection:
            catalog_connection.close()
    return records


def _restore_catalog_entry(codex_home: Path, selected: Path, transaction: dict[str, Any], task_id: str) -> bool:
    current = codex_home / "sqlite" / "codex-dev.db"
    catalog_entry = _backup_entry(transaction, "sqlite/codex-dev.db")
    if not current.is_file() or catalog_entry is None:
        return False
    source = _backup_source(selected, catalog_entry)
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    source_connection.row_factory = sqlite3.Row
    target_connection = sqlite3.connect(current)
    try:
        source_row = source_connection.execute(
            "select * from local_thread_catalog where host_id='local' and thread_id=?",
            (task_id,),
        ).fetchone()
        if source_row is None:
            return False
        target_columns = {row[1] for row in target_connection.execute("pragma table_info(local_thread_catalog)")}
        values = {key: source_row[key] for key in source_row.keys() if key in target_columns}
        values["host_id"] = "local"
        values["thread_id"] = task_id
        if "missing_candidate" in target_columns:
            values["missing_candidate"] = 0
        if "observation_sequence" in target_columns:
            sync_table = target_connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_thread_catalog_sync_state'"
            ).fetchone()
            if sync_table:
                target_connection.execute(
                    "update local_thread_catalog_sync_state set observation_sequence=observation_sequence+1 where host_id='local'"
                )
                sequence = target_connection.execute(
                    "select observation_sequence from local_thread_catalog_sync_state where host_id='local'"
                ).fetchone()
                values["observation_sequence"] = sequence[0] if sequence else source_row["observation_sequence"]
        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(f'"{column}"=excluded."{column}"' for column in columns if column not in {"host_id", "thread_id"})
        target_connection.execute(
            f'insert into local_thread_catalog ({",".join(f"\"{column}\"" for column in columns)}) values ({placeholders}) '
            f"on conflict(host_id,thread_id) do update set {updates}",
            [values[column] for column in columns],
        )
        _metadata = target_connection.execute(
            "select 1 from sqlite_master where type='table' and name='local_thread_catalog_metadata'"
        ).fetchone()
        if _metadata:
            target_connection.execute("update local_thread_catalog_metadata set catalog_revision=catalog_revision+1 where id=1")
        target_connection.commit()
        return True
    except Exception:
        target_connection.rollback()
        raise
    finally:
        source_connection.close()
        target_connection.close()


def _backup_selected_paths(codex_home: Path, paths: list[Path], operation: str) -> tuple[Path, list[tuple[Path, Path]]]:
    backup_root = backup_root_for(codex_home) / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{operation}"
    backup_root.mkdir(parents=True, exist_ok=False)
    backed_up: list[tuple[Path, Path]] = []
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(codex_home)
        destination = backup_root / "files" / relative
        if path.suffix == ".sqlite":
            backup_database(path, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        backed_up.append((path, destination))
    return backup_root, backed_up


def restore_conversation_from_backup(
    backup_path: Path,
    codex_home: Path,
    task_id: str,
    require_codex_closed: bool = True,
) -> dict[str, Any]:
    """Restore one deleted conversation without rolling back the rest of Codex."""
    if require_codex_closed and codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before restoring a conversation")
    codex_home = codex_home.expanduser().resolve()
    selected, transaction = _backup_transaction(backup_path, codex_home)
    if task_id not in set(transaction.get("task_ids", [])):
        raise ValueError(f"The selected backup does not contain conversation: {task_id}")
    snapshot, rollout_entry, _ = _backup_thread_snapshot(selected, transaction, task_id)
    database = find_state_db(codex_home)
    if database is None:
        raise ValueError("Codex thread database was not found")
    if read_sqlite_threads(codex_home, {task_id}):
        raise ValueError(f"Conversation already exists and will not be overwritten: {task_id}")
    target_relative = Path(*PurePosixPath(str(rollout_entry["target"])).parts)
    target = (codex_home / target_relative).resolve()
    if not target.is_relative_to(codex_home):
        raise ValueError("The backup rollout path is unsafe")
    source = _backup_source(selected, rollout_entry)
    if target.exists():
        raise ValueError(f"The target rollout path already exists: {target}")
    index_entry = _backup_entry(transaction, "session_index.jsonl")
    index_source = _backup_source(selected, index_entry) if index_entry else None
    index_rows: dict[str, dict[str, Any]] = {}
    if index_source:
        temporary_index = codex_home / f".restore-index-{os.getpid()}.jsonl"
        temporary_index.write_bytes(index_source.read_bytes())
        try:
            index_rows = read_session_index(temporary_index.parent) if temporary_index.name == "session_index.jsonl" else {}
        finally:
            temporary_index.unlink(missing_ok=True)
        if not index_rows:
            for line in index_source.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row_id(row) == task_id:
                    index_rows[task_id] = row
                    break
    index_row = index_rows.get(task_id)
    operation = {
        "action": "import",
        "source_task_id": task_id,
        "target_task_id": task_id,
        "target_path": str(target),
        "title": snapshot.get("title") or task_id,
        "sqlite_thread_row": snapshot,
        "session_index_row": index_row,
    }
    catalog_database = codex_home / "sqlite" / "codex-dev.db"
    backup_paths = [database]
    if (codex_home / "session_index.jsonl").is_file():
        backup_paths.append(codex_home / "session_index.jsonl")
    if catalog_database.is_file():
        backup_paths.append(catalog_database)
    backup_root, backed_up = _backup_selected_paths(codex_home, backup_paths, "conversation-restore")
    descriptor, lock_path = acquire_lock(codex_home)
    try:
        atomic_write(
            backup_root / "transaction.json",
            json.dumps(
                _transaction_payload(
                    status="in_progress",
                    operation="conversation_restore",
                    codex_home=codex_home,
                    backup_root=backup_root,
                    backed_up=backed_up,
                    created_files=[target],
                    restored_backup=str(selected),
                    task_id=task_id,
                ),
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        merge_session_index(codex_home, [operation])
        merge_sqlite(codex_home, [operation])
        catalog_restored = _restore_catalog_entry(codex_home, selected, transaction, task_id)
        verified = read_sqlite_threads(codex_home, {task_id}).get(task_id)
        if verified is None or resolve_local_path(verified.get("rollout_path"), codex_home) != target or not target.is_file():
            raise ValueError(f"Selective conversation restore verification failed: {task_id}")
        if task_id not in read_session_index(codex_home):
            raise ValueError(f"Session index restore verification failed: {task_id}")
        atomic_write(
            backup_root / "transaction.json",
            json.dumps(
                _transaction_payload(
                    status="committed",
                    operation="conversation_restore",
                    codex_home=codex_home,
                    backup_root=backup_root,
                    backed_up=backed_up,
                    created_files=[target],
                    restored_backup=str(selected),
                    task_id=task_id,
                    catalog_restored=catalog_restored,
                    completed_at=now_iso(),
                ),
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        return {
            "restored_backup": str(selected),
            "task_id": task_id,
            "rollout_path": str(target),
            "catalog_restored": catalog_restored,
            "safety_backup_path": str(backup_root),
        }
    except Exception:
        target.unlink(missing_ok=True)
        for restore_target, backup in reversed(backed_up):
            restore_target.parent.mkdir(parents=True, exist_ok=True)
            if restore_target.suffix == ".sqlite":
                backup_database(backup, restore_target)
            else:
                shutil.copy2(backup, restore_target)
        raise
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def merge_session_index(codex_home: Path, operations: list[dict[str, Any]]) -> None:
    path = codex_home / "session_index.jsonl"
    preserved: list[str] = []
    replaced_ids = {operation["target_task_id"] for operation in operations if operation["action"] != "skip_identical"}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict) and row_id(row) in replaced_ids:
                    continue
            except json.JSONDecodeError:
                pass
            if line.strip():
                preserved.append(line)
    for operation in operations:
        if operation["action"] == "skip_identical":
            continue
        source_id = operation["source_task_id"]
        target_id = operation["target_task_id"]
        suffix = " [migrated branch]" if source_id != target_id else ""
        row = operation["session_index_row"] or {
            "id": source_id,
            "thread_name": operation["title"],
            "updated_at": now_iso(),
        }
        row = replace_strings(row, source_id, target_id, suffix)
        row["id"] = target_id
        row["rollout_path"] = operation["target_path"]
        preserved.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    atomic_write(path, ("\n".join(preserved) + "\n").encode("utf-8"))


def sqlite_fallback(column: str, operation: dict[str, Any], provider: str) -> Any:
    now_seconds = int(dt.datetime.now(dt.timezone.utc).timestamp())
    defaults = {
        "id": operation["target_task_id"],
        "rollout_path": operation["target_path"],
        "created_at": now_seconds,
        "updated_at": now_seconds,
        "source": "vscode",
        "model_provider": provider,
        "cwd": str(Path(operation["target_path"]).parent),
        "title": operation["title"],
        "sandbox_policy": '{"type":"read-only"}',
        "approval_mode": "on-request",
        "tokens_used": 0,
        "has_user_event": 1,
        "archived": 0,
        "cli_version": "",
        "first_user_message": "",
        "memory_mode": "enabled",
        "preview": "",
        "recency_at": now_seconds,
        "recency_at_ms": now_seconds * 1000,
        "history_mode": "legacy",
        "is_pinned": 0,
    }
    return defaults.get(column)


def merge_sqlite(codex_home: Path, operations: list[dict[str, Any]]) -> Path | None:
    database = find_state_db(codex_home)
    if database is None:
        return None
    connection = sqlite3.connect(database)
    try:
        columns = connection.execute("pragma table_info(threads)").fetchall()
        names = [column[1] for column in columns]
        required = {column[1] for column in columns if column[3] and column[4] is None}
        for operation in operations:
            if operation["action"] == "skip_identical":
                continue
            source_id = operation["source_task_id"]
            target_id = operation["target_task_id"]
            suffix = " [migrated branch]" if source_id != target_id else ""
            source = {key: decode_db_value(value) for key, value in (operation["sqlite_thread_row"] or {}).items()}
            source = replace_strings(source, source_id, target_id, suffix)
            source["id"] = target_id
            source["rollout_path"] = operation["target_path"]
            provider = str(source.get("model_provider") or "openai")
            values = {}
            for name in names:
                value = source.get(name)
                if value is None and name in required:
                    value = sqlite_fallback(name, operation, provider)
                if value is not None or name in required or name in source:
                    values[name] = value
            insert_names = list(values)
            placeholders = ",".join("?" for _ in insert_names)
            updates = ",".join(f'"{name}"=excluded."{name}"' for name in insert_names if name != "id")
            sql = (
                f'insert into threads ({",".join(f"\"{name}\"" for name in insert_names)}) '
                f'values ({placeholders}) on conflict(id) do update set {updates}'
            )
            connection.execute(sql, [values[name] for name in insert_names])
        connection.commit()
        return database
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def restore_bundle(bundle_path: Path, codex_home: Path, require_codex_closed: bool = True) -> dict[str, Any]:
    if require_codex_closed and codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before restoring")
    prepared = prepare_restore(bundle_path, codex_home)
    operations = prepared["operations"]
    codex_home = prepared["codex_home"]
    codex_home.mkdir(parents=True, exist_ok=True)
    descriptor, lock_path = acquire_lock(codex_home)
    backup_root = backup_root_for(codex_home) / (
        dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + prepared["manifest"]["bundle_id"][:8]
    )
    created_files: list[Path] = []
    backed_up: list[tuple[Path, Path]] = []
    database = find_state_db(codex_home)
    try:
        backup_root.mkdir(parents=True, exist_ok=False)
        targets = [Path(operation["target_path"]) for operation in operations if operation["action"] != "skip_identical"]
        index_path = codex_home / "session_index.jsonl"
        targets.append(index_path)
        if not index_path.exists():
            created_files.append(index_path)
        for target in targets:
            if target.is_file():
                relative = target.relative_to(codex_home)
                destination = backup_root / "files" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, destination)
                backed_up.append((target, destination))
        if database is not None:
            database_backup = backup_root / "database" / database.name
            backup_database(database, database_backup)
            backed_up.append((database, database_backup))
        atomic_write(backup_root / "transaction.json", json.dumps(_transaction_payload(
            status="prepared",
            operation="sync",
            codex_home=codex_home,
            backup_root=backup_root,
            backed_up=backed_up,
            created_files=created_files,
            bundle_id=prepared["manifest"]["bundle_id"],
        ), indent=2, ensure_ascii=False).encode("utf-8"))

        for operation in operations:
            if operation["action"] == "skip_identical":
                continue
            target = Path(operation["target_path"])
            if not target.exists():
                created_files.append(target)
            atomic_write(target, operation["rewritten_bytes"])
        merge_session_index(codex_home, operations)
        merge_sqlite(codex_home, operations)

        verification = planner.inventory(codex_home, "target-verify")
        verified = {item["task_id"]: item for item in verification["conversations"]}
        failures = []
        for operation in operations:
            item = verified.get(operation["target_task_id"])
            if item is None or item["content_hash"] != operation["expected_hash"]:
                failures.append(operation["target_task_id"])
        if failures:
            raise ValueError(f"Restore verification failed for: {', '.join(failures)}")
        index_rows = read_session_index(codex_home)
        missing_index = [op["target_task_id"] for op in operations if op["target_task_id"] not in index_rows]
        if missing_index:
            raise ValueError(f"Session index verification failed for: {', '.join(missing_index)}")
        if database is not None:
            rows = read_sqlite_threads(codex_home, {op["target_task_id"] for op in operations})
            missing_db = [op["target_task_id"] for op in operations if op["target_task_id"] not in rows]
            if missing_db:
                raise ValueError(f"SQLite verification failed for: {', '.join(missing_db)}")
        atomic_write(backup_root / "transaction.json", json.dumps(_transaction_payload(
            status="committed",
            operation="sync",
            codex_home=codex_home,
            backup_root=backup_root,
            backed_up=backed_up,
            created_files=created_files,
            bundle_id=prepared["manifest"]["bundle_id"],
            completed_at=now_iso(),
            imported=sum(op["action"] != "skip_identical" for op in operations),
        ), indent=2, ensure_ascii=False).encode("utf-8"))
        return {
            "bundle_id": prepared["manifest"]["bundle_id"],
            "backup_path": str(backup_root),
            "imported": sum(op["action"] != "skip_identical" for op in operations),
            "skipped": sum(op["action"] == "skip_identical" for op in operations),
            "operations": [{key: op[key] for key in ("source_task_id", "target_task_id", "title", "action")} for op in operations],
        }
    except Exception:
        for target in created_files:
            if target.is_file():
                target.unlink()
        for target, backup in reversed(backed_up):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        raise
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)

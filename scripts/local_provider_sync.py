#!/usr/bin/env python3
"""Discover local Codex providers, clone conversations, or reassign ownership."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import tomllib
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import migration_bundle
import session_merge_planner as planner


STREAM_CHUNK_BYTES = 1024 * 1024
MAX_SESSION_META_BYTES = 8 * 1024 * 1024


def _rollout_path(codex_home: Path, value: Any) -> Path:
    return migration_bundle.resolve_local_path(value, codex_home)


def _session_meta_providers(codex_home: Path) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for directory in ("sessions", "archived_sessions"):
        root = codex_home / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            if not path.is_file():
                continue
            providers: set[str] = set()
            try:
                with path.open("rb") as stream:
                    first_line = stream.readline(1024 * 1024)
                value = json.loads(first_line.decode("utf-8-sig"))
                if isinstance(value, dict):
                    payload = value.get("payload") if isinstance(value.get("payload"), dict) else value
                    for key in ("model_provider", "provider"):
                        provider = payload.get(key)
                        if isinstance(provider, str) and provider:
                            providers.add(provider)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            for provider in providers or {"openai"}:
                counts[provider] += 1
    return counts


def discover_providers(codex_home: Path) -> list[dict[str, Any]]:
    codex_home = codex_home.expanduser().resolve()
    found: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "sources": set(), "rollout_count": 0, "sqlite_count": 0, "configured": False, "current": False
    })
    config_path = codex_home / "config.toml"
    if config_path.is_file():
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            data = {}
        current = str(data.get("model_provider") or "openai")
        found[current]["current"] = True
        found[current]["sources"].add("config")
        for provider in (data.get("model_providers") or {}):
            found[str(provider)]["configured"] = True
            found[str(provider)]["sources"].add("config")
    else:
        found["openai"]["current"] = True
        found["openai"]["sources"].add("default")

    for provider, count in _session_meta_providers(codex_home).items():
        found[provider]["rollout_count"] = count
        found[provider]["sources"].add("rollout")

    database = migration_bundle.find_state_db(codex_home)
    if database is not None:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            for provider, count in connection.execute(
                "select coalesce(model_provider,'openai'),count(*) from threads group by model_provider"
            ):
                found[provider]["sqlite_count"] = count
                found[provider]["sources"].add("sqlite")
        finally:
            connection.close()

    return [
        {"id": provider, **details, "sources": sorted(details["sources"])}
        for provider, details in sorted(found.items(), key=lambda item: (not item[1]["current"], item[0]))
    ]


def list_provider_threads(codex_home: Path, provider: str) -> list[dict[str, Any]]:
    codex_home = codex_home.expanduser().resolve()
    database = migration_bundle.find_state_db(codex_home)
    if database is None:
        raise ValueError("Codex thread database was not found")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "select id,title,updated_at,rollout_path,model_provider,agent_nickname,agent_path "
            "from threads where coalesce(model_provider,'openai')=? order by updated_at desc",
            (provider,),
        ).fetchall()
        threads = []
        for row in rows:
            path = _rollout_path(codex_home, row["rollout_path"])
            try:
                if not path.is_file():
                    continue
                size_bytes = path.stat().st_size
            except OSError:
                continue
            thread = dict(row)
            thread["size_bytes"] = size_bytes
            threads.append(thread)
        return threads
    finally:
        connection.close()


def assign_provider(value: Any, target_provider: str) -> Any:
    if isinstance(value, dict):
        result = {key: assign_provider(item, target_provider) for key, item in value.items()}
        if value.get("type") == "session_meta" and isinstance(result.get("payload"), dict):
            result["payload"]["model_provider"] = target_provider
        for key in ("model_provider", "provider"):
            if key in result:
                result[key] = target_provider
        return result
    if isinstance(value, list):
        return [assign_provider(item, target_provider) for item in value]
    return value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(STREAM_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _write_and_hash(stream, digest, data: bytes) -> None:
    stream.write(data)
    digest.update(data)


def _stream_clone_session(
    source: Path,
    destination: Path,
    source_id: str,
    target_id: str,
    target_provider: str,
) -> dict[str, Any]:
    before = source.stat()
    destination.parent.mkdir(parents=True, exist_ok=True)
    output_hash = hashlib.sha256()
    encrypted_needle = b"encrypted_content"
    encrypted_carry = b""
    encrypted = False
    source_bytes = 0
    old_id = source_id.encode("ascii")
    new_id = target_id.encode("ascii")
    replacement_carry = b""

    with source.open("rb") as reader, destination.open("wb") as writer:
        first_line_with_separator = reader.readline(MAX_SESSION_META_BYTES + 1)
        source_bytes += len(first_line_with_separator)
        if len(first_line_with_separator) > MAX_SESSION_META_BYTES:
            raise ValueError(f"Session metadata line is too large: {source}")
        if first_line_with_separator.endswith(b"\r\n"):
            first_line = first_line_with_separator[:-2]
            separator = b"\r\n"
        elif first_line_with_separator.endswith(b"\n"):
            first_line = first_line_with_separator[:-1]
            separator = b"\n"
        else:
            first_line = first_line_with_separator
            separator = b""
        try:
            value = json.loads(first_line.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Session metadata is not valid JSON: {source}") from error
        if not isinstance(value, dict) or value.get("type") != "session_meta":
            raise ValueError(f"Session does not start with session_meta: {source}")
        payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
        metadata_id = planner.first_uuid(
            payload.get("id"), payload.get("thread_id"), payload.get("conversation_id"), source.name
        )
        if metadata_id != source_id:
            raise ValueError(f"Session ID does not match SQLite metadata: {source_id}")
        value = assign_provider(value, target_provider)
        value = migration_bundle.replace_strings(value, source_id, target_id, " [migrated branch]")
        rewritten_first_line = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _write_and_hash(writer, output_hash, rewritten_first_line + separator)
        encrypted = encrypted_needle in first_line
        encrypted_carry = first_line[-(len(encrypted_needle) - 1):]

        keep = len(old_id) - 1
        while chunk := reader.read(STREAM_CHUNK_BYTES):
            source_bytes += len(chunk)
            if not encrypted:
                combined = encrypted_carry + chunk
                encrypted = encrypted_needle in combined
                encrypted_carry = combined[-(len(encrypted_needle) - 1):]
            combined = replacement_carry + chunk
            if len(combined) <= keep:
                replacement_carry = combined
                continue
            ready, replacement_carry = combined[:-keep], combined[-keep:]
            _write_and_hash(writer, output_hash, ready.replace(old_id, new_id))
        if replacement_carry:
            _write_and_hash(writer, output_hash, replacement_carry.replace(old_id, new_id))
        writer.flush()
        os.fsync(writer.fileno())

    after = source.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        destination.unlink(missing_ok=True)
        raise ValueError(f"Session changed while it was being copied: {source}")
    return {
        "content_hash": output_hash.hexdigest(),
        "source_bytes": source_bytes,
        "encrypted": encrypted,
    }


def _copy_staged_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with source.open("rb") as reader, temporary.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=STREAM_CHUNK_BYTES)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _provider_first_line(
    first_line_with_separator: bytes,
    source_id: str,
    source_provider: str,
    target_provider: str,
) -> bytes:
    if first_line_with_separator.endswith(b"\r\n"):
        first_line = first_line_with_separator[:-2]
        separator = b"\r\n"
    elif first_line_with_separator.endswith(b"\n"):
        first_line = first_line_with_separator[:-1]
        separator = b"\n"
    else:
        first_line = first_line_with_separator
        separator = b""
    try:
        value = json.loads(first_line.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Session metadata is not valid JSON") from error
    if not isinstance(value, dict) or value.get("type") != "session_meta":
        raise ValueError("Session does not start with session_meta")
    payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
    metadata_id = planner.first_uuid(
        payload.get("id"), payload.get("thread_id"), payload.get("conversation_id")
    )
    if metadata_id != source_id:
        raise ValueError(f"Session ID does not match SQLite metadata: {source_id}")
    current_provider = str(payload.get("model_provider") or payload.get("provider") or "openai")
    if current_provider != source_provider:
        raise ValueError(
            f"Conversation {source_id} rollout belongs to Provider {current_provider}, not {source_provider}"
        )
    rewritten = assign_provider(value, target_provider)
    return json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + separator


def _rewrite_session_provider(
    path: Path,
    source_id: str,
    source_provider: str,
    target_provider: str,
) -> dict[str, Any]:
    before = path.stat()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.provider.tmp")
    encrypted_needle = b"encrypted_content"
    encrypted_carry = b""
    encrypted = False
    source_bytes = 0
    try:
        with path.open("rb") as reader, temporary.open("wb") as writer:
            original_first_line = reader.readline(MAX_SESSION_META_BYTES + 1)
            if len(original_first_line) > MAX_SESSION_META_BYTES:
                raise ValueError(f"Session metadata line is too large: {path}")
            source_bytes += len(original_first_line)
            replacement = _provider_first_line(
                original_first_line, source_id, source_provider, target_provider
            )
            writer.write(replacement)
            encrypted = encrypted_needle in original_first_line
            encrypted_carry = original_first_line[-(len(encrypted_needle) - 1):]
            while chunk := reader.read(STREAM_CHUNK_BYTES):
                source_bytes += len(chunk)
                if not encrypted:
                    combined = encrypted_carry + chunk
                    encrypted = encrypted_needle in combined
                    encrypted_carry = combined[-(len(encrypted_needle) - 1):]
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        shutil.copystat(path, temporary)
        after = path.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise ValueError(f"Session changed while its Provider was being reassigned: {path}")
        os.replace(temporary, path)
        return {"source_bytes": source_bytes, "encrypted": encrypted}
    finally:
        temporary.unlink(missing_ok=True)


def _update_selected_sqlite_provider(
    database: Path,
    selected_ids: set[str],
    source_provider: str,
    target_provider: str,
) -> None:
    placeholders = ",".join("?" for _ in selected_ids)
    connection = sqlite3.connect(database, timeout=10)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            f"update threads set model_provider=? where id in ({placeholders}) "
            "and coalesce(model_provider,'openai')=?",
            [target_provider, *sorted(selected_ids), source_provider],
        )
        if cursor.rowcount != len(selected_ids):
            raise ValueError(
                f"SQLite Provider update matched {cursor.rowcount} of {len(selected_ids)} selected conversations"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class ProviderPreflightError(ValueError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__(format_provider_preflight(report))


def _preflight_problem(
    problems: list[dict[str, Any]],
    code: str,
    message: str,
    task_id: str | None = None,
    path: Path | None = None,
) -> None:
    item: dict[str, Any] = {"code": code, "message": message}
    if task_id:
        item["task_id"] = task_id
    if path is not None:
        item["path"] = str(path)
    problems.append(item)


def format_provider_preflight(report: dict[str, Any]) -> str:
    if report.get("ok"):
        return (
            f"预检通过：{report['selected_count']} 个会话，"
            f"{report['selected_bytes']} 字节；预计需要 {report['required_bytes']} 字节可用空间。"
        )
    lines = [f"执行前检查未通过，共发现 {len(report.get('problems', []))} 个问题；尚未修改任何数据："]
    for index, problem in enumerate(report.get("problems", []), start=1):
        context = ""
        if problem.get("task_id"):
            context += f" [会话 {problem['task_id']}]"
        if problem.get("path"):
            context += f"\n   路径：{problem['path']}"
        lines.append(f"{index}. {problem['message']}{context}")
    return "\n".join(lines)


def preflight_provider_operation(
    codex_home: Path,
    source_provider: str,
    target_provider: str,
    selected_ids: set[str],
    *,
    operation: str,
    create_backup: bool,
    require_codex_closed: bool,
) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    selected_bytes = 0
    largest_bytes = 0
    database: Path | None = None

    try:
        codex_home = migration_bundle.resolve_local_path(codex_home)
    except (OSError, ValueError) as error:
        _preflight_problem(problems, "invalid_codex_home", f"Codex 数据位置无效：{error}")
        return {
            "ok": False,
            "problems": problems,
            "warnings": warnings,
            "operations": operations,
            "selected_count": len(selected_ids),
            "selected_bytes": 0,
            "largest_bytes": 0,
            "required_bytes": 0,
            "free_bytes": 0,
            "codex_home": str(codex_home),
            "database": None,
        }

    if not selected_ids:
        _preflight_problem(problems, "empty_selection", "没有选择任何会话。")
    if not source_provider or not target_provider:
        _preflight_problem(problems, "empty_provider", "来源 Provider 或目标 Provider 为空。")
    elif source_provider == target_provider:
        _preflight_problem(problems, "same_provider", "来源 Provider 和目标 Provider 不能相同。")
    for label, provider in (("来源", source_provider), ("目标", target_provider)):
        if any(ord(character) < 32 for character in provider) or any(
            character in provider for character in ("/", "\\", "\0")
        ):
            _preflight_problem(problems, "invalid_provider", f"{label} Provider 名称包含非法字符。")

    if require_codex_closed and migration_bundle.codex_is_running():
        _preflight_problem(
            problems,
            "codex_running",
            "检测到 Codex 仍在运行。请完全退出 Codex 桌面端、CLI 和相关终端。",
        )
    if not codex_home.is_dir():
        _preflight_problem(problems, "missing_codex_home", "Codex 数据目录不存在。", path=codex_home)
    elif not os.access(codex_home, os.R_OK | os.W_OK):
        _preflight_problem(problems, "codex_home_permissions", "Codex 数据目录不可读写。", path=codex_home)
    lock_path = codex_home / ".cross-device-agent-sync.lock"
    if lock_path.exists():
        _preflight_problem(
            problems,
            "operation_locked",
            "发现未完成或正在执行的同步锁；请确认没有另一个同步工具实例在运行。",
            path=lock_path,
        )

    if codex_home.is_dir():
        database = migration_bundle.find_state_db(codex_home)
    if database is None:
        _preflight_problem(problems, "missing_database", "未找到 Codex 状态数据库 state_*.sqlite。")
        database_rows: dict[str, dict[str, Any]] = {}
    else:
        try:
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=3)
            try:
                integrity = connection.execute("pragma quick_check").fetchone()
                columns = {row[1] for row in connection.execute("pragma table_info(threads)")}
            finally:
                connection.close()
            if not integrity or integrity[0] != "ok":
                _preflight_problem(problems, "database_integrity", "SQLite 完整性检查未通过。", path=database)
            missing_columns = {"id", "rollout_path", "model_provider"} - columns
            if missing_columns:
                _preflight_problem(
                    problems,
                    "database_schema",
                    f"SQLite threads 表缺少字段：{', '.join(sorted(missing_columns))}。",
                    path=database,
                )
            database_rows = migration_bundle.read_sqlite_threads(codex_home, selected_ids)
        except (OSError, sqlite3.Error) as error:
            _preflight_problem(problems, "database_read", f"SQLite 无法读取：{error}", path=database)
            database_rows = {}

    normalized_paths: dict[str, str] = {}
    for task_id in sorted(selected_ids):
        row = database_rows.get(task_id)
        if row is None:
            _preflight_problem(problems, "missing_sqlite_row", "SQLite 中不存在所选会话。", task_id=task_id)
            continue
        provider = str(row.get("model_provider") or "openai")
        if provider != source_provider:
            _preflight_problem(
                problems,
                "provider_drift",
                f"会话当前属于 {provider}，不再属于所选来源 {source_provider}。",
                task_id=task_id,
            )
        try:
            path = _rollout_path(codex_home, row.get("rollout_path"))
        except (OSError, ValueError) as error:
            _preflight_problem(problems, "invalid_rollout_path", f"会话路径无法解析：{error}", task_id=task_id)
            continue
        if not path.is_relative_to(codex_home):
            _preflight_problem(
                problems,
                "outside_codex_home",
                "会话文件位于 Codex 数据目录之外。",
                task_id=task_id,
                path=path,
            )
            continue
        path_key = os.path.normcase(str(path))
        duplicate_id = normalized_paths.get(path_key)
        if duplicate_id:
            _preflight_problem(
                problems,
                "duplicate_rollout_path",
                f"该文件同时被会话 {duplicate_id} 引用。",
                task_id=task_id,
                path=path,
            )
            continue
        normalized_paths[path_key] = task_id
        if not path.is_file():
            _preflight_problem(problems, "missing_rollout", "会话文件不存在。", task_id=task_id, path=path)
            continue
        if not os.access(path, os.R_OK | os.W_OK) or not os.access(path.parent, os.W_OK):
            _preflight_problem(
                problems,
                "rollout_permissions",
                "会话文件或其目录不可读写。",
                task_id=task_id,
                path=path,
            )
            continue
        try:
            size = path.stat().st_size
            first_line = migration_bundle.read_first_line_bytes(path)
            _provider_first_line(first_line, task_id, source_provider, target_provider)
        except (OSError, ValueError) as error:
            _preflight_problem(
                problems,
                "invalid_rollout_metadata",
                f"会话元数据检查失败：{error}",
                task_id=task_id,
                path=path,
            )
            continue
        selected_bytes += size
        largest_bytes = max(largest_bytes, size)
        operations.append({
            "task_id": task_id,
            "path": path,
            "original_first_line": first_line,
            "sqlite_row": row,
            "size_bytes": size,
        })

    database_bytes = database.stat().st_size if database is not None and database.is_file() else 0
    buffer_bytes = 64 * 1024 * 1024
    if operation == "clone":
        required_bytes = selected_bytes * 2 + database_bytes + buffer_bytes
    else:
        persistent_backup_bytes = selected_bytes + database_bytes if create_backup else database_bytes
        required_bytes = persistent_backup_bytes + largest_bytes + buffer_bytes
    try:
        free_bytes = shutil.disk_usage(codex_home).free if codex_home.is_dir() else 0
    except OSError as error:
        free_bytes = 0
        _preflight_problem(problems, "disk_query", f"无法读取磁盘剩余空间：{error}", path=codex_home)
    if free_bytes < required_bytes:
        _preflight_problem(
            problems,
            "insufficient_space",
            f"磁盘空间不足；预计需要 {required_bytes} 字节，当前可用 {free_bytes} 字节。",
            path=codex_home,
        )

    return {
        "ok": not problems and len(operations) == len(selected_ids),
        "problems": problems,
        "warnings": warnings,
        "operations": operations,
        "selected_count": len(selected_ids),
        "selected_bytes": selected_bytes,
        "largest_bytes": largest_bytes,
        "required_bytes": required_bytes,
        "free_bytes": free_bytes,
        "codex_home": str(codex_home),
        "database": str(database) if database is not None else None,
    }


def reassign_provider(
    codex_home: Path,
    source_provider: str,
    target_provider: str,
    selected_ids: set[str],
    require_codex_closed: bool = True,
    create_backup: bool = True,
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    def progress(stage: str, detail: str) -> None:
        if progress_callback is not None:
            progress_callback(stage, detail)

    started_at = time.perf_counter()
    progress("preflight", "正在检查 Codex 状态、会话选择和数据文件...")
    report = preflight_provider_operation(
        codex_home,
        source_provider,
        target_provider,
        selected_ids,
        operation="reassign",
        create_backup=create_backup,
        require_codex_closed=require_codex_closed,
    )
    if not report["ok"]:
        raise ProviderPreflightError(report)
    codex_home = Path(report["codex_home"])
    database = Path(report["database"])
    operations = report["operations"]

    transaction_id = str(uuid.uuid4())
    temporary_backup = None
    if create_backup:
        backup_root = migration_bundle.backup_root_for(codex_home) / (
            dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + transaction_id[:8]
        )
    else:
        temporary_backup = tempfile.TemporaryDirectory(prefix="provider-reassign-")
        backup_root = Path(temporary_backup.name)

    descriptor, lock_path = migration_bundle.acquire_lock(codex_home)
    backed_up: list[tuple[Path, Path]] = []
    metadata_backed_up: list[tuple[Path, Path]] = []
    rewritten_paths: list[Path] = []
    scanned_bytes = 0
    encrypted_count = 0
    try:
        progress(
            "backup",
            "正在完整备份所选会话和数据库..." if create_backup else "正在创建本次操作的临时回滚点...",
        )
        if create_backup:
            backup_root.mkdir(parents=True, exist_ok=False)
        database_backup = backup_root / "database" / database.name
        migration_bundle.backup_database(database, database_backup)
        backed_up.append((database, database_backup))
        for operation in operations:
            if create_backup:
                full_backup = backup_root / "files" / operation["path"].relative_to(codex_home)
                full_backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(operation["path"], full_backup)
                backed_up.append((operation["path"], full_backup))
            else:
                metadata_backup = backup_root / "metadata" / f"{operation['task_id']}.first-line"
                metadata_backup.parent.mkdir(parents=True, exist_ok=True)
                metadata_backup.write_bytes(operation["original_first_line"])
                metadata_backed_up.append((operation["path"], metadata_backup))

        if create_backup:
            migration_bundle.atomic_write(
                backup_root / "transaction.json",
                json.dumps(migration_bundle._transaction_payload(
                    status="prepared",
                    operation="provider_reassign",
                    codex_home=codex_home,
                    backup_root=backup_root,
                    backed_up=backed_up,
                    created_files=[],
                    metadata_backed_up=metadata_backed_up,
                    bundle_id=transaction_id,
                    source_provider=source_provider,
                    target_provider=target_provider,
                ), indent=2, ensure_ascii=False).encode("utf-8"),
            )

        progress("rollouts", f"正在切换 {len(operations)} 个会话文件的 Provider 归属...")
        for index, operation in enumerate(operations, start=1):
            progress("rollouts", f"正在切换会话 {index}/{len(operations)}：{operation['task_id']}")
            result = _rewrite_session_provider(
                operation["path"], operation["task_id"], source_provider, target_provider
            )
            rewritten_paths.append(operation["path"])
            scanned_bytes += result["source_bytes"]
            encrypted_count += int(result["encrypted"])
        progress("database", "正在更新 SQLite 中所选会话的 Provider 记录...")
        _update_selected_sqlite_provider(database, selected_ids, source_provider, target_provider)

        progress("verify", "正在核对会话文件和 SQLite，确认切换结果...")
        verified_database = migration_bundle.read_sqlite_threads(codex_home, selected_ids)
        wrong_database = sorted(
            task_id for task_id, row in verified_database.items()
            if str(row.get("model_provider") or "openai") != target_provider
        )
        wrong_rollouts = []
        for operation in operations:
            first_line = migration_bundle.read_first_line_bytes(operation["path"])
            try:
                _provider_first_line(first_line, operation["task_id"], target_provider, target_provider)
            except ValueError:
                wrong_rollouts.append(operation["task_id"])
        if wrong_database or wrong_rollouts:
            failures = sorted(set(wrong_database + wrong_rollouts))
            raise ValueError(f"Provider reassignment verification failed for: {', '.join(failures)}")

        if create_backup:
            migration_bundle.atomic_write(
                backup_root / "transaction.json",
                json.dumps(migration_bundle._transaction_payload(
                    status="committed",
                    operation="provider_reassign",
                    codex_home=codex_home,
                    backup_root=backup_root,
                    backed_up=backed_up,
                    created_files=[],
                    metadata_backed_up=metadata_backed_up,
                    bundle_id=transaction_id,
                    source_provider=source_provider,
                    target_provider=target_provider,
                    completed_at=migration_bundle.now_iso(),
                    reassigned=len(operations),
                ), indent=2, ensure_ascii=False).encode("utf-8"),
            )
        progress("complete", "切换和验证均已完成。")
        return {
            "bundle_id": transaction_id,
            "backup_path": str(backup_root) if create_backup else None,
            "backup_created": create_backup,
            "reassigned": len(operations),
            "operations": [
                {"task_id": operation["task_id"], "action": "reassign_provider"}
                for operation in operations
            ],
            "encrypted_content_warnings": encrypted_count,
            "source_provider": source_provider,
            "target_provider": target_provider,
            "scanned_conversations": len(operations),
            "scanned_bytes": scanned_bytes,
            "duration_seconds": time.perf_counter() - started_at,
        }
    except Exception:
        if create_backup:
            for target, backup in reversed(backed_up):
                if backup.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
        else:
            for target, backup in reversed(metadata_backed_up):
                if target in rewritten_paths and target.is_file():
                    migration_bundle.replace_first_line(target, backup.read_bytes())
            if database_backup.is_file():
                shutil.copy2(database_backup, database)
        raise
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)
        if temporary_backup is not None:
            temporary_backup.cleanup()


def _selected_operations(
    codex_home: Path,
    database_rows: dict[str, dict[str, Any]],
    index_rows: dict[str, dict[str, Any]],
    source_provider: str,
    target_provider: str,
    selected_ids: set[str],
    staging_root: Path,
) -> tuple[list[dict[str, Any]], int, int]:
    missing = selected_ids - database_rows.keys()
    if missing:
        raise ValueError(f"Selected conversations are missing from SQLite: {', '.join(sorted(missing))}")
    operations = []
    total_source_bytes = 0
    encrypted_count = 0
    for source_id in sorted(selected_ids):
        row = database_rows[source_id]
        provider = str(row.get("model_provider") or "openai")
        if provider != source_provider:
            raise ValueError(f"Conversation {source_id} belongs to Provider {provider}, not {source_provider}")
        source_path = _rollout_path(codex_home, row.get("rollout_path"))
        if not source_path.is_file() or not source_path.is_relative_to(codex_home):
            raise ValueError(f"Selected session is missing or outside Codex home: {source_path}")
        relative = source_path.relative_to(codex_home).as_posix()
        base_target_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"local-provider-sync:{source_provider}:{target_provider}:{source_id}",
        ))
        target_id = base_target_id
        action = "import"
        for attempt in range(1000):
            staged = staging_root / f"{target_id}.jsonl"
            result = _stream_clone_session(
                source_path, staged, source_id, target_id, target_provider
            )
            target_relative = migration_bundle.target_relative_path(relative, source_id, target_id)
            target_path = (codex_home / target_relative).resolve()
            if not target_path.is_relative_to(codex_home):
                raise ValueError(f"Provider clone target escaped Codex home: {target_path}")
            if not target_path.exists():
                break
            if target_path.is_file() and _hash_file(target_path) == result["content_hash"]:
                action = "skip_identical"
                break
            target_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"local-provider-sync:{source_provider}:{target_provider}:{source_id}:{result['content_hash']}:{attempt}",
            ))
            action = "import_as_alternate_branch"
        else:
            raise ValueError(f"Could not allocate a target conversation ID for {source_id}")

        total_source_bytes += result["source_bytes"]
        encrypted_count += int(result["encrypted"])
        target_row = dict(row)
        target_row["model_provider"] = target_provider
        title = str(row.get("title") or index_rows.get(source_id, {}).get("thread_name") or source_id)
        operations.append({
            "source_task_id": source_id,
            "target_task_id": target_id,
            "title": title,
            "action": action,
            "target_path": str(target_path),
            "target_relative_path": target_relative.as_posix(),
            "expected_hash": result["content_hash"],
            "staged_path": str(staged),
            "session_index_row": index_rows.get(source_id),
            "sqlite_thread_row": target_row,
        })
    return operations, total_source_bytes, encrypted_count


def clone_to_provider(
    codex_home: Path,
    source_provider: str,
    target_provider: str,
    selected_ids: set[str],
    require_codex_closed: bool = True,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    report = preflight_provider_operation(
        codex_home,
        source_provider,
        target_provider,
        selected_ids,
        operation="clone",
        create_backup=True,
        require_codex_closed=require_codex_closed,
    )
    if not report["ok"]:
        raise ProviderPreflightError(report)
    codex_home = Path(report["codex_home"])
    database = Path(report["database"])
    database_rows = migration_bundle.read_sqlite_threads(codex_home, selected_ids)
    index_rows = migration_bundle.read_session_index(codex_home)

    with tempfile.TemporaryDirectory() as temporary:
        staging_root = Path(temporary)
        operations, scanned_bytes, encrypted_count = _selected_operations(
            codex_home,
            database_rows,
            index_rows,
            source_provider,
            target_provider,
            selected_ids,
            staging_root,
        )
        descriptor, lock_path = migration_bundle.acquire_lock(codex_home)
        transaction_id = str(uuid.uuid4())
        backup_root = migration_bundle.backup_root_for(codex_home) / (
            dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + transaction_id[:8]
        )
        created_files: list[Path] = []
        backed_up: list[tuple[Path, Path]] = []
        try:
            backup_root.mkdir(parents=True, exist_ok=False)
            targets = [
                Path(operation["target_path"])
                for operation in operations
                if operation["action"] != "skip_identical"
            ]
            index_path = codex_home / "session_index.jsonl"
            targets.append(index_path)
            if not index_path.exists():
                created_files.append(index_path)
            for target in targets:
                if target.is_file():
                    destination = backup_root / "files" / target.relative_to(codex_home)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, destination)
                    backed_up.append((target, destination))
                else:
                    created_files.append(target)
            database_backup = backup_root / "database" / database.name
            migration_bundle.backup_database(database, database_backup)
            backed_up.append((database, database_backup))
            migration_bundle.atomic_write(
                backup_root / "transaction.json",
                json.dumps(migration_bundle._transaction_payload(
                    status="prepared",
                    operation="provider_clone",
                    codex_home=codex_home,
                    backup_root=backup_root,
                    backed_up=backed_up,
                    created_files=created_files,
                    bundle_id=transaction_id,
                    source_provider=source_provider,
                    target_provider=target_provider,
                ), indent=2, ensure_ascii=False).encode("utf-8"),
            )

            for operation in operations:
                if operation["action"] == "skip_identical":
                    continue
                _copy_staged_file(Path(operation["staged_path"]), Path(operation["target_path"]))
            migration_bundle.merge_session_index(codex_home, operations)
            migration_bundle.merge_sqlite(codex_home, operations)

            failures = [
                operation["target_task_id"]
                for operation in operations
                if operation["action"] != "skip_identical"
                and _hash_file(Path(operation["target_path"])) != operation["expected_hash"]
            ]
            if failures:
                raise ValueError(f"Provider clone verification failed for: {', '.join(failures)}")
            written_ids = {operation["target_task_id"] for operation in operations}
            verified_index = migration_bundle.read_session_index(codex_home)
            missing_index = sorted(written_ids - verified_index.keys())
            if missing_index:
                raise ValueError(f"Session index verification failed for: {', '.join(missing_index)}")
            verified_database = migration_bundle.read_sqlite_threads(codex_home, written_ids)
            missing_database = sorted(written_ids - verified_database.keys())
            if missing_database:
                raise ValueError(f"SQLite verification failed for: {', '.join(missing_database)}")
            wrong_provider = sorted(
                task_id for task_id, row in verified_database.items()
                if str(row.get("model_provider") or "openai") != target_provider
            )
            if wrong_provider:
                raise ValueError(f"Provider metadata verification failed for: {', '.join(wrong_provider)}")

            imported = sum(operation["action"] != "skip_identical" for operation in operations)
            migration_bundle.atomic_write(
                backup_root / "transaction.json",
                json.dumps(migration_bundle._transaction_payload(
                    status="committed",
                    operation="provider_clone",
                    codex_home=codex_home,
                    backup_root=backup_root,
                    backed_up=backed_up,
                    created_files=created_files,
                    bundle_id=transaction_id,
                    source_provider=source_provider,
                    target_provider=target_provider,
                    completed_at=migration_bundle.now_iso(),
                    imported=imported,
                ), indent=2, ensure_ascii=False).encode("utf-8"),
            )
            return {
                "bundle_id": transaction_id,
                "backup_path": str(backup_root),
                "imported": imported,
                "skipped": sum(operation["action"] == "skip_identical" for operation in operations),
                "operations": [
                    {key: operation[key] for key in ("source_task_id", "target_task_id", "title", "action")}
                    for operation in operations
                ],
                "encrypted_content_warnings": encrypted_count,
                "source_provider": source_provider,
                "target_provider": target_provider,
                "scanned_conversations": len(operations),
                "scanned_bytes": scanned_bytes,
                "duration_seconds": time.perf_counter() - started_at,
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

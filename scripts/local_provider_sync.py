#!/usr/bin/env python3
"""Discover local Codex providers, clone conversations, or reassign ownership."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import tomllib
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import content_manager
import migration_bundle
import session_merge_planner as planner


STREAM_CHUNK_BYTES = 1024 * 1024
MAX_SESSION_META_BYTES = 8 * 1024 * 1024
PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
PROVIDER_VISIBILITY_SCHEMA_VERSION = 2


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


def _managed_backup_providers(codex_home: Path) -> set[str]:
    providers: set[str] = set()
    root = migration_bundle.backup_root_for(codex_home)
    if not root.is_dir():
        return providers
    for transaction_path in root.glob("*/transaction.json"):
        try:
            transaction = json.loads(transaction_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(transaction, dict):
            continue
        for key in ("source_provider", "target_provider"):
            provider = transaction.get(key)
            if isinstance(provider, str) and PROVIDER_ID_PATTERN.fullmatch(provider):
                providers.add(provider)
    return providers


def discover_providers(codex_home: Path) -> list[dict[str, Any]]:
    codex_home = codex_home.expanduser().resolve()
    found: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "sources": set(), "rollout_count": 0, "sqlite_count": 0, "configured": False, "current": False
    })
    found["openai"]["configured"] = True
    found["openai"]["sources"].add("built-in")
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

    for provider in _managed_backup_providers(codex_home):
        found[provider]["sources"].add("managed-backup")

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


def configured_provider_ids(codex_home: Path) -> list[str]:
    """Return providers that Codex can safely select in config.toml."""
    codex_home = codex_home.expanduser().resolve()
    configured = {"openai"}
    config_path = codex_home / "config.toml"
    if config_path.is_file():
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return sorted(configured)
        providers = data.get("model_providers")
        if isinstance(providers, dict):
            configured.update(str(provider) for provider in providers)
    return sorted(configured)


def _replace_root_config_value(config_text: str, key: str, value: str) -> str:
    lines = config_text.splitlines(keepends=True)
    first_section = len(lines)
    assignment = re.compile(rf"^(\s*){re.escape(key)}\s*=.*$")
    for index, line in enumerate(lines):
        if line.strip().startswith("["):
            first_section = index
            break
        match = assignment.match(line.rstrip("\r\n"))
        if match:
            separator = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = f'{match.group(1)}{key} = "{value}"{separator}'
            return "".join(lines)

    newline = "\r\n" if "\r\n" in config_text else "\n"
    insertion = f'{key} = "{value}"{newline}'
    if first_section and lines and not lines[first_section - 1].endswith(("\n", "\r")):
        lines[first_section - 1] += newline
    lines.insert(first_section, insertion)
    return "".join(lines)


def _switched_config_text(codex_home: Path, target_provider: str) -> tuple[bytes, bytes, str | None]:
    config_path = codex_home / "config.toml"
    original = config_path.read_bytes() if config_path.is_file() else b""
    try:
        text = original.decode("utf-8-sig")
        data = tomllib.loads(text) if text.strip() else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"config.toml 无法解析：{error}") from error
    next_text = _replace_root_config_value(text, "model_provider", target_provider)
    provider_model = None
    providers = data.get("model_providers")
    if target_provider != "openai" and isinstance(providers, dict):
        section = providers.get(target_provider)
        if isinstance(section, dict) and isinstance(section.get("model"), str) and section["model"]:
            provider_model = section["model"]
            next_text = _replace_root_config_value(next_text, "model", provider_model)
    bom = b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b""
    return original, bom + next_text.encode("utf-8"), provider_model


def list_provider_threads(codex_home: Path, provider: str) -> list[dict[str, Any]]:
    codex_home = codex_home.expanduser().resolve()
    database = migration_bundle.find_state_db(codex_home)
    if database is None:
        raise ValueError("Codex thread database was not found")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("pragma table_info(threads)")}
        archived_at_column = ",archived_at" if "archived_at" in columns else ""
        rows = connection.execute(
            "select id,title,updated_at,rollout_path,model_provider,archived,agent_nickname,agent_path "
            f"{archived_at_column} "
            "from threads where coalesce(model_provider,'openai')=? order by updated_at desc",
            (provider,),
        ).fetchall()
        state = _read_provider_visibility_state(codex_home)
        managed_hidden = set(state.get("managed_hidden", []))
        manual_hidden = set(state.get("manual_hidden", []))
        task_ids = {row["id"] for row in rows}
        catalog_database = content_manager._thread_catalog_database(codex_home)
        catalog_ids = (
            content_manager._catalog_contains(catalog_database, task_ids)
            if catalog_database and task_ids
            else set()
        )
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
            task_id = thread["id"]
            archived = bool(thread.get("archived"))
            try:
                relative = path.relative_to(codex_home)
                path_archived = bool(relative.parts and relative.parts[0] == "archived_sessions")
            except ValueError:
                path_archived = False
            archive_time_consistent = (
                "archived_at" not in thread
                or (archived and thread.get("archived_at") is not None)
                or (not archived and thread.get("archived_at") is None)
            )
            archive_consistent = archived == path_archived and archive_time_consistent
            if task_id in manual_hidden:
                visibility_state = "手动隐藏"
                visibility_detail = "由本软件手动隐藏"
            elif task_id in managed_hidden:
                visibility_state = "Provider 隐藏"
                visibility_detail = "归属切换后自动隐藏"
            elif archived:
                visibility_state = "用户归档"
                visibility_detail = "Codex 原有归档状态"
            else:
                visibility_state = "正在显示"
                visibility_detail = "正常侧栏可见"
            if not archive_consistent:
                visibility_state = "状态异常"
                visibility_detail = "归档字段、归档时间和会话文件位置不一致"
            elif archived and task_id in catalog_ids:
                visibility_state = "状态异常"
                visibility_detail = "已归档但侧栏目录仍有残留"
            elif not archived and task_id not in catalog_ids and catalog_database:
                visibility_state = "等待刷新"
                visibility_detail = "启动 Codex 后等待侧栏目录重建"
            thread["visibility_state"] = visibility_state
            thread["visibility_detail"] = visibility_detail
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


def _collect_full_sync_operations(codex_home: Path, target_provider: str) -> tuple[list[dict[str, Any]], list[str]]:
    operations: list[dict[str, Any]] = []
    warnings: list[str] = []
    for directory in ("sessions", "archived_sessions"):
        root = codex_home / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            try:
                first_line = migration_bundle.read_first_line_bytes(path)
                value = json.loads(first_line.decode("utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                warnings.append(f"跳过无法读取的会话文件：{path}（{error}）")
                continue
            if not isinstance(value, dict) or value.get("type") != "session_meta":
                warnings.append(f"跳过缺少 session_meta 的 JSONL：{path}")
                continue
            payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
            task_id = planner.first_uuid(
                payload.get("id"), payload.get("thread_id"), payload.get("conversation_id"), path.name
            )
            if not task_id:
                warnings.append(f"跳过无法识别会话 ID 的 JSONL：{path}")
                continue
            current_provider = str(payload.get("model_provider") or payload.get("provider") or "openai")
            if current_provider == target_provider:
                continue
            try:
                size_bytes = path.stat().st_size
            except OSError as error:
                warnings.append(f"跳过无法读取大小的会话文件：{path}（{error}）")
                continue
            operations.append({
                "task_id": task_id,
                "path": path,
                "source_provider": current_provider,
                "original_first_line": first_line,
                "size_bytes": size_bytes,
            })
    return operations, warnings


def preflight_full_provider_sync(
    codex_home: Path,
    target_provider: str,
    *,
    update_config: bool,
    create_backup: bool,
    require_codex_closed: bool,
) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    warnings: list[str] = []
    database: Path | None = None
    config_original = b""
    config_updated = b""
    provider_model: str | None = None
    try:
        codex_home = migration_bundle.resolve_local_path(codex_home)
    except (OSError, ValueError) as error:
        return {
            "ok": False,
            "problems": [{"code": "invalid_codex_home", "message": f"Codex 数据位置无效：{error}"}],
            "warnings": [],
            "target_provider": target_provider,
        }

    if not target_provider or not PROVIDER_ID_PATTERN.fullmatch(target_provider):
        _preflight_problem(problems, "invalid_provider", "目标 Provider 只能包含字母、数字、点、下划线和连字符。")
    if require_codex_closed and migration_bundle.codex_is_running():
        _preflight_problem(problems, "codex_running", "检测到 Codex 仍在运行。请完全退出 Codex 后再执行。")
    if not codex_home.is_dir():
        _preflight_problem(problems, "missing_codex_home", "Codex 数据目录不存在。", path=codex_home)
    elif not os.access(codex_home, os.R_OK | os.W_OK):
        _preflight_problem(problems, "codex_home_permissions", "Codex 数据目录不可读写。", path=codex_home)
    lock_path = codex_home / ".cross-device-agent-sync.lock"
    if lock_path.exists():
        _preflight_problem(problems, "operation_locked", "发现正在执行或未完成的同步锁。", path=lock_path)

    if update_config and target_provider and target_provider not in configured_provider_ids(codex_home):
        _preflight_problem(
            problems,
            "provider_not_configured",
            f'Provider "{target_provider}" 未在 config.toml 中配置，不能由本软件直接切换。',
        )
    if update_config and codex_home.is_dir():
        try:
            config_original, config_updated, provider_model = _switched_config_text(codex_home, target_provider)
        except (OSError, ValueError) as error:
            _preflight_problem(problems, "config_invalid", str(error), path=codex_home / "config.toml")

    operations, rollout_warnings = _collect_full_sync_operations(codex_home, target_provider)
    warnings.extend(rollout_warnings)
    for operation in operations:
        path = operation["path"]
        if not path.is_relative_to(codex_home):
            _preflight_problem(problems, "outside_codex_home", "会话文件位于 Codex 数据目录之外。", path=path)
        elif not os.access(path, os.R_OK | os.W_OK) or not os.access(path.parent, os.W_OK):
            _preflight_problem(problems, "rollout_permissions", "会话文件或目录不可读写。", path=path)

    database = migration_bundle.find_state_db(codex_home) if codex_home.is_dir() else None
    sqlite_changed_rows = 0
    sqlite_total_rows = 0
    if database is None:
        _preflight_problem(problems, "missing_database", "未找到 Codex 状态数据库 state_*.sqlite。")
    else:
        try:
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=3)
            try:
                integrity = connection.execute("pragma quick_check").fetchone()
                columns = {row[1] for row in connection.execute("pragma table_info(threads)")}
                if {"id", "model_provider"} - columns:
                    _preflight_problem(problems, "database_schema", "SQLite threads 表缺少 Provider 字段。", path=database)
                else:
                    sqlite_total_rows = int(connection.execute("select count(*) from threads").fetchone()[0])
                    sqlite_changed_rows = int(connection.execute(
                        "select count(*) from threads where coalesce(model_provider,'openai')<>?",
                        (target_provider,),
                    ).fetchone()[0])
            finally:
                connection.close()
            if not integrity or integrity[0] != "ok":
                _preflight_problem(problems, "database_integrity", "SQLite 完整性检查未通过。", path=database)
        except sqlite3.Error as error:
            _preflight_problem(problems, "database_read", f"SQLite 无法读取：{error}", path=database)

    changed_bytes = sum(operation["size_bytes"] for operation in operations)
    largest_bytes = max((operation["size_bytes"] for operation in operations), default=0)
    database_bytes = database.stat().st_size if database is not None and database.is_file() else 0
    if create_backup:
        required_bytes = changed_bytes + database_bytes + largest_bytes + 64 * 1024 * 1024
    else:
        required_bytes = database_bytes + largest_bytes + 64 * 1024 * 1024
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
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "codex_home": str(codex_home),
        "database": str(database) if database else None,
        "target_provider": target_provider,
        "update_config": update_config,
        "provider_model": provider_model,
        "config_existed": (codex_home / "config.toml").is_file(),
        "config_original": config_original,
        "config_updated": config_updated,
        "operations": operations,
        "changed_rollout_count": len(operations),
        "changed_rollout_bytes": changed_bytes,
        "sqlite_changed_rows": sqlite_changed_rows,
        "sqlite_total_rows": sqlite_total_rows,
        "required_bytes": required_bytes,
        "free_bytes": free_bytes,
    }


def _update_all_sqlite_providers(database: Path, target_provider: str) -> int:
    connection = sqlite3.connect(database, timeout=10)
    try:
        connection.execute("begin immediate")
        cursor = connection.execute(
            "update threads set model_provider=? where coalesce(model_provider,'openai')<>?",
            (target_provider, target_provider),
        )
        connection.commit()
        return cursor.rowcount
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def sync_all_to_provider(
    codex_home: Path,
    target_provider: str,
    *,
    update_config: bool = False,
    require_codex_closed: bool = True,
    create_backup: bool = True,
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    def progress(stage: str, detail: str) -> None:
        if progress_callback is not None:
            progress_callback(stage, detail)

    started_at = time.perf_counter()
    progress("preflight", "正在检查当前配置、全部会话文件和 SQLite...")
    report = preflight_full_provider_sync(
        codex_home,
        target_provider,
        update_config=update_config,
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
        backup_root.mkdir(parents=True, exist_ok=False)
    else:
        temporary_backup = tempfile.TemporaryDirectory(prefix="provider-sync-")
        backup_root = Path(temporary_backup.name)

    descriptor, lock_path = migration_bundle.acquire_lock(codex_home)
    backed_up: list[tuple[Path, Path]] = []
    metadata_backed_up: list[tuple[Path, Path]] = []
    created_files: list[Path] = []
    rewritten_paths: list[Path] = []
    config_path = codex_home / "config.toml"
    try:
        progress("backup", "正在创建完整备份..." if create_backup else "正在创建临时回滚点...")
        database_backup = backup_root / "database" / database.name
        migration_bundle.backup_database(database, database_backup)
        backed_up.append((database, database_backup))
        if update_config:
            if report["config_existed"]:
                config_backup = backup_root / "config" / "config.toml"
                config_backup.parent.mkdir(parents=True, exist_ok=True)
                config_backup.write_bytes(report["config_original"])
                backed_up.append((config_path, config_backup))
            else:
                created_files.append(config_path)
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
                    operation="provider_switch_sync" if update_config else "provider_sync",
                    codex_home=codex_home,
                    backup_root=backup_root,
                    backed_up=backed_up,
                    created_files=created_files,
                    metadata_backed_up=metadata_backed_up,
                    bundle_id=transaction_id,
                    target_provider=target_provider,
                    update_config=update_config,
                ), indent=2, ensure_ascii=False).encode("utf-8"),
            )

        if update_config:
            progress("config", f"正在将当前 Provider 切换为 {target_provider}...")
            migration_bundle.atomic_write(config_path, report["config_updated"])
        progress("rollouts", f"正在同步 {len(operations)} 个会话文件...")
        encrypted_count = 0
        scanned_bytes = 0
        for index, operation in enumerate(operations, start=1):
            progress("rollouts", f"正在同步会话 {index}/{len(operations)}：{operation['task_id']}")
            result = _rewrite_session_provider(
                operation["path"], operation["task_id"], operation["source_provider"], target_provider
            )
            rewritten_paths.append(operation["path"])
            encrypted_count += int(result["encrypted"])
            scanned_bytes += result["source_bytes"]
        progress("database", "正在同步 SQLite 中的全部会话 Provider...")
        sqlite_rows_updated = _update_all_sqlite_providers(database, target_provider)

        progress("verify", "正在验证配置、会话文件和 SQLite 是否一致...")
        verify = preflight_full_provider_sync(
            codex_home,
            target_provider,
            update_config=False,
            create_backup=False,
            require_codex_closed=False,
        )
        if verify["changed_rollout_count"] or verify["sqlite_changed_rows"]:
            raise ValueError("Provider 全量同步验证失败，仍存在未同步的会话。")
        if update_config:
            providers = {item["id"]: item for item in discover_providers(codex_home)}
            if not providers.get(target_provider, {}).get("current"):
                raise ValueError("Provider 配置切换验证失败。")

        if create_backup:
            migration_bundle.atomic_write(
                backup_root / "transaction.json",
                json.dumps(migration_bundle._transaction_payload(
                    status="committed",
                    operation="provider_switch_sync" if update_config else "provider_sync",
                    codex_home=codex_home,
                    backup_root=backup_root,
                    backed_up=backed_up,
                    created_files=created_files,
                    metadata_backed_up=metadata_backed_up,
                    bundle_id=transaction_id,
                    target_provider=target_provider,
                    update_config=update_config,
                    completed_at=migration_bundle.now_iso(),
                    synchronized_rollouts=len(operations),
                    sqlite_rows_updated=sqlite_rows_updated,
                ), indent=2, ensure_ascii=False).encode("utf-8"),
            )
        progress("complete", "Provider 与全部历史会话已同步完成。")
        return {
            "bundle_id": transaction_id,
            "backup_path": str(backup_root) if create_backup else None,
            "backup_created": create_backup,
            "target_provider": target_provider,
            "config_updated": update_config,
            "provider_model": report["provider_model"],
            "synchronized_rollouts": len(operations),
            "sqlite_rows_updated": sqlite_rows_updated,
            "scanned_conversations": len(operations),
            "scanned_bytes": scanned_bytes,
            "encrypted_content_warnings": encrypted_count,
            "warnings": report["warnings"],
            "duration_seconds": time.perf_counter() - started_at,
        }
    except Exception:
        for target, backup in reversed(backed_up):
            if backup.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
        for created in reversed(created_files):
            created.unlink(missing_ok=True)
        if not create_backup:
            for target, backup in reversed(metadata_backed_up):
                if target in rewritten_paths and target.is_file():
                    migration_bundle.replace_first_line(target, backup.read_bytes())
        raise
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)
        if temporary_backup is not None:
            temporary_backup.cleanup()


def provider_visibility_state_path(codex_home: Path) -> Path:
    return codex_home / "cross-device-agent-sync-state" / "provider-visibility.json"


def _read_provider_visibility_state(codex_home: Path) -> dict[str, Any]:
    path = provider_visibility_state_path(codex_home)
    if not path.is_file():
        return {
            "schema_version": PROVIDER_VISIBILITY_SCHEMA_VERSION,
            "active_provider": None,
            "managed_hidden": [],
            "manual_hidden": [],
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "schema_version": PROVIDER_VISIBILITY_SCHEMA_VERSION,
            "active_provider": None,
            "managed_hidden": [],
            "manual_hidden": [],
        }
    if not isinstance(value, dict) or value.get("schema_version") not in {1, PROVIDER_VISIBILITY_SCHEMA_VERSION}:
        return {
            "schema_version": PROVIDER_VISIBILITY_SCHEMA_VERSION,
            "active_provider": None,
            "managed_hidden": [],
            "manual_hidden": [],
        }
    hidden = value.get("managed_hidden")
    manual = value.get("manual_hidden") if value.get("schema_version") == PROVIDER_VISIBILITY_SCHEMA_VERSION else []
    return {
        "schema_version": PROVIDER_VISIBILITY_SCHEMA_VERSION,
        "active_provider": value.get("active_provider") if isinstance(value.get("active_provider"), str) else None,
        "managed_hidden": sorted({item for item in hidden or [] if isinstance(item, str)}),
        "manual_hidden": sorted({item for item in manual or [] if isinstance(item, str)}),
    }


def _read_all_provider_rows(codex_home: Path) -> tuple[Path, dict[str, dict[str, Any]], bool]:
    database = migration_bundle.find_state_db(codex_home)
    if database is None:
        raise ValueError("Codex thread database was not found")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("pragma table_info(threads)")}
        required = {"id", "rollout_path", "model_provider", "archived"}
        if not required.issubset(columns):
            raise ValueError(f"Codex threads table lacks fields: {', '.join(sorted(required - columns))}")
        archive_timestamp_supported = "archived_at" in columns
        selected_columns = "id,rollout_path,model_provider,archived"
        if archive_timestamp_supported:
            selected_columns += ",archived_at"
        rows = {
            row["id"]: dict(row)
            for row in connection.execute(f"select {selected_columns} from threads")
        }
    finally:
        connection.close()
    return database, rows, archive_timestamp_supported


def plan_provider_workspace(
    codex_home: Path,
    active_provider: str,
    *,
    source_provider: str | None = None,
    target_provider: str | None = None,
    selected_ids: set[str] | None = None,
    create_backup: bool = True,
    visibility_overrides: dict[str, bool] | None = None,
    enforce_provider_isolation: bool = True,
    auto_hide_reassigned: bool = False,
) -> dict[str, Any]:
    codex_home = codex_home.expanduser().resolve()
    selected_ids = set(selected_ids or set())
    visibility_overrides = dict(visibility_overrides or {})
    problems: list[dict[str, Any]] = []
    if not PROVIDER_ID_PATTERN.fullmatch(active_provider or ""):
        _preflight_problem(problems, "invalid_active_provider", "当前侧栏 Provider 名称无效。")
    if selected_ids and not visibility_overrides:
        if not source_provider or not target_provider:
            _preflight_problem(problems, "missing_reassignment_provider", "切换会话归属需要来源和目标 Provider。")
        elif source_provider == target_provider:
            _preflight_problem(problems, "same_provider", "来源和目标 Provider 不能相同。")
        elif not PROVIDER_ID_PATTERN.fullmatch(target_provider):
            _preflight_problem(problems, "invalid_target_provider", "目标 Provider 名称无效。")
    # A reassignment with automatic sidebar handling is a workspace switch:
    # the target Provider, not the source Provider, becomes the active sidebar.
    workspace_active_provider = (
        target_provider
        if selected_ids and target_provider and auto_hide_reassigned
        else active_provider
    )
    workspace_isolation = enforce_provider_isolation or bool(
        selected_ids and target_provider and auto_hide_reassigned
    )
    try:
        database, rows, archive_timestamp_supported = _read_all_provider_rows(codex_home)
    except (OSError, sqlite3.Error, ValueError) as error:
        _preflight_problem(problems, "database_read", str(error))
        return {
            "ok": False,
            "problems": problems,
            "codex_home": str(codex_home),
            "database": None,
            "operations": [],
        }
    missing = selected_ids - set(rows)
    for task_id in sorted(missing):
        _preflight_problem(problems, "missing_thread", "所选会话已不存在，请重新扫描。", task_id=task_id)

    state = _read_provider_visibility_state(codex_home)
    old_hidden = set(state["managed_hidden"])
    old_manual_hidden = set(state.get("manual_hidden", []))
    hidden = old_hidden & set(rows)
    manual_hidden = old_manual_hidden & set(rows)
    operations: list[dict[str, Any]] = []
    state_changed_ids: set[str] = set()
    catalog_remove_ids: set[str] = set()
    desired_archived_by_id: dict[str, bool] = {}
    for task_id, row in rows.items():
        current_provider = str(row.get("model_provider") or "openai")
        resulting_provider = target_provider if target_provider and task_id in selected_ids else current_provider
        if task_id in selected_ids and source_provider and current_provider != source_provider:
            _preflight_problem(
                problems,
                "provider_drift",
                f"会话当前属于 {current_provider}，不再属于来源 {source_provider}。",
                task_id=task_id,
            )
            continue
        if task_id in visibility_overrides and visibility_overrides[task_id] and resulting_provider != workspace_active_provider:
            _preflight_problem(
                problems,
                "visibility_provider_mismatch",
                "只能显示当前 Provider 所属的会话；请先切换归属或选择对应 Provider。",
                task_id=task_id,
            )
            continue
        try:
            source = _rollout_path(codex_home, row.get("rollout_path"))
        except (OSError, ValueError) as error:
            _preflight_problem(problems, "invalid_rollout_path", str(error), task_id=task_id)
            continue
        if not source.is_file() or not source.is_relative_to(codex_home):
            _preflight_problem(problems, "missing_rollout", "会话文件不存在或路径不安全。", task_id=task_id, path=source)
            continue
        relative = source.relative_to(codex_home)
        if not relative.parts or relative.parts[0] not in {"sessions", "archived_sessions"}:
            _preflight_problem(
                problems,
                "unsupported_rollout_location",
                "会话文件不在 sessions 或 archived_sessions 中，不能自动调整归档状态。",
                task_id=task_id,
                path=source,
            )
            continue
        stored_archived = bool(row.get("archived"))
        path_archived = bool(relative.parts and relative.parts[0] == "archived_sessions")
        archived_at = row.get("archived_at") if archive_timestamp_supported else None
        archive_time_consistent = (
            not archive_timestamp_supported
            or (stored_archived and archived_at is not None)
            or (not stored_archived and archived_at is None)
        )
        archive_state_consistent = stored_archived == path_archived and archive_time_consistent
        # The threads row is authoritative. Path and archived_at are repaired to
        # match it unless the requested Provider operation changes visibility.
        currently_archived = stored_archived
        managed = task_id in hidden
        if task_id in visibility_overrides:
            desired_archived = not visibility_overrides[task_id]
            if desired_archived:
                hidden.add(task_id)
                manual_hidden.add(task_id)
            else:
                hidden.discard(task_id)
                manual_hidden.discard(task_id)
            state_changed_ids.add(task_id)
        elif auto_hide_reassigned and task_id in selected_ids and resulting_provider != workspace_active_provider:
            desired_archived = True
            hidden.add(task_id)
            manual_hidden.discard(task_id)
            state_changed_ids.add(task_id)
        elif workspace_isolation and resulting_provider == workspace_active_provider:
            desired_archived = False if managed and task_id not in manual_hidden else currently_archived
            if managed and task_id not in manual_hidden:
                hidden.discard(task_id)
                state_changed_ids.add(task_id)
        elif workspace_isolation:
            desired_archived = True
            if not currently_archived:
                hidden.add(task_id)
                manual_hidden.discard(task_id)
                state_changed_ids.add(task_id)
        else:
            desired_archived = currently_archived

        provider_changed = resulting_provider != current_provider
        visibility_changed = desired_archived != stored_archived or not archive_state_consistent
        desired_archived_by_id[task_id] = desired_archived
        if desired_archived or visibility_changed:
            catalog_remove_ids.add(task_id)
        destination = source
        if visibility_changed:
            destination = (
                codex_home / "archived_sessions" / source.name
                if desired_archived
                else content_manager._active_rollout_destination(codex_home, source)
            ).resolve()
            if not destination.is_relative_to(codex_home):
                _preflight_problem(problems, "unsafe_destination", "侧栏隔离目标路径不安全。", task_id=task_id)
                continue
            if destination != source and destination.exists():
                _preflight_problem(problems, "destination_exists", "侧栏隔离目标文件已存在。", task_id=task_id, path=destination)
                continue
        if provider_changed:
            try:
                first_line = migration_bundle.read_first_line_bytes(source)
                _provider_first_line(first_line, task_id, current_provider, resulting_provider)
            except (OSError, ValueError) as error:
                _preflight_problem(problems, "invalid_rollout_metadata", str(error), task_id=task_id, path=source)
                continue
        if provider_changed or visibility_changed:
            operations.append({
                "task_id": task_id,
                "source": source,
                "destination": destination,
                "source_provider": current_provider,
                "target_provider": resulting_provider,
                "provider_changed": provider_changed,
                "visibility_changed": visibility_changed,
                "desired_archived": desired_archived,
                "desired_archived_at": (
                    (archived_at or int(dt.datetime.now(dt.timezone.utc).timestamp()))
                    if archive_timestamp_supported and desired_archived
                    else None
                ),
                "size_bytes": source.stat().st_size,
            })

    next_state = {
        "schema_version": PROVIDER_VISIBILITY_SCHEMA_VERSION,
        "active_provider": workspace_active_provider,
        "managed_hidden": sorted(hidden),
        "manual_hidden": sorted(manual_hidden),
        "updated_at": migration_bundle.now_iso(),
    }
    state_changed = (
        state.get("active_provider") != active_provider
        or set(state.get("managed_hidden", [])) != hidden
        or set(state.get("manual_hidden", [])) != manual_hidden
    )
    archive_count = sum(1 for item in operations if item["visibility_changed"] and item["desired_archived"])
    restore_count = sum(1 for item in operations if item["visibility_changed"] and not item["desired_archived"])
    reassign_count = sum(1 for item in operations if item["provider_changed"])
    rollback_bytes = database.stat().st_size
    full_backup_bytes = sum(item["size_bytes"] for item in operations) + rollback_bytes
    catalog_database = content_manager._thread_catalog_database(codex_home)
    # The catalog can contain orphaned rows (including rows from old projects)
    # that are not present in state_5.sqlite anymore. Keep only the rows that
    # are expected to be visible in the active workspace; remove everything
    # else so empty project groups cannot survive as shell entries.
    catalog_all_ids = (
        content_manager._catalog_thread_ids(catalog_database)
        if catalog_database
        else set()
    )
    catalog_visible_ids = {
        task_id
        for task_id, row in rows.items()
        if (
            (target_provider if target_provider and task_id in selected_ids else str(row.get("model_provider") or "openai"))
            == workspace_active_provider
            and not desired_archived_by_id.get(task_id, bool(row.get("archived")))
        )
    }
    catalog_remove_ids |= catalog_all_ids - catalog_visible_ids
    catalog_cleanup_ids = (
        content_manager._catalog_contains(catalog_database, catalog_remove_ids)
        if catalog_database and catalog_remove_ids
        else set()
    )
    if catalog_database:
        rollback_bytes += catalog_database.stat().st_size
        full_backup_bytes += catalog_database.stat().st_size
    index_path = codex_home / "session_index.jsonl"
    if index_path.is_file():
        rollback_bytes += index_path.stat().st_size
        full_backup_bytes += index_path.stat().st_size
    state_path = provider_visibility_state_path(codex_home)
    if state_path.is_file():
        rollback_bytes += state_path.stat().st_size
        full_backup_bytes += state_path.stat().st_size
    required_bytes = full_backup_bytes if create_backup else rollback_bytes
    free_bytes = shutil.disk_usage(codex_home).free
    if free_bytes < required_bytes + 64 * 1024 * 1024:
        _preflight_problem(problems, "insufficient_space", "完整恢复备份所需磁盘空间不足。", path=codex_home)
    return {
        "ok": not problems,
        "problems": problems,
        "codex_home": str(codex_home),
        "database": str(database),
        "archive_timestamp_supported": archive_timestamp_supported,
        "catalog_database": str(catalog_database) if catalog_database else None,
        "index_path": str(index_path),
        "active_provider": workspace_active_provider,
        "operations": operations,
        "next_state": next_state,
        "state_changed": state_changed,
        "state_changed_ids": sorted(state_changed_ids),
        "catalog_remove_ids": sorted(catalog_remove_ids),
        "catalog_cleanup_count": len(catalog_cleanup_ids),
        "archive_count": archive_count,
        "restore_count": restore_count,
        "reassign_count": reassign_count,
        "backup_bytes": full_backup_bytes,
        "required_bytes": required_bytes,
        "free_bytes": free_bytes,
        "total_threads": len(rows),
        "visible_after": sum(
            1
            for task_id, row in rows.items()
            if (target_provider if target_provider and task_id in selected_ids else str(row.get("model_provider") or "openai")) == workspace_active_provider
            and not (bool(row.get("archived")) and task_id not in old_hidden)
        ),
        "visibility_overrides": visibility_overrides,
        "enforce_provider_isolation": workspace_isolation,
    }


def apply_provider_workspace(
    codex_home: Path,
    active_provider: str,
    *,
    source_provider: str | None = None,
    target_provider: str | None = None,
    selected_ids: set[str] | None = None,
    require_codex_closed: bool = True,
    create_backup: bool = True,
    visibility_overrides: dict[str, bool] | None = None,
    enforce_provider_isolation: bool = True,
    auto_hide_reassigned: bool = False,
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    def progress(stage: str, detail: str) -> None:
        if progress_callback:
            progress_callback(stage, detail)

    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("检测到 Codex 正在运行。请完全关闭 Codex 后再切换侧栏。")
    progress("preflight", "正在检查会话归属、归档状态和侧栏目录...")
    plan = plan_provider_workspace(
        codex_home,
        active_provider,
        source_provider=source_provider,
        target_provider=target_provider,
        selected_ids=selected_ids,
        create_backup=create_backup,
        visibility_overrides=visibility_overrides,
        enforce_provider_isolation=enforce_provider_isolation,
        auto_hide_reassigned=auto_hide_reassigned,
    )
    if not plan["ok"]:
        raise ProviderPreflightError(plan)
    codex_home = Path(plan["codex_home"])
    database = Path(plan["database"])
    catalog_database = Path(plan["catalog_database"]) if plan["catalog_database"] else None
    index_path = Path(plan["index_path"])
    state_path = provider_visibility_state_path(codex_home)
    operations = plan["operations"]
    if not operations and not plan["state_changed"] and not plan["catalog_cleanup_count"]:
        return {
            **plan,
            "backup_path": None,
            "backup_created": False,
            "changed": 0,
            "catalog_rebuild_required": False,
        }

    backup_paths = [database]
    if index_path.is_file():
        backup_paths.append(index_path)
    if catalog_database:
        backup_paths.append(catalog_database)
    if state_path.is_file():
        backup_paths.append(state_path)
    backup_paths = list(dict.fromkeys(backup_paths))
    temporary_backup = None
    metadata_backed_up: list[tuple[Path, Path]] = []
    if create_backup:
        backup_paths.extend(item["source"] for item in operations)
        backup_paths = list(dict.fromkeys(backup_paths))
        progress("backup", "正在完整备份会话、SQLite、侧栏目录和隔离状态...")
        backup_root, backed_up = content_manager._backup_selected_files(
            codex_home, backup_paths, "provider-workspace"
        )
    else:
        progress("backup", "正在创建失败时使用的临时回滚点...")
        temporary_backup = tempfile.TemporaryDirectory(prefix="provider-workspace-")
        backup_root = Path(temporary_backup.name)
        backed_up = []
        for path in backup_paths:
            if not path.is_file():
                continue
            destination = backup_root / "files" / path.relative_to(codex_home)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".sqlite":
                migration_bundle.backup_database(path, destination)
            else:
                shutil.copy2(path, destination)
            backed_up.append((path, destination))
        for item in operations:
            if not item["provider_changed"]:
                continue
            metadata_backup = backup_root / "metadata" / f"{item['task_id']}.first-line"
            metadata_backup.parent.mkdir(parents=True, exist_ok=True)
            metadata_backup.write_bytes(migration_bundle.read_first_line_bytes(item["source"]))
            metadata_backed_up.append((item["source"], metadata_backup))
    created_files = [
        item["destination"] for item in operations if item["destination"] != item["source"]
    ]
    if not state_path.is_file():
        created_files.append(state_path)
    descriptor, lock_path = migration_bundle.acquire_lock(codex_home)
    moved: dict[str, Path] = {}
    catalog_ids = set(plan["catalog_remove_ids"])
    try:
        if create_backup:
            content_manager._write_transaction(
                codex_home,
                backup_root,
                backed_up,
                "provider_workspace",
                "in_progress",
                created_files=created_files,
                active_provider=plan["active_provider"],
                task_ids=[item["task_id"] for item in operations],
            )
        progress("rollouts", "正在更新会话归属并调整侧栏可见性...")
        for item in operations:
            if item["provider_changed"]:
                _rewrite_session_provider(
                    item["source"], item["task_id"], item["source_provider"], item["target_provider"]
                )
            if item["destination"] != item["source"]:
                item["destination"].parent.mkdir(parents=True, exist_ok=True)
                os.replace(item["source"], item["destination"])
            if item["visibility_changed"]:
                moved[item["task_id"]] = item["destination"]
        if moved:
            content_manager._update_index_rollout_paths(codex_home, moved)
        if catalog_database and catalog_ids:
            content_manager._remove_catalog_rows(catalog_database, catalog_ids)

        progress("database", "正在更新 SQLite 会话归属和归档状态...")
        connection = sqlite3.connect(database, timeout=10)
        try:
            connection.execute("begin immediate")
            for item in operations:
                if plan["archive_timestamp_supported"]:
                    connection.execute(
                        "update threads set model_provider=?,rollout_path=?,archived=?,archived_at=? where id=?",
                        (
                            item["target_provider"],
                            str(item["destination"]),
                            int(item["desired_archived"]),
                            item["desired_archived_at"],
                            item["task_id"],
                        ),
                    )
                else:
                    connection.execute(
                        "update threads set model_provider=?,rollout_path=?,archived=? where id=?",
                        (
                            item["target_provider"],
                            str(item["destination"]),
                            int(item["desired_archived"]),
                            item["task_id"],
                        ),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        migration_bundle.atomic_write(
            state_path,
            (json.dumps(plan["next_state"], indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )

        progress("verify", "正在验证当前 Provider 侧栏只保留所属会话...")
        _, verified, _ = _read_all_provider_rows(codex_home)
        for item in operations:
            row = verified.get(item["task_id"])
            if (
                row is None
                or str(row.get("model_provider") or "openai") != item["target_provider"]
                or bool(row.get("archived")) != item["desired_archived"]
                or (
                    plan["archive_timestamp_supported"]
                    and ((row.get("archived_at") is not None) != item["desired_archived"])
                )
                or _rollout_path(codex_home, row.get("rollout_path")) != item["destination"]
                or not item["destination"].is_file()
            ):
                raise ValueError(f"Provider 侧栏隔离验证失败：{item['task_id']}")
            if item["provider_changed"]:
                first_line = migration_bundle.read_first_line_bytes(item["destination"])
                _provider_first_line(
                    first_line, item["task_id"], item["target_provider"], item["target_provider"]
                )
        if catalog_database and catalog_ids and content_manager._catalog_contains(catalog_database, catalog_ids):
            raise ValueError("Provider 侧栏目录清理验证失败。")
        if create_backup:
            content_manager._write_transaction(
                codex_home,
                backup_root,
                backed_up,
                "provider_workspace",
                "committed",
                created_files=created_files,
                active_provider=plan["active_provider"],
                task_ids=[item["task_id"] for item in operations],
                archived_for_other_providers=plan["archive_count"],
                restored_for_active_provider=plan["restore_count"],
                reassigned=plan["reassign_count"],
            )
        progress("complete", "Provider 归属与侧栏隔离已完成。")
        return {
            **plan,
            "backup_path": str(backup_root) if create_backup else None,
            "backup_created": create_backup,
            "changed": len(operations),
            "catalog_rebuild_required": bool(catalog_database and plan["restore_count"]),
        }
    except Exception:
        if create_backup:
            for path in reversed(created_files):
                path.unlink(missing_ok=True)
        else:
            for item in reversed(operations):
                if item["destination"] != item["source"] and item["destination"].is_file():
                    item["source"].parent.mkdir(parents=True, exist_ok=True)
                    os.replace(item["destination"], item["source"])
            for target, backup in reversed(metadata_backed_up):
                if target.is_file():
                    migration_bundle.replace_first_line(target, backup.read_bytes())
            if state_path in created_files:
                state_path.unlink(missing_ok=True)
        for target, backup in reversed(backed_up):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
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

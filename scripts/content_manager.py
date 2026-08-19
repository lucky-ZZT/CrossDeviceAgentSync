#!/usr/bin/env python3
"""Read-only content inventory and recoverable bulk management operations."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from collections import deque
from pathlib import Path, PureWindowsPath
from typing import Any, Iterator

import migration_bundle


REMOVE = object()
UUID_TITLE_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)
DATA_IMAGE_RE = re.compile(r"data:image/[^\"\\]*?;base64,[A-Za-z0-9+/=]+")
INJECTED_TEXT_PREFIXES = (
    "<recommended_plugins>",
    "<app-context>",
    "<skills_instructions>",
    "<environment_context>",
    "# agents.md instructions",
)
MAX_PREVIEW_JSON_LINE = 4 * 1024 * 1024
CODEX_TEMP_WORKSPACE_RE = re.compile(r"^new-chat(?:-\d+)?$", re.IGNORECASE)
DATE_DIRECTORY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
THREAD_CATALOG_RELATIVE_PATH = Path("sqlite") / "codex-dev.db"
THREAD_CATALOG_REQUIRED_COLUMNS = {
    "host_id",
    "thread_id",
    "display_title",
    "cwd",
    "source_detail",
    "missing_candidate",
}


def _now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _data_url_parts(value: str) -> tuple[str, str] | None:
    if not value.startswith("data:image/") or ";base64," not in value:
        return None
    header, encoded = value.split(",", 1)
    mime = header[5:].split(";", 1)[0]
    return mime, encoded


def _node_image(node: Any) -> tuple[str, bytes, str] | None:
    if not isinstance(node, dict):
        return None
    for key in ("image_url", "url"):
        value = node.get(key)
        if isinstance(value, str):
            parts = _data_url_parts(value)
            if parts:
                mime, encoded = parts
                try:
                    kind = "browser_screenshot" if key == "url" and ("tabId" in node or "pageUrl" in node) else "user_image"
                    return mime, base64.b64decode(encoded, validate=True), kind
                except (ValueError, base64.binascii.Error):
                    return None
    if node.get("type") == "image" and isinstance(node.get("data"), str):
        mime_type = str(node.get("mimeType") or "image/png")
        if mime_type.startswith("image/"):
            try:
                return mime_type, base64.b64decode(node["data"], validate=True), "tool_image"
            except (ValueError, base64.binascii.Error):
                return None
    return None


def _walk_images(node: Any) -> Iterator[tuple[str, bytes, str]]:
    image = _node_image(node)
    if image:
        yield image
        return
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk_images(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_images(value)


def scan_rollout_images(path: Path) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    line_count = 0
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, start=1):
            line_count = line_number
            if "data:image/" not in line and '"type":"image"' not in line and '"type": "image"' not in line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            for mime, data, kind in _walk_images(value):
                digest = hashlib.sha256(data).hexdigest()
                record = found.setdefault(
                    digest,
                    {
                        "digest": digest,
                        "mime_type": mime,
                        "size_bytes": len(data),
                        "occurrences": 0,
                        "first_line": line_number,
                        "last_line": line_number,
                        "kinds": set(),
                    },
                )
                record["occurrences"] += 1
                record["last_line"] = line_number
                record["kinds"].add(kind)
    for record in found.values():
        record["kinds"] = sorted(record["kinds"])
        record["has_later_events"] = record["last_line"] < line_count
    return sorted(found.values(), key=lambda item: (-item["size_bytes"], item["digest"]))


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\x00", " ")).strip()


def display_project_path(value: str) -> str:
    """Remove Windows device-path syntax for display and project grouping only."""
    path = value.strip()
    if path.casefold().startswith("\\\\?\\unc\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return path.rstrip("\\/") or path


def project_name_from_path(value: str) -> str:
    path = display_project_path(value)
    if not path:
        return ""
    if "\\" in path or re.match(r"^[A-Za-z]:", path):
        return PureWindowsPath(path).name or path
    return Path(path).name or path


def project_identity_from_cwd(value: str) -> tuple[str, str]:
    """Return an actual project identity, excluding Codex's generic conversation workspaces."""
    path = display_project_path(value)
    if not path:
        return "", ""
    if "\\" in path or re.match(r"^[A-Za-z]:", path):
        pure = PureWindowsPath(path)
        parts = pure.parts
        if (
            CODEX_TEMP_WORKSPACE_RE.fullmatch(pure.name)
            and len(parts) >= 3
            and DATE_DIRECTORY_RE.fullmatch(pure.parent.name)
            and pure.parent.parent.name.casefold() == "codex"
        ):
            return "", ""
    resolved = Path(path).expanduser()
    broad_roots = {
        Path.home(),
        Path.home() / "Documents",
        Path.home() / "Desktop",
    }
    try:
        if resolved.resolve() in {root.resolve() for root in broad_roots}:
            return "", ""
    except OSError:
        pass
    return path, project_name_from_path(path)


def _mojibake_score(value: str) -> int:
    markers = ("Ã", "Â", "â€", "锟", "�")
    score = sum(value.count(marker) * 4 for marker in markers)
    score += sum(1 for character in value if "\u00c0" <= character <= "\u00ff")
    return score


def repair_mojibake(value: str) -> str:
    """Repair only reversible UTF-8 text that was decoded as Latin-1 or CP1252."""
    raw = value.replace("\x00", " ").strip()
    original = _normalize_text(raw)
    best = original
    best_score = _mojibake_score(raw)
    if best_score == 0:
        return original
    for encoding in ("latin-1", "cp1252"):
        try:
            candidate = _normalize_text(raw.encode(encoding).decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        candidate_score = _mojibake_score(candidate)
        if candidate and candidate_score < best_score:
            best, best_score = candidate, candidate_score
    return best


def _is_injected_text(value: str) -> bool:
    lowered = value.lstrip().casefold()
    return any(lowered.startswith(prefix) for prefix in INJECTED_TEXT_PREFIXES)


def _preview_content_parts(node: Any) -> list[str]:
    if isinstance(node, str):
        if node.startswith("data:image/"):
            return ["[图片]"]
        return [] if _is_injected_text(node) else [node]
    if isinstance(node, list):
        return [part for item in node for part in _preview_content_parts(item)]
    if not isinstance(node, dict):
        return []
    node_type = str(node.get("type") or "").casefold()
    if node_type in {"input_image", "image", "image_url"} or (
        isinstance(node.get("image_url"), str) and node["image_url"].startswith("data:image/")
    ):
        return ["[图片]"]
    if node_type in {"input_text", "output_text", "text"} and isinstance(node.get("text"), str):
        return _preview_content_parts(node["text"])
    if "content" in node:
        return _preview_content_parts(node["content"])
    if isinstance(node.get("text"), str):
        return _preview_content_parts(node["text"])
    return []


def _message_text(node: Any, limit: int) -> str:
    parts = []
    for part in _preview_content_parts(node):
        normalized = _normalize_text(DATA_IMAGE_RE.sub("[图片]", part))
        if normalized:
            parts.append(normalized)
    text = " ".join(parts)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _event_messages(value: Any, text_limit: int) -> list[tuple[str, str]]:
    if not isinstance(value, dict):
        return []
    event_type = str(value.get("type") or "")
    payload = value.get("payload") if isinstance(value.get("payload"), dict) else value
    role = str(payload.get("role") or "").casefold()
    if event_type == "response_item" and role in {"user", "assistant"}:
        text = _message_text(payload.get("content") or payload.get("text"), text_limit)
        return [(role, text)] if text else []
    payload_type = str(payload.get("type") or "").casefold()
    if event_type == "event_msg" and payload_type in {"user_message", "agent_message"}:
        role = "user" if payload_type == "user_message" else "assistant"
        text = _message_text(payload.get("message"), text_limit)
        if payload.get("images"):
            text = f"{text} [图片]".strip()
        return [(role, text)] if text else []
    if event_type == "response_item" and payload_type in {
        "function_call", "custom_tool_call", "local_shell_call", "web_search_call", "computer_tool_call"
    }:
        return [("tool", "[工具调用]")]
    return []


def _safe_json_line(line: str) -> Any | None:
    if "data:image/" in line:
        line = DATA_IMAGE_RE.sub("[图片]", line)
    if len(line) > MAX_PREVIEW_JSON_LINE:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _title_from_user_message(value: str) -> str:
    delegation = re.search(r"<input>(.*?)(?:</input>|$)", value, flags=re.IGNORECASE | re.DOTALL)
    if delegation:
        value = delegation.group(1)
    match = re.search(r"##\s*My request:\s*(.+)", value, flags=re.IGNORECASE | re.DOTALL)
    if match:
        value = match.group(1)
    value = re.sub(r"^#\s*Files mentioned by the user:.*?(?=##\s*My request:|$)", "", value, flags=re.DOTALL)
    work_in = re.match(r"^Work in\s+.+?\.\s+(.+)$", value, flags=re.IGNORECASE | re.DOTALL)
    if work_in:
        value = work_in.group(1)
    return _normalize_text(value)[:120]


def scan_rollout_identity(path: Path, max_lines: int = 5000) -> dict[str, str | None]:
    session_title = None
    first_user_message = None
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, start=1):
            if line_number > max_lines:
                break
            value = _safe_json_line(line)
            if not isinstance(value, dict):
                continue
            payload = value.get("payload") if isinstance(value.get("payload"), dict) else value
            if value.get("type") == "session_meta" and session_title is None:
                for key in ("thread_name", "name", "title"):
                    candidate = payload.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        session_title = _normalize_text(candidate)
                        break
            for role, text in _event_messages(value, 1200):
                if role == "user" and first_user_message is None:
                    candidate = _title_from_user_message(text)
                    if candidate and not _is_injected_text(candidate):
                        first_user_message = candidate
                        break
            if first_user_message is not None and (session_title is not None or line_number >= 20):
                break
    return {"session_title": session_title, "first_user_message": first_user_message}


def _registered_title(value: str | None, source: str) -> tuple[str, str] | None:
    """Return a stored title without semantic rewriting, repairing only reversible mojibake."""
    raw = (value or "").replace("\x00", " ").strip()
    if not raw:
        return None
    repaired = repair_mojibake(raw)
    raw_score = _mojibake_score(raw)
    if repaired != raw and _mojibake_score(repaired) < raw_score:
        return repaired, f"{source}（可逆乱码修复）"
    if raw_score >= 4:
        return raw, f"{source}（疑似乱码，未改动）"
    return raw, source


def resolve_registered_display_title(
    catalog_title: str | None,
    database_title: str,
    session_title: str | None,
    first_user_message: str | None,
    cwd: str,
    task_id: str,
) -> tuple[str, str]:
    """Prefer stored Codex titles and never infer a formal title from conversation content."""
    del first_user_message, cwd
    for value, source in (
        (catalog_title, "Codex 侧栏名称"),
        (database_title, "主数据库标题"),
        (session_title, "会话元数据标题"),
    ):
        resolved = _registered_title(value, source)
        if resolved:
            return resolved
    return task_id, "任务 ID"


def preview_conversation(path: Path, max_messages: int = 10, text_limit: int = 1600) -> dict[str, Any]:
    """Stream a read-only, compact user/assistant transcript from a rollout file."""
    recent: deque[dict[str, str]] = deque(maxlen=max_messages)
    session_title = None
    first_user_message = None
    message_count = 0
    tool_count = 0
    image_occurrences = 0
    skipped_large_lines = 0
    last_message: tuple[str, str] | None = None
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            image_occurrences += line.count("data:image/")
            value = _safe_json_line(line)
            if value is None:
                if len(line) > MAX_PREVIEW_JSON_LINE:
                    skipped_large_lines += 1
                continue
            if isinstance(value, dict) and value.get("type") == "session_meta" and session_title is None:
                payload = value.get("payload") if isinstance(value.get("payload"), dict) else value
                for key in ("thread_name", "name", "title"):
                    candidate = payload.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        session_title = _normalize_text(candidate)
                        break
            for role, text in _event_messages(value, text_limit):
                if role == "tool":
                    tool_count += 1
                    if not recent or recent[-1]["role"] != "tool":
                        recent.append({"role": "tool", "text": text})
                    continue
                signature = (role, text)
                if signature == last_message:
                    continue
                last_message = signature
                message_count += 1
                if role == "user" and first_user_message is None:
                    first_user_message = _title_from_user_message(text)
                recent.append({"role": role, "text": text})
    return {
        "session_title": session_title,
        "first_user_message": first_user_message,
        "message_count": message_count,
        "tool_call_count": tool_count,
        "image_occurrences": image_occurrences,
        "skipped_large_lines": skipped_large_lines,
        "messages": list(recent),
    }


def _read_thread_rows(codex_home: Path) -> dict[str, dict[str, Any]]:
    database = migration_bundle.find_state_db(codex_home)
    if database is None:
        return {}
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return {row["id"]: dict(row) for row in connection.execute("select * from threads")}
    finally:
        connection.close()


def _thread_catalog_database(codex_home: Path) -> Path | None:
    database = codex_home / THREAD_CATALOG_RELATIVE_PATH
    if not database.is_file():
        return None
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        table = connection.execute(
            "select 1 from sqlite_master where type='table' and name='local_thread_catalog'"
        ).fetchone()
        if table is None:
            raise ValueError("Codex sidebar catalog has an unsupported schema")
        columns = {row[1] for row in connection.execute("pragma table_info(local_thread_catalog)")}
        if not THREAD_CATALOG_REQUIRED_COLUMNS.issubset(columns):
            raise ValueError("Codex sidebar catalog has an unsupported schema")
    finally:
        connection.close()
    return database


def _read_thread_catalog(codex_home: Path) -> tuple[Path | None, dict[str, dict[str, Any]]]:
    database = _thread_catalog_database(codex_home)
    if database is None:
        return None, {}
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "select * from local_thread_catalog where host_id='local' and missing_candidate=0"
        )
        return database, {row["thread_id"]: dict(row) for row in rows}
    finally:
        connection.close()


def _rollout_ids(codex_home: Path) -> set[str]:
    found = set()
    for root_name in ("sessions", "archived_sessions"):
        root = codex_home / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            match = UUID_TITLE_RE.search(path.name)
            if match:
                found.add(match.group(0).lower())
    return found


def _consistency_inventory(
    codex_home: Path,
    database_rows: dict[str, dict[str, Any]],
    usable_ids: set[str],
    missing_file_ids: set[str],
) -> dict[str, Any]:
    catalog_database, catalog_rows = _read_thread_catalog(codex_home)
    catalog_ids = set(catalog_rows)
    database_ids = set(database_rows)
    archived_ids = {task_id for task_id, row in database_rows.items() if bool(row.get("archived"))}
    index_rows = migration_bundle.read_session_index(codex_home)
    index_ids = set(index_rows)
    rollout_ids = _rollout_ids(codex_home)

    stale_catalog_ids = sorted((catalog_ids - database_ids) | (catalog_ids & archived_ids))
    state_only_ids = sorted((usable_ids - archived_ids) - catalog_ids) if catalog_database else []
    index_only_ids = sorted(index_ids - database_ids)
    orphan_rollout_ids = sorted(rollout_ids - database_ids)
    stale_catalog = []
    for task_id in stale_catalog_ids:
        title = repair_mojibake(str(catalog_rows[task_id].get("display_title") or task_id))
        if len(title) > 180:
            title = title[:179].rstrip() + "…"
        source_detail = display_project_path(str(catalog_rows[task_id].get("source_detail") or ""))
        stale_catalog.append({
            "task_id": task_id,
            "title": title,
            "project_path": project_identity_from_cwd(
                str(catalog_rows[task_id].get("cwd") or "")
            )[0],
            "source_detail": source_detail,
            "archived": task_id in archived_ids
            or "archived_sessions" in source_detail.replace("\\", "/").casefold(),
        })
    return {
        "catalog_available": catalog_database is not None,
        "catalog_path": str(catalog_database) if catalog_database else None,
        "catalog_visible": len(catalog_ids),
        "stale_catalog": stale_catalog,
        "stale_catalog_ids": stale_catalog_ids,
        "state_only_ids": state_only_ids,
        "index_only_ids": index_only_ids,
        "orphan_rollout_ids": orphan_rollout_ids,
        "missing_file_ids": sorted(missing_file_ids),
    }


def scan_content(codex_home: Path) -> dict[str, Any]:
    codex_home = codex_home.expanduser().resolve()
    database_rows = _read_thread_rows(codex_home)
    _catalog_database, catalog_rows = _read_thread_catalog(codex_home)
    conversations = []
    images = []
    projects: dict[str, dict[str, Any]] = {}
    missing_files = 0
    missing_file_ids: set[str] = set()
    for task_id, row in database_rows.items():
        try:
            rollout_path = migration_bundle.resolve_local_path(row.get("rollout_path"), codex_home)
        except (TypeError, ValueError):
            missing_files += 1
            missing_file_ids.add(task_id)
            continue
        if not rollout_path.is_file() or not rollout_path.is_relative_to(codex_home):
            missing_files += 1
            missing_file_ids.add(task_id)
            continue
        stat = rollout_path.stat()
        updated_value = row.get("updated_at")
        try:
            updated_at = dt.datetime.fromtimestamp(int(updated_value), tz=dt.timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            updated_at = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).isoformat()
        image_rows = scan_rollout_images(rollout_path)
        image_bytes = sum(image["size_bytes"] * image["occurrences"] for image in image_rows)
        unique_image_bytes = sum(image["size_bytes"] for image in image_rows)
        cwd_value = str(row.get("cwd") or "").strip()
        project_path, project_name = project_identity_from_cwd(cwd_value)
        provider = str(row.get("model_provider") or "openai")
        original_title = str(row.get("title") or "")
        catalog_title = str(catalog_rows.get(task_id, {}).get("display_title") or "")
        identity = scan_rollout_identity(rollout_path)
        title, title_source = resolve_registered_display_title(
            catalog_title,
            original_title,
            identity["session_title"],
            identity["first_user_message"],
            cwd_value,
            task_id,
        )
        relative_path = rollout_path.relative_to(codex_home).as_posix()
        database_archived = bool(row.get("archived"))
        archived_at_supported = "archived_at" in row
        archived_at = row.get("archived_at") if archived_at_supported else None
        path_archived = relative_path.startswith("archived_sessions/")
        archive_time_consistent = (
            not archived_at_supported
            or (database_archived and archived_at is not None)
            or (not database_archived and archived_at is None)
        )
        archive_consistent = database_archived == path_archived and archive_time_consistent
        archived = database_archived or path_archived
        if archive_consistent:
            archive_state = "已归档" if archived else "使用中"
        elif database_archived and not path_archived:
            archive_state = "归档状态异常：数据库已归档，文件仍在使用区"
        elif database_archived and archived_at_supported and archived_at is None:
            archive_state = "归档状态异常：数据库已归档，但缺少归档时间"
        elif not database_archived and archived_at_supported and archived_at is not None:
            archive_state = "归档状态异常：数据库未归档，但仍保留归档时间"
        else:
            archive_state = "归档状态异常：文件在归档区，数据库未标记归档"
        conversation = {
            "task_id": task_id,
            "title": title,
            "original_title": original_title,
            "catalog_title": catalog_title,
            "title_source": title_source,
            "provider": provider,
            "cwd": cwd_value,
            "project_path": project_path,
            "project_name": project_name,
            "rollout_path": str(rollout_path),
            "relative_path": relative_path,
            "updated_at": updated_at,
            "archived": archived,
            "database_archived": database_archived,
            "archived_at": archived_at,
            "archived_at_supported": archived_at_supported,
            "path_archived": path_archived,
            "archive_consistent": archive_consistent,
            "archive_state": archive_state,
            "size_bytes": stat.st_size,
            "image_count": len(image_rows),
            "image_occurrences": sum(image["occurrences"] for image in image_rows),
            "image_bytes": image_bytes,
            "unique_image_bytes": unique_image_bytes,
        }
        conversations.append(conversation)
        for image in image_rows:
            kinds = set(image.get("kinds", []))
            if "user_image" in kinds:
                risk_level, risk_reason = "高", "用户提供的图片，可能是后续任务依据"
            elif image["occurrences"] > 1 and "browser_screenshot" in kinds and "user_image" not in kinds:
                risk_level, risk_reason = "低", "浏览器截图有重复副本，默认只清理重复项并保留一份"
            elif image["occurrences"] > 1:
                risk_level, risk_reason = "中", "图片有重复副本，但仍可能被后续上下文使用"
            else:
                risk_level, risk_reason = "高", "唯一图片，无法证明删除后不影响后续上下文"
            if image["has_later_events"]:
                risk_reason += "；图片后仍有后续事件"
            images.append({
                **image,
                "task_id": task_id,
                "title": title,
                "rollout_path": str(rollout_path),
                "updated_at": updated_at,
                "stored_bytes": image["size_bytes"] * image["occurrences"],
                "archived": archived,
                "database_archived": database_archived,
                "path_archived": path_archived,
                "archive_consistent": archive_consistent,
                "risk_level": risk_level,
                "risk_reason": risk_reason,
                "safe_to_clean": risk_level == "低",
            })
        if project_path:
            project = projects.setdefault(
                project_path,
                {
                    "path": project_path,
                    "thread_ids": [],
                    "thread_count": 0,
                    "conversation_bytes": 0,
                    "image_bytes": 0,
                    "latest_updated_at": "",
                    "exists": Path(project_path).expanduser().is_dir(),
                },
            )
            project["thread_ids"].append(task_id)
            project["thread_count"] += 1
            project["conversation_bytes"] += stat.st_size
            project["image_bytes"] += image_bytes
            project["latest_updated_at"] = max(project["latest_updated_at"], updated_at)
    conversations.sort(key=lambda item: str(item["updated_at"]), reverse=True)
    conversation_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in conversations:
        normalized_title = item["title"].replace(" [migrated branch]", "").strip().casefold()
        conversation_groups.setdefault((normalized_title, item["project_path"].casefold()), []).append(item)
    for group in conversation_groups.values():
        for item in group:
            item["possible_duplicates"] = len(group) if len(group) > 1 else 0
    project_values = list(projects.values())
    project_groups: dict[str, list[dict[str, Any]]] = {}
    for item in project_values:
        name = Path(item["path"]).name.casefold()
        base = re.sub(r"-from-old-computer(?:-\d+)?$", "", name)
        project_groups.setdefault(base, []).append(item)
    for group in project_groups.values():
        for item in group:
            item["possible_duplicates"] = len(group) if len(group) > 1 else 0
    images.sort(key=lambda item: (-item["stored_bytes"], item["title"], item["digest"]))
    consistency = _consistency_inventory(
        codex_home,
        database_rows,
        {item["task_id"] for item in conversations},
        missing_file_ids,
    )
    return {
        "codex_home": str(codex_home),
        "conversations": conversations,
        "projects": sorted(project_values, key=lambda item: item["latest_updated_at"], reverse=True),
        "images": images,
        "consistency": consistency,
        "summary": {
            "conversations": len(conversations),
            "projects": len(projects),
            "unique_images": len(images),
            "image_occurrences": sum(item["occurrences"] for item in images),
            "image_bytes": sum(item["stored_bytes"] for item in images),
            "missing_files": missing_files,
            "catalog_visible": consistency["catalog_visible"],
            "stale_catalog": len(consistency["stale_catalog_ids"]),
            "state_only": len(consistency["state_only_ids"]),
            "index_only": len(consistency["index_only_ids"]),
            "orphan_rollouts": len(consistency["orphan_rollout_ids"]),
        },
    }


def extract_image(rollout_path: Path, digest: str, output_directory: Path) -> Path:
    with rollout_path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if "data:image/" not in line and '"type":"image"' not in line and '"type": "image"' not in line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            for mime, data, _kind in _walk_images(value):
                if hashlib.sha256(data).hexdigest() == digest:
                    subtype = mime.split("/", 1)[-1]
                    suffix = {"jpeg": ".jpg", "svg+xml": ".svg"}.get(subtype, f".{subtype}")
                    output_directory.mkdir(parents=True, exist_ok=True)
                    output = output_directory / f"{digest}{suffix}"
                    if not output.is_file():
                        output.write_bytes(data)
                    return output
    raise ValueError("The selected image no longer exists in this conversation")


def _transform_images(
    node: Any,
    selected: set[str],
    keep_one: bool = False,
    kept: set[str] | None = None,
) -> tuple[Any, int, int]:
    kept = kept if kept is not None else set()
    image = _node_image(node)
    if image:
        _mime, data, _kind = image
        digest = hashlib.sha256(data).hexdigest()
        if digest in selected:
            if keep_one and digest not in kept:
                kept.add(digest)
                return node, 0, 0
            return REMOVE, 1, len(data)
        return node, 0, 0
    removed = 0
    removed_bytes = 0
    if isinstance(node, list):
        result = []
        for value in node:
            transformed, count, size = _transform_images(value, selected, keep_one, kept)
            removed += count
            removed_bytes += size
            if transformed is not REMOVE:
                result.append(transformed)
        return result, removed, removed_bytes
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            transformed, count, size = _transform_images(value, selected, keep_one, kept)
            removed += count
            removed_bytes += size
            if transformed is not REMOVE:
                result[key] = transformed
        return result, removed, removed_bytes
    return node, 0, 0


def _backup_selected_files(codex_home: Path, paths: list[Path], operation: str) -> tuple[Path, list[tuple[Path, Path]]]:
    backup_root = migration_bundle.backup_root_for(codex_home) / f"{_now_stamp()}-{operation}"
    required = sum(path.stat().st_size for path in paths if path.is_file())
    free = shutil.disk_usage(migration_bundle.backup_root_for(codex_home).parent).free
    if free < required + 64 * 1024 * 1024:
        raise ValueError("Not enough free disk space for the required recovery backup")
    backup_root.mkdir(parents=True, exist_ok=False)
    backed_up = []
    for path in paths:
        if not path.is_file():
            continue
        destination = backup_root / "files" / path.relative_to(codex_home)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".sqlite":
            migration_bundle.backup_database(path, destination)
        else:
            shutil.copy2(path, destination)
        backed_up.append((path, destination))
    return backup_root, backed_up


def _write_transaction(
    codex_home: Path,
    backup_root: Path,
    backed_up: list[tuple[Path, Path]],
    operation: str,
    status: str,
    created_files: list[Path] | None = None,
    **extra: Any,
) -> None:
    payload = migration_bundle._transaction_payload(
        status=status,
        operation=operation,
        codex_home=codex_home,
        backup_root=backup_root,
        backed_up=backed_up,
        created_files=created_files or [],
        **extra,
    )
    if status == "committed":
        payload["completed_at"] = migration_bundle.now_iso()
    migration_bundle.atomic_write(
        backup_root / "transaction.json",
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def clean_images(
    codex_home: Path,
    selections: dict[str, set[str]],
    require_codex_closed: bool = True,
    keep_one: bool = False,
) -> dict[str, Any]:
    if not selections:
        raise ValueError("No images were selected")
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before cleaning images")
    codex_home = codex_home.expanduser().resolve()
    rows = _read_thread_rows(codex_home)
    paths = []
    for task_id in selections:
        row = rows.get(task_id)
        if row is None:
            raise ValueError(f"Conversation is no longer registered: {task_id}")
        path = migration_bundle.resolve_local_path(row["rollout_path"], codex_home)
        if not path.is_file() or not path.is_relative_to(codex_home):
            raise ValueError(f"Conversation file is missing or unsafe: {task_id}")
        paths.append(path)
    backup_root, backed_up = _backup_selected_files(codex_home, paths, "image-cleanup")
    descriptor, lock_path = migration_bundle.acquire_lock(codex_home)
    removed = 0
    removed_bytes = 0
    try:
        _write_transaction(codex_home, backup_root, backed_up, "image_cleanup", "in_progress")
        for task_id, path in zip(selections, paths):
            temporary = path.with_name(f".{path.name}.{os.getpid()}.image-cleanup.tmp")
            kept: set[str] = set()
            try:
                with path.open("r", encoding="utf-8", errors="strict") as source, temporary.open("w", encoding="utf-8", newline="\n") as target:
                    for line in source:
                        if "data:image/" not in line and '"type":"image"' not in line and '"type": "image"' not in line:
                            target.write(line)
                            continue
                        value = json.loads(line)
                        transformed, count, size = _transform_images(
                            value, selections[task_id], keep_one=keep_one, kept=kept
                        )
                        removed += count
                        removed_bytes += size
                        if transformed is not REMOVE:
                            target.write(json.dumps(transformed, ensure_ascii=False, separators=(",", ":")) + "\n")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        if removed == 0:
            raise ValueError("Selected images were not found; rescan before cleaning")
        _write_transaction(
            codex_home,
            backup_root,
            backed_up,
            "image_cleanup",
            "committed",
            removed_images=removed,
            removed_bytes=removed_bytes,
            keep_one=keep_one,
        )
        return {"backup_path": str(backup_root), "removed_images": removed, "removed_bytes": removed_bytes}
    except Exception:
        for target, backup in reversed(backed_up):
            shutil.copy2(backup, target)
        raise
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _remove_index_rows(codex_home: Path, task_ids: set[str]) -> None:
    index_path = codex_home / "session_index.jsonl"
    if not index_path.is_file():
        return
    preserved = []
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            row = None
        if isinstance(row, dict) and migration_bundle.row_id(row) in task_ids:
            continue
        if line.strip():
            preserved.append(line)
    migration_bundle.atomic_write(index_path, (("\n".join(preserved) + "\n") if preserved else "").encode("utf-8"))


def _bump_catalog_revision(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        "select 1 from sqlite_master where type='table' and name='local_thread_catalog_metadata'"
    ).fetchone()
    if table:
        connection.execute(
            "update local_thread_catalog_metadata set catalog_revision=catalog_revision+1 where id=1"
        )


def _remove_catalog_rows(database: Path, task_ids: set[str]) -> int:
    if not task_ids:
        return 0
    placeholders = ",".join("?" for _ in task_ids)
    values = sorted(task_ids)
    connection = sqlite3.connect(database)
    try:
        connection.execute("begin immediate")
        visible_count = connection.execute(
            f"select count(*) from local_thread_catalog "
            f"where host_id='local' and missing_candidate=0 and thread_id in ({placeholders})",
            values,
        ).fetchone()[0]
        cursor = connection.execute(
            f"delete from local_thread_catalog where host_id='local' and thread_id in ({placeholders})",
            values,
        )
        deleted = cursor.rowcount
        if visible_count:
            _bump_catalog_revision(connection)
        connection.commit()
        return deleted
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _catalog_contains(database: Path, task_ids: set[str]) -> set[str]:
    if not task_ids:
        return set()
    placeholders = ",".join("?" for _ in task_ids)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        return {
            row[0]
            for row in connection.execute(
                f"select thread_id from local_thread_catalog "
                f"where host_id='local' and thread_id in ({placeholders})",
                sorted(task_ids),
            )
        }
    finally:
        connection.close()


def _catalog_thread_ids(database: Path) -> set[str]:
    """Return every local catalog row, including stale/orphaned entries."""
    if not database or not database.is_file():
        return set()
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        return {
            row[0]
            for row in connection.execute(
                "select thread_id from local_thread_catalog where host_id='local'"
            )
        }
    finally:
        connection.close()


def _update_index_rollout_paths(codex_home: Path, destinations: dict[str, Path]) -> None:
    index_path = codex_home / "session_index.jsonl"
    if not index_path.is_file():
        return
    updated = []
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            updated.append(line)
            continue
        task_id = migration_bundle.row_id(row) if isinstance(row, dict) else None
        if task_id in destinations and "rollout_path" in row:
            row["rollout_path"] = str(destinations[task_id])
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        updated.append(line)
    migration_bundle.atomic_write(index_path, (("\n".join(updated) + "\n") if updated else "").encode("utf-8"))


def _active_rollout_destination(codex_home: Path, source: Path) -> Path:
    match = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T", source.name)
    if match:
        year, month, day = match.groups()
    else:
        timestamp = None
        with source.open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, start=1):
                if line_number > 100:
                    break
                value = _safe_json_line(line)
                if not isinstance(value, dict) or value.get("type") != "session_meta":
                    continue
                payload = value.get("payload") if isinstance(value.get("payload"), dict) else value
                raw_timestamp = payload.get("timestamp") or value.get("timestamp")
                if isinstance(raw_timestamp, str):
                    try:
                        timestamp = dt.datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
                    except ValueError:
                        pass
                break
        timestamp = timestamp or dt.datetime.fromtimestamp(source.stat().st_mtime, tz=dt.timezone.utc)
        year, month, day = timestamp.strftime("%Y"), timestamp.strftime("%m"), timestamp.strftime("%d")
    return codex_home / "sessions" / year / month / day / source.name


def set_conversations_archived(
    codex_home: Path,
    task_ids: set[str],
    archived: bool,
    require_codex_closed: bool = True,
) -> dict[str, Any]:
    if not task_ids:
        raise ValueError("No conversations were selected")
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before changing archive state")
    codex_home = codex_home.expanduser().resolve()
    database = migration_bundle.find_state_db(codex_home)
    if database is None:
        raise ValueError("Codex thread database was not found")
    rows = migration_bundle.read_sqlite_threads(codex_home, task_ids)
    missing = task_ids - set(rows)
    if missing:
        raise ValueError(f"Conversations changed after scanning: {', '.join(sorted(missing))}")

    operations = []
    skipped = 0
    for task_id in sorted(task_ids):
        row = rows[task_id]
        source = migration_bundle.resolve_local_path(row["rollout_path"], codex_home)
        if not source.is_file() or not source.is_relative_to(codex_home):
            raise ValueError(f"Conversation file is missing or unsafe: {task_id}")
        relative = source.relative_to(codex_home)
        archived_at_supported = "archived_at" in row
        archive_time_consistent = (
            not archived_at_supported
            or (archived and row.get("archived_at") is not None)
            or (not archived and row.get("archived_at") is None)
        )
        is_archived = (
            bool(row.get("archived"))
            and relative.parts[0] == "archived_sessions"
            and archive_time_consistent
        )
        is_active = (
            not bool(row.get("archived"))
            and relative.parts[0] == "sessions"
            and archive_time_consistent
        )
        if (archived and is_archived) or (not archived and is_active):
            skipped += 1
            continue
        destination = (
            codex_home / "archived_sessions" / source.name
            if archived
            else _active_rollout_destination(codex_home, source)
        ).resolve()
        if not destination.is_relative_to(codex_home):
            raise ValueError(f"Archive destination is unsafe: {task_id}")
        if destination != source and destination.exists():
            raise ValueError(f"Archive destination already exists: {destination}")
        operations.append({"task_id": task_id, "source": source, "destination": destination})
    if not operations:
        return {"backup_path": None, "changed": 0, "skipped": skipped, "archived": archived}

    index_path = codex_home / "session_index.jsonl"
    catalog_database = _thread_catalog_database(codex_home)
    source_paths = [operation["source"] for operation in operations]
    backup_paths = source_paths + [database] + ([index_path] if index_path.is_file() else [])
    if catalog_database:
        backup_paths.append(catalog_database)
    operation_name = "conversation-archive" if archived else "conversation-unarchive"
    backup_root, backed_up = _backup_selected_files(codex_home, backup_paths, operation_name)
    created_files = [
        operation["destination"] for operation in operations if operation["destination"] != operation["source"]
    ]
    descriptor, lock_path = migration_bundle.acquire_lock(codex_home)
    try:
        _write_transaction(
            codex_home,
            backup_root,
            backed_up,
            operation_name.replace("-", "_"),
            "in_progress",
            created_files=created_files,
            task_ids=[operation["task_id"] for operation in operations],
            archived=archived,
        )
        destinations = {}
        for operation in operations:
            source = operation["source"]
            destination = operation["destination"]
            if destination != source:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
            destinations[operation["task_id"]] = destination
        _update_index_rollout_paths(codex_home, destinations)
        if catalog_database:
            # Codex treats archive as authoritative removal. On unarchive its own
            # observer rebuilds the full catalog row after the application starts.
            _remove_catalog_rows(catalog_database, set(destinations))
        connection = sqlite3.connect(database)
        try:
            columns = {row[1] for row in connection.execute("pragma table_info(threads)")}
            if not {"rollout_path", "archived"}.issubset(columns):
                raise ValueError("The Codex thread database does not support archive state")
            archive_timestamp = int(dt.datetime.now(dt.timezone.utc).timestamp())
            for task_id, destination in destinations.items():
                if "archived_at" in columns:
                    current_archived_at = rows[task_id].get("archived_at")
                    target_archived_at = (current_archived_at or archive_timestamp) if archived else None
                    connection.execute(
                        "update threads set rollout_path=?, archived=?, archived_at=? where id=?",
                        (str(destination), int(archived), target_archived_at, task_id),
                    )
                else:
                    connection.execute(
                        "update threads set rollout_path=?, archived=? where id=?",
                        (str(destination), int(archived), task_id),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        verified = migration_bundle.read_sqlite_threads(codex_home, set(destinations))
        for task_id, destination in destinations.items():
            row = verified.get(task_id)
            if (
                row is None
                or bool(row.get("archived")) != archived
                or (
                    "archived_at" in row
                    and ((row.get("archived_at") is not None) != archived)
                )
                or migration_bundle.resolve_local_path(row.get("rollout_path"), codex_home) != destination
                or not destination.is_file()
            ):
                raise ValueError(f"Archive verification failed: {task_id}")
        if catalog_database and _catalog_contains(catalog_database, set(destinations)):
            raise ValueError("Sidebar catalog authoritative-removal verification failed")
        _write_transaction(
            codex_home,
            backup_root,
            backed_up,
            operation_name.replace("-", "_"),
            "committed",
            created_files=created_files,
            task_ids=list(destinations),
            archived=archived,
            changed=len(destinations),
            skipped=skipped,
        )
        return {
            "backup_path": str(backup_root),
            "changed": len(destinations),
            "skipped": skipped,
            "archived": archived,
            "catalog_rebuild_required": bool(catalog_database and not archived),
        }
    except Exception:
        for path in reversed(created_files):
            path.unlink(missing_ok=True)
        for target, backup in reversed(backed_up):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        raise
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def delete_conversations(codex_home: Path, task_ids: set[str], require_codex_closed: bool = True) -> dict[str, Any]:
    if not task_ids:
        raise ValueError("No conversations were selected")
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before deleting conversations")
    codex_home = codex_home.expanduser().resolve()
    database = migration_bundle.find_state_db(codex_home)
    if database is None:
        raise ValueError("Codex thread database was not found")
    rows = migration_bundle.read_sqlite_threads(codex_home, task_ids)
    missing = task_ids - set(rows)
    if missing:
        raise ValueError(f"Conversations changed after scanning: {', '.join(sorted(missing))}")
    rollout_paths = [migration_bundle.resolve_local_path(rows[task_id]["rollout_path"], codex_home) for task_id in sorted(task_ids)]
    for path in rollout_paths:
        if not path.is_file() or not path.is_relative_to(codex_home):
            raise ValueError(f"Conversation file is missing or unsafe: {path}")
    index_path = codex_home / "session_index.jsonl"
    catalog_database = _thread_catalog_database(codex_home)
    backup_paths = rollout_paths + [database] + ([index_path] if index_path.is_file() else [])
    if catalog_database:
        backup_paths.append(catalog_database)
    backup_root, backed_up = _backup_selected_files(codex_home, backup_paths, "conversation-delete")
    descriptor, lock_path = migration_bundle.acquire_lock(codex_home)
    try:
        _write_transaction(codex_home, backup_root, backed_up, "conversation_delete", "in_progress", task_ids=sorted(task_ids))
        _remove_index_rows(codex_home, task_ids)
        if catalog_database:
            _remove_catalog_rows(catalog_database, task_ids)
        connection = sqlite3.connect(database)
        try:
            connection.execute("pragma foreign_keys=on")
            placeholders = ",".join("?" for _ in task_ids)
            values = sorted(task_ids)
            edge_table = connection.execute(
                "select 1 from sqlite_master where type='table' and name='thread_spawn_edges'"
            ).fetchone()
            if edge_table:
                connection.execute(
                    f"delete from thread_spawn_edges where parent_thread_id in ({placeholders}) or child_thread_id in ({placeholders})",
                    values + values,
                )
            connection.execute(f"delete from threads where id in ({placeholders})", values)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        for path in rollout_paths:
            path.unlink()
        if migration_bundle.read_sqlite_threads(codex_home, task_ids):
            raise ValueError("Conversation database deletion verification failed")
        if catalog_database and _catalog_contains(catalog_database, task_ids):
            raise ValueError("Sidebar catalog deletion verification failed")
        _write_transaction(codex_home, backup_root, backed_up, "conversation_delete", "committed", task_ids=sorted(task_ids))
        return {"backup_path": str(backup_root), "deleted": len(task_ids)}
    except Exception:
        for target, backup in reversed(backed_up):
            if target.suffix == ".sqlite":
                shutil.copy2(backup, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
        raise
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def clean_stale_sidebar_entries(
    codex_home: Path,
    task_ids: set[str],
    require_codex_closed: bool = True,
) -> dict[str, Any]:
    if not task_ids:
        raise ValueError("No stale sidebar entries were selected")
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before cleaning sidebar entries")
    codex_home = codex_home.expanduser().resolve()
    catalog_database, catalog_rows = _read_thread_catalog(codex_home)
    if catalog_database is None:
        raise ValueError("Codex sidebar catalog was not found")
    state_rows = _read_thread_rows(codex_home)
    state_ids = set(state_rows)
    archived_state_ids = {task_id for task_id, row in state_rows.items() if bool(row.get("archived"))}
    rollout_ids = _rollout_ids(codex_home)
    orphan_catalog_ids = set(catalog_rows) - state_ids - rollout_ids
    archived_catalog_ids = set(catalog_rows) & archived_state_ids
    safe_stale_ids = (orphan_catalog_ids | archived_catalog_ids) & task_ids
    unsafe = task_ids - safe_stale_ids
    if unsafe:
        raise ValueError(
            "Some sidebar entries are no longer safe to clean; rescan first: "
            + ", ".join(sorted(unsafe))
        )

    index_path = codex_home / "session_index.jsonl"
    backup_paths = [catalog_database] + ([index_path] if index_path.is_file() else [])
    backup_root, backed_up = _backup_selected_files(codex_home, backup_paths, "sidebar-stale-cleanup")
    descriptor, lock_path = migration_bundle.acquire_lock(codex_home)
    try:
        _write_transaction(
            codex_home,
            backup_root,
            backed_up,
            "sidebar_stale_cleanup",
            "in_progress",
            task_ids=sorted(safe_stale_ids),
        )
        removable_index_ids = safe_stale_ids & orphan_catalog_ids
        _remove_index_rows(codex_home, removable_index_ids)
        deleted = _remove_catalog_rows(catalog_database, safe_stale_ids)
        if _catalog_contains(catalog_database, safe_stale_ids):
            raise ValueError("Sidebar catalog cleanup verification failed")
        remaining_index = set(migration_bundle.read_session_index(codex_home))
        if remaining_index & removable_index_ids:
            raise ValueError("Session index cleanup verification failed")
        _write_transaction(
            codex_home,
            backup_root,
            backed_up,
            "sidebar_stale_cleanup",
            "committed",
            task_ids=sorted(safe_stale_ids),
            deleted=deleted,
        )
        return {"backup_path": str(backup_root), "deleted": deleted}
    except Exception:
        for target, backup in reversed(backed_up):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        raise
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _project_trash_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "CrossDeviceAgentSync" / "project-trash"


def _validate_project_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    protected = {
        Path.home().resolve(),
        (Path.home() / "Documents").resolve(),
        (Path.home() / "Desktop").resolve(),
        (Path.home() / ".codex").resolve(),
        Path(path.anchor).resolve(),
    }
    if path in protected or len(path.parts) < 4:
        raise ValueError(f"Refusing to move a broad or protected directory: {path}")
    if not path.is_dir():
        raise ValueError(f"Project directory does not exist: {path}")
    return path


def move_projects_to_trash(paths: list[Path], require_codex_closed: bool = True) -> dict[str, Any]:
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before moving project directories")
    selected = sorted({_validate_project_path(path) for path in paths}, key=lambda path: len(path.parts))
    for index, parent in enumerate(selected):
        if any(child.is_relative_to(parent) for child in selected[index + 1:]):
            raise ValueError("Do not select both a project directory and one of its subdirectories")
    trash_root = _project_trash_root()
    trash_root.mkdir(parents=True, exist_ok=True)
    moved = []
    try:
        for source in selected:
            item_root = trash_root / f"{_now_stamp()}-{source.name}-{uuid.uuid4().hex[:8]}"
            target = item_root / "project"
            item_root.mkdir(parents=True, exist_ok=False)
            shutil.move(str(source), str(target))
            manifest = {
                "schema_version": 1,
                "status": "trashed",
                "original_path": str(source),
                "project_path": str(target),
                "created_at": migration_bundle.now_iso(),
            }
            (item_root / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            moved.append((source, target, item_root))
        return {"moved": len(moved), "trash_root": str(trash_root), "items": [str(item[2]) for item in moved]}
    except Exception:
        for source, target, item_root in reversed(moved):
            if target.exists() and not source.exists():
                shutil.move(str(target), str(source))
            shutil.rmtree(item_root, ignore_errors=True)
        raise


def list_project_trash() -> list[dict[str, Any]]:
    root = _project_trash_root()
    if not root.is_dir():
        return []
    results = []
    for item_root in root.iterdir():
        manifest_path = item_root / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("status") == "trashed" and (item_root / "project").is_dir():
            results.append({**manifest, "item_root": str(item_root)})
    return sorted(results, key=lambda item: item.get("created_at", ""), reverse=True)


def restore_project(item_root: Path, require_codex_closed: bool = True) -> dict[str, Any]:
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before restoring a project directory")
    item_root = item_root.expanduser().resolve()
    root = _project_trash_root().resolve()
    if not item_root.is_relative_to(root):
        raise ValueError("Selected project is outside the managed project trash")
    manifest_path = item_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = item_root / "project"
    target = Path(manifest["original_path"]).expanduser().resolve()
    if target.exists():
        target = target.with_name(f"{target.name}-restored-{_now_stamp()}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    manifest["status"] = "restored"
    manifest["restored_path"] = str(target)
    manifest["restored_at"] = migration_bundle.now_iso()
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"restored_path": str(target)}

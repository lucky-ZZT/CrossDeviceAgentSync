#!/usr/bin/env python3
"""Read-only content inventory and recoverable bulk management operations."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import ntpath
import os
import re
import shutil
import sqlite3
import uuid
from collections import deque
from pathlib import Path, PureWindowsPath
from typing import Any, Iterator

import codex_compat
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
ROLLOUT_PATH_NORMALIZE_TRIGGERS = {
    "threads_rollout_path_normalize_after_insert",
    "threads_rollout_path_normalize_after_update",
}
GLOBAL_STATE_FILE_NAME = ".codex-global-state.json"
PROJECT_ID_SEQUENCE_FIELDS = (
    "project-order",
    "pinned-project-ids",
    "electron-saved-workspace-roots",
)
PROJECT_ID_KEYED_FIELDS = (
    "project-appearances",
    "project-files",
    "sidebar-project-thread-orders",
    "project-writable-roots",
)


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


def _normalized_extended_rollout_path(value: Any) -> tuple[str | None, str | None]:
    raw = str(value or "").strip()
    lowered = raw.casefold()
    if lowered.startswith("\\\\?\\unc\\"):
        return "\\\\" + raw[8:], "extended_unc"
    if lowered.startswith("\\\\?\\"):
        candidate = raw[4:]
        if re.match(r"^[A-Za-z]:\\", candidate):
            return candidate, "extended_drive"
        return None, "unsupported_extended"
    return None, None


def _normalized_project_path(value: Any) -> tuple[str | None, str | None]:
    raw = str(value or "").strip()
    if not raw:
        return None, None
    normalized, kind = _normalized_extended_rollout_path(raw)
    candidate = normalized if kind is not None else raw
    if candidate is None or not ntpath.isabs(candidate):
        return None, kind
    try:
        resolved = str(Path(candidate).expanduser().resolve())
    except (OSError, RuntimeError):
        resolved = ntpath.normpath(candidate)
    return resolved, kind


def _project_path_identity(value: Any) -> str | None:
    normalized, _kind = _normalized_project_path(value)
    return ntpath.normcase(ntpath.normpath(normalized)) if normalized else None


def _path_belongs_to_project(value: Any, project_roots: list[str]) -> bool:
    identity = _project_path_identity(value)
    if identity is None:
        return False
    for root in project_roots:
        root_identity = _project_path_identity(root)
        if root_identity is None:
            continue
        try:
            if ntpath.commonpath([identity, root_identity]) == root_identity:
                return True
        except ValueError:
            continue
    return False


def _project_roots_identity(project: Any) -> tuple[str, ...] | None:
    roots = project.get("rootPaths") if isinstance(project, dict) else None
    if not isinstance(roots, list) or not roots:
        return None
    identities = tuple(_project_path_identity(root) for root in roots)
    if any(identity is None for identity in identities):
        return None
    return identities  # type: ignore[return-value]


def _project_reference_paths(payload: Any, project_ids: set[str]) -> dict[str, list[str]]:
    found = {project_id: [] for project_id in project_ids}

    def walk(node: Any, path: tuple[Any, ...]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in project_ids:
                    found[key].append(".".join(map(str, path + (key, "<key>"))))
                walk(value, path + (key,))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, path + (index,))
        elif isinstance(node, str) and node in project_ids:
            found[node].append(".".join(map(str, path)))

    walk(payload, ())
    return found


def _known_project_reference(path: str, project_id: str) -> bool:
    parts = path.split(".")
    if len(parts) >= 2 and parts[0] == "local-projects" and parts[1] == project_id:
        return len(parts) == 3 and parts[2] in {"<key>", "id"}
    if parts and parts[0] in PROJECT_ID_SEQUENCE_FIELDS:
        return len(parts) in {1, 2}
    if parts == ["selected-project", "projectId"]:
        return True
    if len(parts) == 3 and parts[0] == "thread-project-assignments" and parts[2] == "projectId":
        return True
    if len(parts) == 3 and parts[0] in PROJECT_ID_KEYED_FIELDS and parts[1] == project_id:
        return parts[2] == "<key>"
    return False


def _project_reference_count(paths: list[str], project_id: str) -> int:
    return sum(
        not path.startswith(f"local-projects.{project_id}.")
        for path in paths
    )


def _project_assignment_count(payload: dict[str, Any], project_id: str) -> int:
    assignments = payload.get("thread-project-assignments")
    if not isinstance(assignments, dict):
        return 0
    return sum(
        isinstance(value, dict) and value.get("projectId") == project_id
        for value in assignments.values()
    )


def _inspect_project_path_health(
    codex_home: Path,
    thread_rows: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Inspect extended and duplicate paths in Codex's local project registry."""
    state_path = codex_home / GLOBAL_STATE_FILE_NAME
    empty = {
        "global_state": None,
        "global_state_sha256": None,
        "project_extended_paths": [],
        "repairable_project_paths": [],
        "duplicate_projects": [],
        "blocked_duplicate_projects": [],
        "blocked_project_paths": [],
        "removable_projects": [],
        "actionable_project_registrations": [],
        "registered_projects": [],
    }
    if not state_path.is_file():
        return empty
    try:
        raw_state = state_path.read_bytes()
        payload = json.loads(raw_state.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        result = dict(empty)
        result.update({
            "global_state": str(state_path),
            "blocked_project_paths": [{
                "project_id": "",
                "project_name": "",
                "raw_path": "",
                "normalized_path": None,
                "repairable": False,
                "removable": False,
                "reason": f"Codex 全局项目状态无法读取：{error}",
            }],
        })
        return result
    projects = payload.get("local-projects")
    if not isinstance(projects, dict):
        projects = {}
    rows = thread_rows if thread_rows is not None else _read_thread_rows(codex_home)
    database_available = migration_bundle.find_state_db(codex_home) is not None
    normalized_cwds: list[str] = []
    for row in rows.values():
        cwd = str(row.get("cwd") or "").strip()
        identity = _project_path_identity(cwd)
        if identity:
            normalized_cwds.append(identity)

    registered_projects = []
    for project_id, project in projects.items():
        if not isinstance(project, dict):
            continue
        project_name = str(project.get("name") or project_id)
        roots = project.get("rootPaths")
        if not isinstance(roots, list):
            continue
        for root_index, root in enumerate(roots):
            raw_path = str(root or "")
            normalized_path, path_kind = _normalized_project_path(raw_path)
            display_path = normalized_path or raw_path
            exists = bool(normalized_path and Path(normalized_path).is_dir())
            if path_kind:
                path_status = "扩展路径"
            elif exists:
                path_status = "普通路径"
            else:
                path_status = "目录不存在"
            registered_projects.append({
                "project_id": str(project_id),
                "project_name": project_name,
                "root_index": root_index,
                "raw_path": raw_path,
                "path": display_path,
                "path_kind": path_kind or "ordinary",
                "path_status": path_status,
                "exists": exists,
            })

    all_project_ids = {str(project_id) for project_id in projects}
    reference_paths = _project_reference_paths(payload, all_project_ids)
    unknown_references = {
        project_id: [
            path for path in paths
            if not _known_project_reference(path, project_id)
        ]
        for project_id, paths in reference_paths.items()
    }

    identity_groups: dict[tuple[str, ...], list[str]] = {}
    for project_id, project in projects.items():
        identity = _project_roots_identity(project)
        if identity:
            identity_groups.setdefault(identity, []).append(str(project_id))

    duplicate_projects = []
    blocked_duplicate_projects = []
    duplicate_losers: set[str] = set()
    duplicate_members: set[str] = set()
    for identity, project_ids in identity_groups.items():
        if len(project_ids) < 2:
            continue

        def keeper_key(project_id: str) -> tuple[Any, ...]:
            project = projects[project_id]
            roots = project.get("rootPaths", [])
            has_extended = any(_normalized_extended_rollout_path(root)[1] for root in roots)
            references = _project_reference_count(reference_paths.get(project_id, []), project_id)
            created = project.get("createdAt")
            created_value = created if isinstance(created, (int, float)) else float("inf")
            return has_extended, -references, created_value, project_id

        ordered = sorted(project_ids, key=keeper_key)
        keeper_id, remove_ids = ordered[0], ordered[1:]
        unknown = {
            project_id: unknown_references.get(project_id, [])
            for project_id in ordered
            if unknown_references.get(project_id)
        }
        members = []
        for project_id in ordered:
            project = projects[project_id]
            raw_roots = [str(root or "") for root in project.get("rootPaths", [])]
            root_details = []
            for raw_root in raw_roots:
                normalized_root, path_kind = _normalized_project_path(raw_root)
                root_details.append({
                    "raw_path": raw_root,
                    "normalized_path": normalized_root,
                    "path_kind": path_kind or "ordinary",
                    "exists": bool(normalized_root and Path(normalized_root).is_dir()),
                })
            member_identities = {
                identity for identity in (_project_path_identity(root) for root in raw_roots)
                if identity
            }
            members.append({
                "project_id": project_id,
                "project_name": str(project.get("name") or project_id),
                "roots": root_details,
                "has_extended_path": any(root["path_kind"] != "ordinary" for root in root_details),
                "all_directories_exist": bool(root_details) and all(root["exists"] for root in root_details),
                "known_reference_count": _project_reference_count(
                    reference_paths.get(project_id, []), project_id
                ),
                "assignment_count": _project_assignment_count(payload, project_id),
                "linked_tasks": sum(cwd in member_identities for cwd in normalized_cwds),
                "recommended_keeper": project_id == keeper_id,
            })
        item = {
            "keeper_id": keeper_id,
            "keeper_name": str(projects[keeper_id].get("name") or keeper_id),
            "remove_ids": remove_ids,
            "remove_names": [str(projects[item].get("name") or item) for item in remove_ids],
            "member_ids": ordered,
            "member_names": [str(projects[item].get("name") or item) for item in ordered],
            "members": members,
            "normalized_paths": list(identity),
            "unknown_references": unknown,
            "reason": (
                "存在无法识别的项目引用，禁止自动合并"
                if unknown else "多个项目注册指向同一目录，可合并为一个项目"
            ),
        }
        duplicate_members.update(ordered)
        if unknown:
            blocked_duplicate_projects.append(item)
        else:
            duplicate_projects.append(item)
            duplicate_losers.update(remove_ids)

    extended_paths = []
    project_groups: dict[str, list[dict[str, Any]]] = {}
    for project_id, project in projects.items():
        if not isinstance(project, dict):
            continue
        roots = project.get("rootPaths")
        if not isinstance(roots, list):
            continue
        project_name = str(project.get("name") or project_id)
        for root_index, value in enumerate(roots):
            raw = str(value or "")
            normalized, kind = _normalized_extended_rollout_path(raw)
            if kind is None:
                continue
            item = {
                "project_id": str(project_id),
                "project_name": project_name,
                "root_index": root_index,
                "raw_path": raw,
                "normalized_path": normalized,
                "kind": kind,
                "repairable": False,
                "removable": False,
                "linked_tasks": 0,
                "assignment_count": _project_assignment_count(payload, str(project_id)),
                "unknown_references": unknown_references.get(str(project_id), []),
                "reason": "",
            }
            if normalized is None:
                item["reason"] = "项目扩展路径格式无法安全识别"
            else:
                normalized_value, _ignored = _normalized_project_path(raw)
                item["normalized_path"] = normalized_value
                item["linked_tasks"] = sum(
                    cwd == _project_path_identity(normalized_value) for cwd in normalized_cwds
                )
                if normalized_value is None:
                    item["reason"] = "规范化后的项目路径不是绝对路径"
                elif project_id in duplicate_losers:
                    keeper = next(
                        group for group in duplicate_projects
                        if project_id in group["remove_ids"]
                    )
                    item["reason"] = f"与 {keeper['keeper_name']} 指向同一目录，将合并项目记录"
                elif any(
                    project_id in group["remove_ids"] or project_id == group["keeper_id"]
                    for group in blocked_duplicate_projects
                ):
                    item["reason"] = "同路径重复项目存在未知引用，需人工检查"
                elif Path(normalized_value).is_dir():
                    item["repairable"] = True
                    item["reason"] = "项目目录存在，可安全规范化"
                elif item["linked_tasks"]:
                    item["reason"] = "项目目录不存在，但仍有关联对话"
                elif not database_available:
                    item["reason"] = "项目目录不存在，且无法确认是否有关联对话"
                else:
                    item["reason"] = "项目目录不存在且没有关联对话，可移除残留项目注册"
            extended_paths.append(item)
            project_groups.setdefault(str(project_id), []).append(item)

    removable_projects = []
    for project_id, items in project_groups.items():
        project = projects.get(project_id, {})
        roots = project.get("rootPaths") if isinstance(project, dict) else None
        if (
            isinstance(roots, list)
            and len(items) == len(roots)
            and items
            and all(
                not item["repairable"]
                and item.get("normalized_path")
                and item["linked_tasks"] == 0
                and "可移除残留项目注册" in item["reason"]
                for item in items
            )
        ):
            assignment_count = _project_assignment_count(payload, project_id)
            unknown = unknown_references.get(project_id, [])
            removable = {
                "project_id": project_id,
                "project_name": str(project.get("name") or project_id),
                "paths": [item["raw_path"] for item in items],
                "normalized_paths": [item.get("normalized_path") for item in items],
                "assignment_count": assignment_count,
                "unknown_references": unknown,
                "reason": "全部项目目录均不存在且没有关联对话",
            }
            if assignment_count:
                for item in items:
                    item["reason"] = f"项目目录不存在，但仍有 {assignment_count} 条侧栏任务归属"
            elif unknown:
                for item in items:
                    item["reason"] = "项目目录不存在，但存在无法识别的项目引用"
            else:
                removable_projects.append(removable)
                for item in items:
                    item["removable"] = True

    repairable_project_paths = [
        item for item in extended_paths
        if item["repairable"] and item["project_id"] not in duplicate_losers
    ]
    blocked_project_paths = [
        item for item in extended_paths
        if (
            not item["repairable"]
            and not item["removable"]
            and item["project_id"] not in duplicate_members
        )
    ]
    actionable_project_registrations = []
    for group in duplicate_projects + blocked_duplicate_projects:
        for member in group.get("members", []):
            status = (
                "扩展路径异常"
                if member["has_extended_path"] else
                "普通路径" if member["all_directories_exist"] else
                "目录不存在"
            )
            actionable_project_registrations.append({
                **member,
                "issue_type": "duplicate",
                "duplicate_group": group["keeper_id"],
                "status": status,
                "recommended_action": "keep" if member["recommended_keeper"] else "delete",
                "reason": "同一目录存在重复注册",
                "unknown_references": group.get("unknown_references", {}).get(
                    member["project_id"], []
                ),
            })
    repairable_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in repairable_project_paths:
        repairable_by_id.setdefault(item["project_id"], []).append(item)
    for project_id, items in repairable_by_id.items():
        actionable_project_registrations.append({
            "project_id": project_id,
            "project_name": items[0]["project_name"],
            "roots": [{
                "raw_path": item["raw_path"],
                "normalized_path": item["normalized_path"],
                "path_kind": item["kind"],
                "exists": bool(item.get("normalized_path") and Path(item["normalized_path"]).is_dir()),
            } for item in items],
            "has_extended_path": True,
            "all_directories_exist": True,
            "known_reference_count": _project_reference_count(
                reference_paths.get(project_id, []), project_id
            ),
            "assignment_count": items[0].get("assignment_count", 0),
            "linked_tasks": max(item.get("linked_tasks", 0) for item in items),
            "issue_type": "extended",
            "duplicate_group": None,
            "status": "扩展路径异常",
            "recommended_action": "normalize",
            "reason": items[0]["reason"],
            "unknown_references": items[0].get("unknown_references", []),
        })
    for item in removable_projects:
        actionable_project_registrations.append({
            "project_id": item["project_id"],
            "project_name": item["project_name"],
            "roots": [{
                "raw_path": raw,
                "normalized_path": normalized,
                "path_kind": _normalized_extended_rollout_path(raw)[1] or "ordinary",
                "exists": False,
            } for raw, normalized in zip(item["paths"], item.get("normalized_paths", []))],
            "has_extended_path": any(_normalized_extended_rollout_path(raw)[1] for raw in item["paths"]),
            "all_directories_exist": False,
            "known_reference_count": _project_reference_count(
                reference_paths.get(item["project_id"], []), item["project_id"]
            ),
            "assignment_count": item.get("assignment_count", 0),
            "linked_tasks": 0,
            "issue_type": "stale",
            "duplicate_group": None,
            "status": "目录不存在",
            "recommended_action": "delete",
            "reason": item["reason"],
            "unknown_references": item.get("unknown_references", []),
        })

    existing_actionable_ids = {
        item["project_id"] for item in actionable_project_registrations
    }
    blocked_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in blocked_project_paths:
        blocked_by_id.setdefault(item["project_id"], []).append(item)
    for project_id, items in blocked_by_id.items():
        if project_id in existing_actionable_ids:
            continue
        root_details = [{
            "raw_path": item["raw_path"],
            "normalized_path": item.get("normalized_path"),
            "path_kind": item.get("kind") or "unsupported",
            "exists": bool(item.get("normalized_path") and Path(item["normalized_path"]).is_dir()),
        } for item in items]
        actionable_project_registrations.append({
            "project_id": project_id,
            "project_name": items[0]["project_name"],
            "roots": root_details,
            "has_extended_path": any(root["path_kind"] != "ordinary" for root in root_details),
            "all_directories_exist": bool(root_details) and all(root["exists"] for root in root_details),
            "known_reference_count": _project_reference_count(
                reference_paths.get(project_id, []), project_id
            ),
            "assignment_count": items[0].get("assignment_count", 0),
            "linked_tasks": max(item.get("linked_tasks", 0) for item in items),
            "issue_type": "blocked",
            "duplicate_group": None,
            "status": "目录不存在" if not any(root["exists"] for root in root_details) else "路径需检查",
            "recommended_action": "keep",
            "reason": items[0]["reason"],
            "unknown_references": items[0].get("unknown_references", []),
        })

    duplicate_group_sizes = {
        group["keeper_id"]: len(group.get("member_ids") or [group["keeper_id"]] + group["remove_ids"])
        for group in duplicate_projects + blocked_duplicate_projects
    }
    for registration in actionable_project_registrations:
        project_id = registration["project_id"]
        roots = [
            root["normalized_path"] or root["raw_path"]
            for root in registration.get("roots", [])
            if root.get("normalized_path") or root.get("raw_path")
        ]
        related_rows = [
            row for row in rows.values()
            if _path_belongs_to_project(row.get("cwd"), roots)
        ]
        related_tasks = [{
            "task_id": str(row.get("id") or ""),
            "title": repair_mojibake(str(row.get("title") or row.get("id") or "")),
        } for row in related_rows]
        registration["related_tasks"] = related_tasks
        registration["linked_tasks"] = len(related_tasks)
        unknown = unknown_references.get(project_id, [])

        extended_roots = [
            root for root in registration.get("roots", [])
            if root.get("path_kind") != "ordinary"
        ]
        can_normalize = bool(extended_roots) and all(
            root.get("normalized_path") and Path(root["normalized_path"]).is_dir()
            for root in extended_roots
        )
        if not extended_roots:
            normalize_reason = "当前已经是普通路径"
        elif not can_normalize:
            normalize_reason = "规范化后的目录不存在，需选择正确目录"
        else:
            normalize_reason = "已验证规范化后的目录存在"

        can_delete_registration = not unknown
        delete_reason = (
            "存在无法识别的项目引用，不能保证删除完整"
            if unknown else "可删除该项目 ID 及全部已知侧栏元数据"
        )

        shared_count = duplicate_group_sizes.get(registration.get("duplicate_group"), 1)
        full_delete_reason = ""
        can_full_delete = True
        if not database_available:
            can_full_delete = False
            full_delete_reason = "无法读取线程数据库，不能完整枚举关联对话"
        elif unknown:
            can_full_delete = False
            full_delete_reason = "存在无法识别的项目引用"
        elif shared_count > 1:
            can_full_delete = False
            full_delete_reason = "项目目录仍被同路径的其他注册共用"
        elif not roots or not all(Path(root).is_dir() for root in roots):
            can_full_delete = False
            full_delete_reason = "项目目录不存在"
        else:
            try:
                for root in roots:
                    _validate_project_path(Path(root))
                for row in related_rows:
                    rollout = migration_bundle.resolve_local_path(row.get("rollout_path"), codex_home)
                    if not rollout.is_file() or not rollout.is_relative_to(codex_home):
                        raise ValueError("关联对话文件缺失或不在当前 Codex 数据目录")
            except (OSError, RuntimeError, ValueError) as error:
                can_full_delete = False
                full_delete_reason = str(error)
        if can_full_delete:
            full_delete_reason = "可备份并删除关联对话、注册，并把项目目录移入可恢复区"

        registration["capabilities"] = {
            "details": {"enabled": True, "reason": "可查看完整注册和关联信息"},
            "keep": {"enabled": True, "reason": "保留当前项目注册"},
            "normalize": {"enabled": can_normalize, "reason": normalize_reason},
            "repoint": {"enabled": True, "reason": "选择一个实际存在且未冲突的目录后可更正"},
            "rename": {"enabled": True, "reason": "只修改侧栏显示名称，不移动目录"},
            "delete": {"enabled": can_delete_registration, "reason": delete_reason},
            "full_delete": {"enabled": can_full_delete, "reason": full_delete_reason},
        }

    return {
        "global_state": str(state_path),
        "global_state_sha256": hashlib.sha256(raw_state).hexdigest(),
        "project_extended_paths": extended_paths,
        "repairable_project_paths": repairable_project_paths,
        "duplicate_projects": duplicate_projects,
        "blocked_duplicate_projects": blocked_duplicate_projects,
        "blocked_project_paths": blocked_project_paths,
        "removable_projects": removable_projects,
        "actionable_project_registrations": actionable_project_registrations,
        "registered_projects": registered_projects,
    }


def _dedupe_values(values: list[Any]) -> list[Any]:
    result = []
    signatures = set()
    for value in values:
        try:
            signature = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            signature = repr(value)
        if signature not in signatures:
            signatures.add(signature)
            result.append(value)
    return result


def _rewrite_project_sequence(
    payload: dict[str, Any],
    field: str,
    remap: dict[str, str],
    removed: set[str],
) -> None:
    value = payload.get(field)
    if isinstance(value, list):
        rewritten = []
        for item in value:
            if isinstance(item, str):
                item = remap.get(item, item)
                if item in removed:
                    continue
            rewritten.append(item)
        payload[field] = _dedupe_values(rewritten)
    elif isinstance(value, str):
        rewritten = remap.get(value, value)
        if rewritten in removed:
            payload.pop(field, None)
        else:
            payload[field] = rewritten


def _merge_keyed_project_value(field: str, keeper: Any, duplicate: Any) -> Any:
    if keeper is None:
        return duplicate
    if duplicate is None:
        return keeper
    if field in {"project-files", "project-writable-roots"}:
        if isinstance(keeper, list) and isinstance(duplicate, list):
            return _dedupe_values(keeper + duplicate)
    if field == "sidebar-project-thread-orders":
        if isinstance(keeper, dict) and isinstance(duplicate, dict):
            merged = dict(duplicate)
            merged.update(keeper)
            keeper_threads = keeper.get("threadIds")
            duplicate_threads = duplicate.get("threadIds")
            if isinstance(keeper_threads, list) and isinstance(duplicate_threads, list):
                merged["threadIds"] = _dedupe_values(keeper_threads + duplicate_threads)
            return merged
    return keeper


def _rewrite_project_references(
    payload: dict[str, Any],
    remap: dict[str, str],
    removed: set[str],
) -> None:
    for field in PROJECT_ID_SEQUENCE_FIELDS:
        _rewrite_project_sequence(payload, field, remap, removed)

    selected = payload.get("selected-project")
    if isinstance(selected, dict):
        project_id = selected.get("projectId")
        if isinstance(project_id, str):
            project_id = remap.get(project_id, project_id)
            if project_id in removed:
                payload.pop("selected-project", None)
            else:
                selected["projectId"] = project_id

    assignments = payload.get("thread-project-assignments")
    if isinstance(assignments, dict):
        for task_id, assignment in list(assignments.items()):
            if not isinstance(assignment, dict):
                continue
            project_id = assignment.get("projectId")
            if isinstance(project_id, str) and project_id in remap:
                assignment["projectId"] = remap[project_id]
            elif project_id in removed:
                assignments.pop(task_id, None)

    for field in PROJECT_ID_KEYED_FIELDS:
        values = payload.get(field)
        if not isinstance(values, dict):
            continue
        for old_id, keeper_id in remap.items():
            if old_id not in values:
                continue
            duplicate = values.pop(old_id)
            values[keeper_id] = _merge_keyed_project_value(
                field, values.get(keeper_id), duplicate
            )
        for project_id in removed:
            values.pop(project_id, None)


def _validated_project_display_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("Project display name cannot be empty")
    if len(name) > 120:
        raise ValueError("Project display name cannot exceed 120 characters")
    if any(ord(character) < 32 for character in name):
        raise ValueError("Project display name contains control characters")
    return name


def _validated_project_directory(value: Any) -> str:
    normalized, _kind = _normalized_project_path(value)
    if normalized is None:
        raise ValueError("Project directory must be an absolute Windows path")
    candidate = Path(normalized).expanduser()
    if not candidate.is_absolute() or not candidate.is_dir():
        raise ValueError(f"Project directory does not exist: {normalized}")
    try:
        return str(candidate.resolve())
    except (OSError, RuntimeError):
        return str(candidate)


def _apply_project_registry_repairs(
    payload: dict[str, Any],
    duplicate_projects: list[dict[str, Any]],
    project_repairs: list[dict[str, Any]],
    removable_projects: list[dict[str, Any]],
    removed_projects: list[dict[str, Any]] | None = None,
    renamed_projects: list[dict[str, Any]] | None = None,
    repointed_projects: list[dict[str, Any]] | None = None,
) -> tuple[int, int, int, int, int, int]:
    projects = payload.get("local-projects")
    if not isinstance(projects, dict):
        raise ValueError("Codex local project registry changed after scanning")
    removed_projects = removed_projects or []
    renamed_projects = renamed_projects or []
    repointed_projects = repointed_projects or []

    remap: dict[str, str] = {}
    duplicate_remove_ids: set[str] = set()
    for group in duplicate_projects:
        keeper_id = group["keeper_id"]
        remove_ids = list(group["remove_ids"])
        keeper = projects.get(keeper_id)
        if not isinstance(keeper, dict):
            raise ValueError(f"Project changed after scanning: {group['keeper_name']}")
        for project_id in remove_ids:
            duplicate = projects.get(project_id)
            if not isinstance(duplicate, dict):
                raise ValueError(f"Duplicate project changed after scanning: {project_id}")
            if _project_roots_identity(duplicate) != _project_roots_identity(keeper):
                raise ValueError(f"Duplicate project path changed after scanning: {project_id}")
            remap[project_id] = keeper_id
            duplicate_remove_ids.add(project_id)
            created_values = [
                value for value in (keeper.get("createdAt"), duplicate.get("createdAt"))
                if isinstance(value, (int, float))
            ]
            updated_values = [
                value for value in (keeper.get("updatedAt"), duplicate.get("updatedAt"))
                if isinstance(value, (int, float))
            ]
            if created_values:
                keeper["createdAt"] = min(created_values)
            if updated_values:
                keeper["updatedAt"] = max(updated_values)
        keeper_roots = keeper.get("rootPaths")
        if isinstance(keeper_roots, list):
            normalized_roots = []
            for root in keeper_roots:
                normalized, _kind = _normalized_project_path(str(root or ""))
                normalized_roots.append(normalized or root)
            keeper["rootPaths"] = _dedupe_values(normalized_roots)

    stale_ids = {item["project_id"] for item in removable_projects}
    explicit_remove_ids = {item["project_id"] for item in removed_projects}
    all_removed_ids = stale_ids | explicit_remove_ids
    _rewrite_project_references(payload, remap, all_removed_ids)

    for project_id in duplicate_remove_ids:
        projects.pop(project_id, None)
    for item in removable_projects:
        project = projects.get(item["project_id"])
        if not isinstance(project, dict) or project.get("name") != item["project_name"]:
            raise ValueError(f"Project changed after scanning: {item['project_name']}")
        projects.pop(item["project_id"])
    for item in removed_projects:
        project = projects.get(item["project_id"])
        if not isinstance(project, dict) or project.get("name") != item["project_name"]:
            raise ValueError(f"Project changed after scanning: {item['project_name']}")
        projects.pop(item["project_id"])

    for item in project_repairs:
        project_id = remap.get(item["project_id"], item["project_id"])
        project = projects.get(project_id)
        roots = project.get("rootPaths") if isinstance(project, dict) else None
        index = item["root_index"]
        expected = item["raw_path"]
        if item["project_id"] in remap:
            continue
        if not isinstance(roots, list) or index >= len(roots) or roots[index] != expected:
            raise ValueError(f"Project changed after scanning: {item['project_name']}")
        roots[index] = item["normalized_path"]

    repointed = 0
    for item in repointed_projects:
        project_id = item["project_id"]
        project = projects.get(project_id)
        roots = project.get("rootPaths") if isinstance(project, dict) else None
        if not isinstance(roots, list) or roots != item["old_paths"]:
            raise ValueError(f"Project changed after scanning: {item['project_name']}")
        target_path = _validated_project_directory(item["new_path"])
        target_identity = _project_path_identity(target_path)
        for other_id, other_project in projects.items():
            if other_id == project_id:
                continue
            other_identity = _project_roots_identity(other_project)
            if other_identity and target_identity in other_identity:
                raise ValueError(
                    f"Target directory is already registered by project {other_id}; merge that registration instead"
                )
        project["rootPaths"] = [target_path]
        repointed += 1

    renamed = 0
    for item in renamed_projects:
        project = projects.get(item["project_id"])
        if not isinstance(project, dict):
            raise ValueError(f"Project changed after scanning: {item['project_id']}")
        expected_name = item["project_name"]
        if str(project.get("name") or item["project_id"]) != expected_name:
            raise ValueError(f"Project name changed after scanning: {expected_name}")
        new_name = _validated_project_display_name(item["new_name"])
        if new_name != expected_name:
            project["name"] = new_name
            renamed += 1

    for project_id, project in projects.items():
        if isinstance(project, dict):
            project["id"] = project_id
    return (
        len(project_repairs),
        len(duplicate_projects),
        len(removable_projects),
        len(removed_projects),
        renamed,
        repointed,
    )


def inspect_rollout_path_health(codex_home: Path) -> dict[str, Any]:
    """Inspect Codex-owned rollout paths without changing the database."""
    codex_home = codex_home.expanduser().resolve()
    database = migration_bundle.find_state_db(codex_home)
    if database is None:
        health = {
            "database": None,
            "extended_paths": [],
            "repairable_paths": [],
            "blocked_paths": [],
            "normalization_triggers": [],
        }
        health.update(_inspect_project_path_health(codex_home, {}))
        return health
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("pragma table_info(threads)")}
        if not {"id", "rollout_path"}.issubset(columns):
            raise ValueError("The Codex thread database does not expose rollout paths")
        rows = connection.execute("select id, rollout_path from threads order by id").fetchall()
        thread_rows = {row["id"]: dict(row) for row in connection.execute("select * from threads")}
        trigger_names = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='trigger'")
            if row[0] in ROLLOUT_PATH_NORMALIZE_TRIGGERS
        }
    finally:
        connection.close()

    extended_paths = []
    for row in rows:
        raw = str(row["rollout_path"] or "")
        normalized, kind = _normalized_extended_rollout_path(raw)
        if kind is None:
            continue
        item = {
            "task_id": row["id"],
            "raw_path": raw,
            "normalized_path": normalized,
            "kind": kind,
            "repairable": False,
            "reason": "",
        }
        if normalized is None:
            item["reason"] = "扩展路径格式无法安全识别"
        else:
            candidate = Path(normalized).expanduser()
            try:
                resolved = candidate.resolve()
            except (OSError, RuntimeError):
                resolved = candidate
            if not candidate.is_absolute():
                item["reason"] = "规范化结果不是绝对路径"
            elif not resolved.is_relative_to(codex_home):
                item["reason"] = "会话文件不在当前 Codex 数据目录内"
            elif not resolved.is_file():
                item["reason"] = "规范化后的会话文件不存在"
            else:
                item["normalized_path"] = str(resolved)
                item["repairable"] = True
                item["reason"] = "可安全规范化"
        extended_paths.append(item)
    health = {
        "database": str(database),
        "extended_paths": extended_paths,
        "repairable_paths": [item for item in extended_paths if item["repairable"]],
        "blocked_paths": [item for item in extended_paths if not item["repairable"]],
        "normalization_triggers": sorted(trigger_names),
    }
    health.update(_inspect_project_path_health(codex_home, thread_rows))
    return health


def repair_rollout_path_health(
    codex_home: Path,
    require_codex_closed: bool = True,
    remove_normalization_triggers: bool = True,
    selected_project_actions: dict[str, str] | None = None,
    selected_project_names: dict[str, str] | None = None,
    selected_project_paths: dict[str, str] | None = None,
    repair_conversation_paths: bool = True,
) -> dict[str, Any]:
    """Back up and repair only verified extended rollout paths."""
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before repairing rollout paths")
    codex_home = codex_home.expanduser().resolve()
    codex_compat.require_operation_supported(codex_home, "path_repair", "修复路径")
    health = inspect_rollout_path_health(codex_home)
    database_value = health.get("database")
    repairs = health["repairable_paths"]
    if not repair_conversation_paths:
        repairs = []
    triggers = health["normalization_triggers"] if remove_normalization_triggers else []
    project_repairs = health.get("repairable_project_paths", [])
    duplicate_projects = health.get("duplicate_projects", [])
    removable_projects = health.get("removable_projects", [])
    removed_projects: list[dict[str, Any]] = []
    renamed_projects: list[dict[str, Any]] = []
    repointed_projects: list[dict[str, Any]] = []
    registration_mode = bool(
        selected_project_actions
        and any(key.startswith("registration:") for key in selected_project_actions)
    )
    if registration_mode:
        selected_names = selected_project_names or {}
        selected_paths = selected_project_paths or {}
        registrations = {
            item["project_id"]: item
            for item in health.get("actionable_project_registrations", [])
        }
        requested_project_ids = {
            key.split(":", 1)[1]
            for key, action in selected_project_actions.items()
            if key.startswith("registration:") and action != "keep"
        }
        requested_project_ids.update(
            key.split(":", 1)[1]
            for key in (selected_project_names or {})
            if key.startswith("registration:")
        )
        requested_project_ids.update(
            key.split(":", 1)[1]
            for key in (selected_project_paths or {})
            if key.startswith("registration:")
        )
        missing_requested_ids = requested_project_ids - set(registrations)
        if missing_requested_ids:
            # Ordinary registrations are intentionally absent from the path
            # repair dialog, but deletion from Content Management still needs
            # to validate and process their IDs.
            global_state_value = health.get("global_state")
            database_available = bool(health.get("database"))
            if global_state_value:
                state_path = Path(global_state_value)
                try:
                    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    state_payload = {}
                state_projects = state_payload.get("local-projects", {})
                if isinstance(state_projects, dict):
                    current_thread_rows = _read_thread_rows(codex_home)
                    reference_paths = _project_reference_paths(
                        state_payload, {str(project_id) for project_id in state_projects}
                    )
                    for project_id in sorted(missing_requested_ids):
                        project = state_projects.get(project_id)
                        if not isinstance(project, dict):
                            continue
                        roots = []
                        for root_index, raw_value in enumerate(project.get("rootPaths", [])):
                            normalized, kind = _normalized_project_path(raw_value)
                            roots.append({
                                "raw_path": str(raw_value or ""),
                                "normalized_path": normalized,
                                "path_kind": kind or "ordinary",
                                "exists": bool(normalized and Path(normalized).is_dir()),
                                "root_index": root_index,
                            })
                        root_paths = [root["normalized_path"] or root["raw_path"] for root in roots]
                        related_rows = [
                            row for row in current_thread_rows.values()
                            if _path_belongs_to_project(row.get("cwd"), root_paths)
                        ]
                        unknown = [
                            path for path in reference_paths.get(project_id, [])
                            if not _known_project_reference(path, project_id)
                        ]
                        identity = _project_roots_identity(project)
                        shared_count = sum(
                            _project_roots_identity(other) == identity
                            for other_id, other in state_projects.items()
                            if str(other_id) != project_id and isinstance(other, dict)
                        ) + 1 if identity else 1
                        full_delete_enabled = bool(
                            database_available
                            and roots
                            and all(root["exists"] for root in roots)
                            and not unknown
                            and shared_count == 1
                        )
                        if full_delete_enabled:
                            try:
                                for root in roots:
                                    _validate_project_path(Path(root["normalized_path"] or root["raw_path"]))
                                for row in related_rows:
                                    rollout = migration_bundle.resolve_local_path(
                                        row.get("rollout_path"), codex_home
                                    )
                                    if not rollout.is_file() or not rollout.is_relative_to(codex_home):
                                        full_delete_enabled = False
                                        break
                                    _validate_project_path(Path(row.get("cwd") or ""))
                            except (OSError, RuntimeError, ValueError):
                                full_delete_enabled = False
                        registrations[project_id] = {
                            "project_id": project_id,
                            "project_name": str(project.get("name") or project_id),
                            "roots": roots,
                            "related_tasks": [{
                                "task_id": str(row.get("id") or ""),
                                "title": repair_mojibake(str(row.get("title") or row.get("id") or "")),
                            } for row in related_rows],
                            "unknown_references": unknown,
                            "capabilities": {
                                "delete": {
                                    "enabled": not unknown,
                                    "reason": "存在无法识别的项目引用" if unknown else "可删除该项目注册",
                                },
                                "full_delete": {
                                    "enabled": full_delete_enabled,
                                    "reason": (
                                        "可备份并删除关联对话、注册，并把项目目录移入可恢复区"
                                        if full_delete_enabled else
                                        "项目目录、关联对话或重复注册状态无法安全确认"
                                    ),
                                },
                            },
                        }
            missing_requested_ids = requested_project_ids - set(registrations)
        if missing_requested_ids:
            raise ValueError(
                "Project registration changed after scanning: "
                + ", ".join(sorted(missing_requested_ids))
            )
        for project_id in requested_project_ids:
            action = selected_project_actions.get(f"registration:{project_id}", "keep")
            capability_key = "rename" if project_id in {
                key.split(":", 1)[1] for key in (selected_project_names or {})
            } and action == "keep" else action
            capability = registrations[project_id].get("capabilities", {}).get(capability_key)
            if capability and not capability.get("enabled"):
                raise ValueError(str(capability.get("reason") or "Selected operation is no longer available"))
        actions_by_id = {
            project_id: selected_project_actions.get(f"registration:{project_id}", "keep")
            for project_id in registrations
        }
        project_repairs = []
        duplicate_projects = []
        removable_projects = []
        duplicate_member_ids: set[str] = set()

        def add_name_change(project_id: str) -> None:
            row = registrations[project_id]
            key = f"registration:{project_id}"
            requested_name = selected_names.get(key, row["project_name"])
            if requested_name != row["project_name"]:
                renamed_projects.append({
                    "project_id": project_id,
                    "project_name": row["project_name"],
                    "new_name": requested_name,
                })

        def add_repoint(project_id: str) -> None:
            row = registrations[project_id]
            key = f"registration:{project_id}"
            if key not in selected_paths:
                raise ValueError(f"Choose a replacement directory for project: {row['project_name']}")
            repointed_projects.append({
                "project_id": project_id,
                "project_name": row["project_name"],
                "old_paths": [root["raw_path"] for root in row.get("roots", [])],
                "new_path": _validated_project_directory(selected_paths[key]),
            })

        original_duplicate_groups = (
            health.get("duplicate_projects", [])
            + health.get("blocked_duplicate_projects", [])
        )
        for group in original_duplicate_groups:
            member_ids = list(group.get("member_ids") or [group["keeper_id"]] + group["remove_ids"])
            duplicate_member_ids.update(member_ids)
            kept_ids = [project_id for project_id in member_ids if actions_by_id.get(project_id) != "delete"]
            deleted_ids = [project_id for project_id in member_ids if actions_by_id.get(project_id) == "delete"]
            blocked_deleted_ids = set(deleted_ids) & set(group.get("unknown_references", {}))
            if blocked_deleted_ids:
                raise ValueError(
                    "Duplicate projects have unknown references and cannot be deleted: "
                    + ", ".join(sorted(blocked_deleted_ids))
                )
            if not kept_ids:
                removed_projects.extend({
                    "project_id": project_id,
                    "project_name": registrations[project_id]["project_name"],
                } for project_id in member_ids)
                continue
            for project_id in kept_ids:
                action = actions_by_id.get(project_id, "keep")
                if action == "repoint":
                    add_repoint(project_id)
                elif action == "normalize":
                    for root in registrations[project_id].get("roots", []):
                        if root.get("path_kind") != "ordinary" and root.get("normalized_path"):
                            project_repairs.append({
                                "project_id": project_id,
                                "project_name": registrations[project_id]["project_name"],
                                "root_index": registrations[project_id]["roots"].index(root),
                                "raw_path": root["raw_path"],
                                "normalized_path": root["normalized_path"],
                            })
                add_name_change(project_id)
            if deleted_ids:
                keeper_id = next(
                    (project_id for project_id in kept_ids if project_id == group["keeper_id"]),
                    kept_ids[0],
                )
                name_by_id = {
                    project_id: registrations[project_id]["project_name"] for project_id in member_ids
                }
                duplicate_projects.append({
                    **group,
                    "keeper_id": keeper_id,
                    "keeper_name": name_by_id[keeper_id],
                    "remove_ids": deleted_ids,
                    "remove_names": [name_by_id[project_id] for project_id in deleted_ids],
                })

        original_project_repairs = health.get("repairable_project_paths", [])
        repairs_by_id: dict[str, list[dict[str, Any]]] = {}
        for item in original_project_repairs:
            repairs_by_id.setdefault(item["project_id"], []).append(item)
        stale_by_id = {
            item["project_id"]: item for item in health.get("removable_projects", [])
        }
        for project_id, row in registrations.items():
            if project_id in duplicate_member_ids:
                continue
            action = actions_by_id.get(project_id, "keep")
            if action == "delete":
                source = stale_by_id.get(project_id)
                if source is not None:
                    removable_projects.append(source)
                else:
                    if row.get("unknown_references"):
                        raise ValueError(f"Project has unknown references and cannot be removed: {row['project_name']}")
                    removed_projects.append({
                        "project_id": project_id,
                        "project_name": row["project_name"],
                    })
                continue
            if action == "normalize":
                project_repairs.extend(repairs_by_id.get(project_id, []))
            elif action == "repoint":
                add_repoint(project_id)
            add_name_change(project_id)
    elif selected_project_actions is not None:
        selected_names = selected_project_names or {}
        selected_repairs = []
        seen_project_ids: set[str] = set()
        for item in project_repairs:
            project_id = item["project_id"]
            key = f"normalize:{project_id}"
            action = selected_project_actions.get(key, "ignore")
            if action == "normalize":
                selected_repairs.append(item)
            elif action == "remove" and project_id not in seen_project_ids:
                if item.get("unknown_references"):
                    raise ValueError(f"Project has unknown references and cannot be removed: {item['project_name']}")
                removed_projects.append({
                    "project_id": project_id,
                    "project_name": item["project_name"],
                })
            if action != "remove" and project_id not in seen_project_ids:
                requested_name = selected_names.get(key, item["project_name"])
                if requested_name != item["project_name"]:
                    renamed_projects.append({
                        "project_id": project_id,
                        "project_name": item["project_name"],
                        "new_name": requested_name,
                    })
            seen_project_ids.add(project_id)
        project_repairs = selected_repairs

        selected_duplicates = []
        for item in duplicate_projects:
            key = f"duplicate:{item['keeper_id']}"
            action = selected_project_actions.get(key, "ignore")
            member_ids = list(item.get("member_ids") or [item["keeper_id"]] + item["remove_ids"])
            member_names = list(item.get("member_names") or [item["keeper_name"]] + item["remove_names"])
            name_by_id = dict(zip(member_ids, member_names))
            if action.startswith("merge:"):
                keeper_id = action.split(":", 1)[1]
                if keeper_id not in member_ids:
                    raise ValueError(f"Selected duplicate keeper is no longer valid: {keeper_id}")
                adjusted = dict(item)
                adjusted["keeper_id"] = keeper_id
                adjusted["keeper_name"] = name_by_id[keeper_id]
                adjusted["remove_ids"] = [project_id for project_id in member_ids if project_id != keeper_id]
                adjusted["remove_names"] = [name_by_id[project_id] for project_id in adjusted["remove_ids"]]
                selected_duplicates.append(adjusted)
                requested_name = selected_names.get(key, adjusted["keeper_name"])
                if requested_name != adjusted["keeper_name"]:
                    renamed_projects.append({
                        "project_id": keeper_id,
                        "project_name": adjusted["keeper_name"],
                        "new_name": requested_name,
                    })
            elif action == "remove":
                if item.get("unknown_references"):
                    raise ValueError(f"Duplicate projects have unknown references and cannot be removed: {item['keeper_name']}")
                removed_projects.extend(
                    {"project_id": project_id, "project_name": name_by_id[project_id]}
                    for project_id in member_ids
                )
        duplicate_projects = selected_duplicates

        removable_projects = [
            item for item in removable_projects
            if selected_project_actions.get(f"stale:{item['project_id']}") == "remove"
        ]
    blocked_project_count = (
        len(health.get("blocked_project_paths", []))
        + len(health.get("blocked_duplicate_projects", []))
    )
    if not any((
        repairs,
        triggers,
        project_repairs,
        duplicate_projects,
        removable_projects,
        removed_projects,
        renamed_projects,
        repointed_projects,
    )):
        return {
            "backup_path": None,
            "repaired": 0,
            "blocked": len(health["blocked_paths"]),
            "triggers_removed": [],
            "project_paths_repaired": 0,
            "duplicate_projects_merged": 0,
            "stale_projects_removed": 0,
            "project_registrations_removed": 0,
            "project_names_changed": 0,
            "project_paths_repointed": 0,
            "project_paths_blocked": blocked_project_count,
        }

    database = Path(database_value) if database_value else None
    global_state_value = health.get("global_state")
    global_state = Path(global_state_value) if global_state_value else None
    backup_paths = []
    if database is not None and (repairs or triggers):
        backup_paths.append(database)
    if global_state is not None and any((
        project_repairs,
        duplicate_projects,
        removable_projects,
        removed_projects,
        renamed_projects,
        repointed_projects,
    )):
        backup_paths.append(global_state)
    backup_root, backed_up = _backup_selected_files(codex_home, backup_paths, "path-health-repair")
    descriptor, lock_path = migration_bundle.acquire_lock(codex_home)
    try:
        _write_transaction(
            codex_home,
            backup_root,
            backed_up,
            "rollout_path_repair",
            "in_progress",
            repairs=repairs,
            triggers_to_remove=triggers,
            blocked_paths=health["blocked_paths"],
            project_repairs=project_repairs,
            duplicate_projects=duplicate_projects,
            removable_projects=removable_projects,
            removed_projects=removed_projects,
            renamed_projects=renamed_projects,
            repointed_projects=repointed_projects,
            blocked_project_paths=health.get("blocked_project_paths", []),
            blocked_duplicate_projects=health.get("blocked_duplicate_projects", []),
        )
        if repairs or triggers:
            if database is None:
                raise ValueError("Codex thread database was not found")
            connection = sqlite3.connect(database, timeout=10)
            try:
                connection.execute("begin immediate")
                for item in repairs:
                    cursor = connection.execute(
                        "update threads set rollout_path=? where id=? and rollout_path=?",
                        (item["normalized_path"], item["task_id"], item["raw_path"]),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError(f"Conversation changed after scanning: {item['task_id']}")
                for trigger in triggers:
                    if trigger not in ROLLOUT_PATH_NORMALIZE_TRIGGERS:
                        raise ValueError(f"Refusing to remove an unknown trigger: {trigger}")
                    connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        if any((
            project_repairs,
            duplicate_projects,
            removable_projects,
            removed_projects,
            renamed_projects,
            repointed_projects,
        )):
            if global_state is None:
                raise ValueError("Codex global project state was not found")
            current_state = global_state.read_bytes()
            if hashlib.sha256(current_state).hexdigest() != health.get("global_state_sha256"):
                raise ValueError("Codex global project state changed after scanning")
            payload = json.loads(current_state.decode("utf-8"))
            _apply_project_registry_repairs(
                payload,
                duplicate_projects,
                project_repairs,
                removable_projects,
                removed_projects,
                renamed_projects,
                repointed_projects,
            )
            migration_bundle.atomic_write(
                global_state,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            )

        verified = inspect_rollout_path_health(codex_home)
        repaired_ids = {item["task_id"] for item in repairs}
        remaining_ids = {item["task_id"] for item in verified["extended_paths"]}
        if repaired_ids & remaining_ids:
            raise ValueError("Rollout path repair verification failed")
        if set(triggers) & set(verified["normalization_triggers"]):
            raise ValueError("Normalization trigger removal verification failed")
        remaining_project_raw_paths = {
            item["raw_path"] for item in verified.get("project_extended_paths", [])
        }
        if {item["raw_path"] for item in project_repairs} & remaining_project_raw_paths:
            raise ValueError("Project path repair verification failed")
        remaining_project_ids = {
            item["project_id"] for item in verified.get("project_extended_paths", [])
        }
        if {item["project_id"] for item in removable_projects} & remaining_project_ids:
            raise ValueError("Stale project removal verification failed")
        removed_project_ids = {
            project_id
            for group in duplicate_projects
            for project_id in group["remove_ids"]
        } | {
            item["project_id"] for item in removable_projects + removed_projects
        }
        if global_state is not None and removed_project_ids:
            verified_payload = json.loads(global_state.read_text(encoding="utf-8"))
            remaining_references = _project_reference_paths(verified_payload, removed_project_ids)
            if any(remaining_references.values()):
                raise ValueError("Project reference merge verification failed")
        merged_id_sets = {frozenset(group["remove_ids"] + [group["keeper_id"]]) for group in duplicate_projects}
        for group in verified.get("duplicate_projects", []) + verified.get("blocked_duplicate_projects", []):
            if frozenset(group["remove_ids"] + [group["keeper_id"]]) in merged_id_sets:
                raise ValueError("Duplicate project merge verification failed")
        if global_state is not None and renamed_projects:
            verified_payload = json.loads(global_state.read_text(encoding="utf-8"))
            verified_projects = verified_payload.get("local-projects", {})
            for item in renamed_projects:
                project = verified_projects.get(item["project_id"])
                if not isinstance(project, dict) or project.get("name") != _validated_project_display_name(item["new_name"]):
                    raise ValueError("Project display name verification failed")
        if global_state is not None and repointed_projects:
            verified_payload = json.loads(global_state.read_text(encoding="utf-8"))
            verified_projects = verified_payload.get("local-projects", {})
            for item in repointed_projects:
                project = verified_projects.get(item["project_id"])
                if not isinstance(project, dict) or project.get("rootPaths") != [
                    _validated_project_directory(item["new_path"])
                ]:
                    raise ValueError("Project directory correction verification failed")
        _write_transaction(
            codex_home,
            backup_root,
            backed_up,
            "rollout_path_repair",
            "committed",
            repairs=repairs,
            repaired=len(repairs),
            blocked_paths=verified["blocked_paths"],
            triggers_removed=triggers,
            project_repairs=project_repairs,
            project_paths_repaired=len(project_repairs),
            duplicate_projects=duplicate_projects,
            duplicate_projects_merged=len(duplicate_projects),
            stale_projects_removed=len(removable_projects),
            removed_projects=removed_projects,
            project_registrations_removed=len(removed_projects),
            renamed_projects=renamed_projects,
            project_names_changed=len(renamed_projects),
            repointed_projects=repointed_projects,
            project_paths_repointed=len(repointed_projects),
            blocked_project_paths=verified.get("blocked_project_paths", []),
            blocked_duplicate_projects=verified.get("blocked_duplicate_projects", []),
        )
        return {
            "backup_path": str(backup_root),
            "repaired": len(repairs),
            "blocked": len(verified["blocked_paths"]),
            "triggers_removed": triggers,
            "project_paths_repaired": len(project_repairs),
            "duplicate_projects_merged": len(duplicate_projects),
            "stale_projects_removed": len(removable_projects),
            "project_registrations_removed": len(removed_projects),
            "project_names_changed": len(renamed_projects),
            "project_paths_repointed": len(repointed_projects),
            "project_paths_blocked": (
                len(verified.get("blocked_project_paths", []))
                + len(verified.get("blocked_duplicate_projects", []))
            ),
        }
    except Exception:
        for target, backup in reversed(backed_up):
            if target.suffix == ".sqlite":
                migration_bundle.backup_database(backup, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
        raise
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def fully_delete_registered_project(
    codex_home: Path,
    project_id: str,
    require_codex_closed: bool = True,
) -> dict[str, Any]:
    """Remove one safe project registration, its conversations, and its directories recoverably."""
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before deleting a project")
    codex_home = codex_home.expanduser().resolve()
    codex_compat.require_operation_supported(
        codex_home, "full_project_delete", "彻底删除项目"
    )
    health = inspect_rollout_path_health(codex_home)
    registration = next(
        (
            item for item in health.get("actionable_project_registrations", [])
            if item["project_id"] == project_id
        ),
        None,
    )
    if registration is None:
        global_state_value = health.get("global_state")
        database_available = bool(health.get("database"))
        if global_state_value:
            try:
                state_payload = json.loads(Path(global_state_value).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                state_payload = {}
            project = state_payload.get("local-projects", {}).get(project_id)
            if isinstance(project, dict):
                current_rows = _read_thread_rows(codex_home)
                roots = []
                for root_index, raw_value in enumerate(project.get("rootPaths", [])):
                    normalized, kind = _normalized_project_path(raw_value)
                    roots.append({
                        "raw_path": str(raw_value or ""),
                        "normalized_path": normalized,
                        "path_kind": kind or "ordinary",
                        "exists": bool(normalized and Path(normalized).is_dir()),
                        "root_index": root_index,
                    })
                root_paths = [root["normalized_path"] or root["raw_path"] for root in roots]
                related_rows = [
                    row for row in current_rows.values()
                    if _path_belongs_to_project(row.get("cwd"), root_paths)
                ]
                reference_paths = _project_reference_paths(
                    state_payload, {str(value) for value in state_payload.get("local-projects", {})}
                )
                unknown = [
                    path for path in reference_paths.get(project_id, [])
                    if not _known_project_reference(path, project_id)
                ]
                identity = _project_roots_identity(project)
                shared_count = sum(
                    _project_roots_identity(other) == identity
                    for other_id, other in state_payload.get("local-projects", {}).items()
                    if str(other_id) != project_id and isinstance(other, dict)
                ) + 1 if identity else 1
                full_delete_enabled = bool(
                    database_available
                    and roots
                    and all(root["exists"] for root in roots)
                    and not unknown
                    and shared_count == 1
                )
                if full_delete_enabled:
                    try:
                        for root in roots:
                            _validate_project_path(Path(root["normalized_path"] or root["raw_path"]))
                        for row in related_rows:
                            rollout = migration_bundle.resolve_local_path(row.get("rollout_path"), codex_home)
                            if not rollout.is_file() or not rollout.is_relative_to(codex_home):
                                full_delete_enabled = False
                                break
                    except (OSError, RuntimeError, ValueError):
                        full_delete_enabled = False
                registration = {
                    "project_id": project_id,
                    "project_name": str(project.get("name") or project_id),
                    "roots": roots,
                    "related_tasks": [{
                        "task_id": str(row.get("id") or ""),
                        "title": repair_mojibake(str(row.get("title") or row.get("id") or "")),
                    } for row in related_rows],
                    "capabilities": {
                        "full_delete": {
                            "enabled": full_delete_enabled,
                            "reason": (
                                "可备份并删除关联对话、注册，并把项目目录移入可恢复区"
                                if full_delete_enabled else
                                "项目目录、关联对话或重复注册状态无法安全确认"
                            ),
                        },
                    },
                }
    if registration is None:
        raise ValueError("Project registration changed after scanning; rescan before deleting")
    capability = registration.get("capabilities", {}).get("full_delete", {})
    if not capability.get("enabled"):
        raise ValueError(str(capability.get("reason") or "Project cannot be deleted safely"))
    roots = [
        _validate_project_path(Path(root["normalized_path"] or root["raw_path"]))
        for root in registration.get("roots", [])
    ]
    task_ids = {
        str(item["task_id"]) for item in registration.get("related_tasks", [])
        if item.get("task_id")
    }
    registration_backup = None
    conversation_backup = None
    trashed_items: list[str] = []
    try:
        registry_result = repair_rollout_path_health(
            codex_home,
            require_codex_closed=False,
            remove_normalization_triggers=False,
            selected_project_actions={f"registration:{project_id}": "delete"},
            repair_conversation_paths=False,
        )
        registration_backup = registry_result.get("backup_path")
        if registry_result.get("project_registrations_removed", 0) != 1:
            raise ValueError("Project registration deletion verification failed")
        if task_ids:
            conversation_result = delete_conversations(
                codex_home, task_ids, require_codex_closed=False
            )
            conversation_backup = conversation_result.get("backup_path")
        trash_result = move_projects_to_trash(roots, require_codex_closed=False)
        trashed_items = list(trash_result.get("items", []))
        for item_value in trashed_items:
            manifest_path = Path(item_value) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update({
                "operation": "full_project_delete",
                "codex_home": str(codex_home),
                "project_id": project_id,
                "registration_backup": registration_backup,
                "conversation_backup": conversation_backup,
                "task_ids": sorted(task_ids),
            })
            migration_bundle.atomic_write(
                manifest_path,
                (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            )
        verified_state = json.loads((codex_home / GLOBAL_STATE_FILE_NAME).read_text(encoding="utf-8"))
        if project_id in verified_state.get("local-projects", {}):
            raise ValueError("Project registration still exists after deletion")
        if task_ids and migration_bundle.read_sqlite_threads(codex_home, task_ids):
            raise ValueError("Project conversations still exist after deletion")
        if any(path.exists() for path in roots):
            raise ValueError("Project directory still exists after deletion")
        return {
            "project_id": project_id,
            "deleted_conversations": len(task_ids),
            "registration_backup": registration_backup,
            "conversation_backup": conversation_backup,
            "trash_items": trashed_items,
            "trash_root": trash_result["trash_root"],
        }
    except Exception:
        for item_value in reversed(trashed_items):
            try:
                restore_project(Path(item_value), require_codex_closed=False)
            except Exception:
                pass
        if conversation_backup:
            try:
                migration_bundle.restore_backup(
                    Path(conversation_backup), codex_home, require_codex_closed=False
                )
            except Exception:
                pass
        if registration_backup:
            try:
                migration_bundle.restore_backup(
                    Path(registration_backup), codex_home, require_codex_closed=False
                )
            except Exception:
                pass
        raise


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
    compatibility = codex_compat.inspect_codex_storage(codex_home)
    native_projects = codex_compat.read_native_projects(codex_home)
    native_projects_by_id = {
        str(project["project_id"]): project for project in native_projects
    }
    database_rows = _read_thread_rows(codex_home)
    path_health = inspect_rollout_path_health(codex_home)
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
        project_id = str(row.get("project_id") or "").strip()
        native_project = native_projects_by_id.get(project_id)
        if native_project:
            native_roots = [str(value) for value in native_project.get("roots", [])]
            project_path = str(native_project.get("primary_root") or "")
            if not project_path:
                project_path, _fallback_name = project_identity_from_cwd(cwd_value)
            project_name = str(native_project.get("project_name") or project_id)
        else:
            native_roots = []
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
            "project_id": project_id,
            "project_roots": native_roots,
            "is_pinned": bool(row.get("is_pinned")),
            "thread_section_id": str(row.get("thread_section_id") or ""),
            "section_position": row.get("section_position"),
            "history_mode": str(row.get("history_mode") or "legacy"),
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
                    "project_id": project_id,
                    "native_project_ids": [],
                    "project_roots": [],
                    "storage_sources": [],
                },
            )
            if project_id and native_project:
                if project_id not in project["native_project_ids"]:
                    project["native_project_ids"].append(project_id)
                project["project_id"] = project_id
                project["project_name"] = project_name
                project["registered"] = True
                project["storage_sources"] = list(dict.fromkeys(
                    [*project.get("storage_sources", []), "state_db"]
                ))
                project["project_roots"] = list(dict.fromkeys(
                    [*project.get("project_roots", []), *native_roots]
                ))
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
    # The sidebar project registry is authoritative for project existence, even
    # when a registered project has no conversation rows yet. Merge it with
    # cwd-derived projects by normalized path so empty or newly imported
    # projects remain visible in content management.
    registered_by_identity: dict[str, list[dict[str, Any]]] = {}
    for registration in path_health.get("registered_projects", []):
        path = str(registration.get("path") or "").strip()
        identity = _project_path_identity(path)
        if identity:
            registered_by_identity.setdefault(identity, []).append(registration)

    project_by_identity: dict[str, dict[str, Any]] = {}
    for project in projects.values():
        identity = _project_path_identity(project.get("path"))
        if identity:
            project_by_identity[identity] = project
        project.setdefault("registered", False)
        project.setdefault("registration_ids", [])
        project.setdefault("registration_names", [])
        project.setdefault("registration_statuses", [])
        project.setdefault("native_project_ids", [])
        project.setdefault("project_roots", [])
        project.setdefault("storage_sources", [])

    for identity, registrations in registered_by_identity.items():
        project = project_by_identity.get(identity)
        if project is None:
            first = registrations[0]
            path = str(first.get("path") or "")
            project = {
                "path": path,
                "thread_ids": [],
                "thread_count": 0,
                "conversation_bytes": 0,
                "image_bytes": 0,
                "latest_updated_at": "",
                "exists": bool(first.get("exists")),
            }
            projects[path] = project
            project_by_identity[identity] = project
        project["registered"] = True
        project["storage_sources"] = list(dict.fromkeys(
            [*project.get("storage_sources", []), "global_state"]
        ))
        project["registration_ids"] = list(dict.fromkeys(
            [*project.get("registration_ids", []), *[
                str(item.get("project_id") or "") for item in registrations
            ]]
        ))
        project["registration_names"] = list(dict.fromkeys(
            [*project.get("registration_names", []), *[
                str(item.get("project_name") or "") for item in registrations
            ]]
        ))
        project["registration_statuses"] = list(dict.fromkeys(
            [*project.get("registration_statuses", []), *[
                str(item.get("path_status") or "") for item in registrations
            ]]
        ))
        project["exists"] = any(bool(item.get("exists")) for item in registrations)
        if not project.get("project_name"):
            project["project_name"] = str(registrations[0].get("project_name") or "")

    # First-class projects can exist before any thread is assigned to them and
    # can contain more than one root. Keep these projects visible without
    # pretending that their IDs are legacy global-state registration IDs.
    for native_project in native_projects:
        roots = [str(value) for value in native_project.get("roots", [])]
        primary_root = str(native_project.get("primary_root") or "")
        identity = _project_path_identity(primary_root)
        project = project_by_identity.get(identity) if identity else None
        if project is None:
            project_key = primary_root or f"@native:{native_project['project_id']}"
            project = {
                "path": primary_root,
                "thread_ids": [],
                "thread_count": 0,
                "conversation_bytes": 0,
                "image_bytes": 0,
                "latest_updated_at": "",
                "exists": bool(primary_root) and Path(primary_root).expanduser().is_dir(),
            }
            projects[project_key] = project
            if identity:
                project_by_identity[identity] = project
        native_id = str(native_project["project_id"])
        project["registered"] = True
        project["project_id"] = native_id
        project["project_name"] = str(native_project.get("project_name") or native_id)
        project["native_project_ids"] = list(dict.fromkeys(
            [*project.get("native_project_ids", []), native_id]
        ))
        project["project_roots"] = list(dict.fromkeys(
            [*project.get("project_roots", []), *roots]
        ))
        project["storage_sources"] = list(dict.fromkeys(
            [*project.get("storage_sources", []), "state_db"]
        ))
        project.setdefault("registration_ids", [])
        project.setdefault("registration_names", [])
        project.setdefault("registration_statuses", [])

    for project in projects.values():
        project.setdefault("project_name", project_name_from_path(str(project.get("path") or "")))
    project_values = list(projects.values())
    project_groups: dict[str, list[dict[str, Any]]] = {}
    for item in project_values:
        name = (Path(item["path"]).name or item.get("project_name") or item.get("project_id") or "").casefold()
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
        "path_health": path_health,
        "compatibility": compatibility,
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
            "extended_rollout_paths": len(path_health["extended_paths"]),
            "repairable_rollout_paths": len(path_health["repairable_paths"]),
            "rollout_path_triggers": len(path_health["normalization_triggers"]),
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
    codex_compat.require_operation_supported(
        codex_home, "conversation_content", "清理图片"
    )
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


def archive_projects(
    codex_home: Path,
    projects: list[dict[str, Any]],
    require_codex_closed: bool = True,
) -> dict[str, Any]:
    """Archive project folders and remove their sidebar registrations, preserving conversations."""
    if require_codex_closed and migration_bundle.codex_is_running():
        raise ValueError("Codex is running. Close Codex completely before archiving projects")
    codex_home = codex_home.expanduser().resolve()
    codex_compat.require_operation_supported(
        codex_home, "project_registry", "项目移入回收区"
    )
    selected = []
    registration_ids: list[str] = []
    for item in projects:
        path = _validate_project_path(Path(str(item.get("path") or "")))
        selected.append(path)
        registration_ids.extend(str(value) for value in item.get("registration_ids", []) if value)
    selected = sorted(set(selected), key=lambda path: len(path.parts))
    registration_ids = list(dict.fromkeys(registration_ids))
    for index, parent in enumerate(selected):
        if any(child.is_relative_to(parent) for child in selected[index + 1:]):
            raise ValueError("Do not archive both a project directory and one of its subdirectories")

    registration_backup = None
    moved_items: list[str] = []
    try:
        if registration_ids:
            registry_result = repair_rollout_path_health(
                codex_home,
                require_codex_closed=False,
                remove_normalization_triggers=False,
                selected_project_actions={
                    f"registration:{project_id}": "delete" for project_id in registration_ids
                },
                repair_conversation_paths=False,
            )
            registration_backup = registry_result.get("backup_path")
            if registry_result.get("project_registrations_removed", 0) != len(registration_ids):
                raise ValueError("Project registration archive verification failed")
        trash_result = move_projects_to_trash(selected, require_codex_closed=False)
        moved_items = list(trash_result.get("items", []))
        for item_value in moved_items:
            manifest_path = Path(item_value) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update({
                "operation": "project_archive",
                "codex_home": str(codex_home),
                "registration_backup": registration_backup,
                "registration_ids": registration_ids,
            })
            migration_bundle.atomic_write(
                manifest_path,
                (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            )
        return {
            "archived": len(moved_items),
            "registration_removed": len(registration_ids),
            "registration_backup": registration_backup,
            "trash_root": trash_result["trash_root"],
            "items": moved_items,
        }
    except Exception:
        for item_value in reversed(moved_items):
            try:
                restore_project(Path(item_value), require_codex_closed=False)
            except Exception:
                pass
        if registration_backup:
            try:
                migration_bundle.restore_backup(
                    Path(registration_backup), codex_home, require_codex_closed=False
                )
            except Exception:
                pass
        raise


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
    codex_compat.require_operation_supported(
        codex_home, "thread_lifecycle", "更改对话归档状态"
    )
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
    codex_compat.require_operation_supported(
        codex_home, "thread_lifecycle", "删除对话"
    )
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
    codex_compat.require_operation_supported(
        codex_home, "sidebar_cleanup", "清理侧栏残留"
    )
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


def restore_project(
    item_root: Path,
    require_codex_closed: bool = True,
    codex_home: Path | None = None,
) -> dict[str, Any]:
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
    operation = manifest.get("operation")
    full_delete = operation == "full_project_delete"
    restore_registration = operation in {"full_project_delete", "project_archive"}
    if codex_home is not None and restore_registration:
        codex_compat.require_operation_supported(
            codex_home, "project_registry", "恢复项目注册"
        )
        if full_delete:
            codex_compat.require_operation_supported(
                codex_home, "conversation_import", "恢复项目对话"
            )
    if full_delete and target.exists():
        raise ValueError("The original project directory already exists; full-project recovery cannot overwrite it")
    if target.exists():
        target = target.with_name(f"{target.name}-restored-{_now_stamp()}")
    target.parent.mkdir(parents=True, exist_ok=True)
    guard_backups: list[str] = []
    shutil.move(str(source), str(target))
    try:
        if restore_registration:
            effective_codex_home = (
                codex_home.expanduser().resolve()
                if codex_home is not None else
                Path(manifest["codex_home"]).expanduser().resolve()
            )
            if str(effective_codex_home) != str(Path(manifest["codex_home"]).expanduser().resolve()):
                raise ValueError("This project deletion belongs to a different Codex data directory")
            backup_keys = ("conversation_backup", "registration_backup") if full_delete else ("registration_backup",)
            for backup_key in backup_keys:
                backup_value = manifest.get(backup_key)
                if not backup_value:
                    continue
                restored = migration_bundle.restore_backup(
                    Path(backup_value), effective_codex_home, require_codex_closed=False
                )
                guard_backups.append(restored["safety_backup_path"])
        manifest["status"] = "restored"
        manifest["restored_path"] = str(target)
        manifest["restored_at"] = migration_bundle.now_iso()
        migration_bundle.atomic_write(
            manifest_path,
            (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        return {
            "restored_path": str(target),
            "full_project_restored": full_delete,
            "registration_restored": restore_registration and bool(manifest.get("registration_backup")),
            "restored_layers": len(guard_backups),
        }
    except Exception:
        effective_codex_home = codex_home.expanduser().resolve() if codex_home is not None else None
        if effective_codex_home is not None:
            for backup_value in reversed(guard_backups):
                try:
                    migration_bundle.restore_backup(
                        Path(backup_value), effective_codex_home, require_codex_closed=False
                    )
                except Exception:
                    pass
        if target.exists() and not source.exists():
            shutil.move(str(target), str(source))
        raise

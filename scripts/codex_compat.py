#!/usr/bin/env python3
"""Versioned, read-only inspection of Codex local storage capabilities."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


KNOWN_STATE_SCHEMA_MAX = 50
KNOWN_HISTORY_SCHEMA_MAX = 4
GLOBAL_STATE_FILE_NAME = ".codex-global-state.json"


def _state_database(codex_home: Path) -> Path | None:
    candidates = sorted(codex_home.glob("state_*.sqlite"), reverse=True)
    return candidates[0] if candidates else None


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "select name from sqlite_master where type='table'"
        )
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'pragma table_info("{table}")')}


def _migration_version(connection: sqlite3.Connection, tables: set[str]) -> int | None:
    if "_sqlx_migrations" not in tables:
        return None
    row = connection.execute(
        "select max(version) from _sqlx_migrations where success=1"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _row_count(connection: sqlite3.Connection, table: str, tables: set[str]) -> int:
    if table not in tables:
        return 0
    return int(connection.execute(f'select count(*) from "{table}"').fetchone()[0])


def _global_project_count(codex_home: Path) -> int:
    path = codex_home / GLOBAL_STATE_FILE_NAME
    if not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return 0
    projects = payload.get("local-projects") if isinstance(payload, dict) else None
    return len(projects) if isinstance(projects, dict) else 0


def read_native_projects(codex_home: Path) -> list[dict[str, Any]]:
    """Read first-class projects without mutating or migrating their database."""
    codex_home = codex_home.expanduser().resolve()
    database = _state_database(codex_home)
    if database is None:
        return []
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = _table_names(connection)
        if not {"projects", "project_roots"}.issubset(tables):
            return []
        project_columns = _table_columns(connection, "projects")
        required = {"id", "name", "position", "created_at_ms", "updated_at_ms"}
        if not required.issubset(project_columns):
            return []
        rows = connection.execute(
            "select * from projects order by position, created_at_ms, id"
        ).fetchall()
        projects = []
        for row in rows:
            roots = [
                str(root[0])
                for root in connection.execute(
                    "select path from project_roots where project_id=? order by position",
                    (row["id"],),
                )
            ]
            metadata: Any = {}
            if "metadata" in row.keys():
                try:
                    metadata = json.loads(str(row["metadata"] or "{}"))
                except (ValueError, TypeError):
                    metadata = {}
            projects.append(
                {
                    "project_id": str(row["id"]),
                    "project_name": str(row["name"]),
                    "roots": roots,
                    "primary_root": roots[0] if roots else "",
                    "position": int(row["position"]),
                    "created_at_ms": int(row["created_at_ms"]),
                    "updated_at_ms": int(row["updated_at_ms"]),
                    "metadata": metadata if isinstance(metadata, dict) else {},
                    "storage_source": "state_db",
                }
            )
        return projects
    finally:
        connection.close()


def inspect_codex_storage(codex_home: Path) -> dict[str, Any]:
    """Return the local schema profile and whether known offline writes are safe."""
    codex_home = codex_home.expanduser().resolve()
    warnings: list[str] = []
    blockers: list[str] = []
    database = _state_database(codex_home)
    state_version = None
    state_tables: set[str] = set()
    thread_columns: set[str] = set()
    native_project_count = 0
    native_project_schema = False

    if database is None:
        warnings.append("尚未发现 Codex 状态数据库；按未初始化或旧版目录处理")
    else:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            state_tables = _table_names(connection)
            state_version = _migration_version(connection, state_tables)
            if "threads" in state_tables:
                thread_columns = _table_columns(connection, "threads")
            else:
                blockers.append("状态数据库缺少 threads 表")
            required_thread_columns = {"id", "rollout_path", "cwd", "archived"}
            if thread_columns and not required_thread_columns.issubset(thread_columns):
                blockers.append("threads 表缺少工具写入所需的核心字段")
            if state_version is not None and state_version > KNOWN_STATE_SCHEMA_MAX:
                blockers.append(
                    f"状态数据库协议 {state_version} 高于已验证上限 {KNOWN_STATE_SCHEMA_MAX}"
                )
            native_project_schema = {"projects", "project_roots"}.issubset(state_tables)
            native_project_count = _row_count(connection, "projects", state_tables)
        finally:
            connection.close()

    history_database = codex_home / "thread_history_1.sqlite"
    history_version = None
    history_turns = 0
    history_items = 0
    if history_database.is_file():
        connection = sqlite3.connect(
            f"file:{history_database.as_posix()}?mode=ro", uri=True
        )
        try:
            history_tables = _table_names(connection)
            history_version = _migration_version(connection, history_tables)
            history_turns = _row_count(connection, "thread_turns", history_tables)
            history_items = _row_count(connection, "thread_items", history_tables)
            if history_version is not None and history_version > KNOWN_HISTORY_SCHEMA_MAX:
                blockers.append(
                    f"分页历史协议 {history_version} 高于已验证上限 {KNOWN_HISTORY_SCHEMA_MAX}"
                )
        finally:
            connection.close()

    global_project_count = _global_project_count(codex_home)
    if native_project_count and global_project_count:
        project_storage_mode = "dual"
        warnings.append("同时检测到全局项目注册和新版项目表，扫描时将合并去重")
    elif native_project_count:
        project_storage_mode = "state_db"
    elif native_project_schema and global_project_count:
        project_storage_mode = "transitioning"
        warnings.append("新版项目表已创建但项目仍由全局注册表提供，当前处于过渡存储模式")
    elif global_project_count:
        project_storage_mode = "global_state"
    elif native_project_schema:
        project_storage_mode = "state_db_empty"
    else:
        project_storage_mode = "legacy_or_empty"

    if state_version is None and database is not None:
        warnings.append("状态数据库没有迁移版本记录，将按字段能力兼容")

    write_compatible = not blockers
    paginated_history = history_database.is_file()
    operation_capabilities = {
        "path_repair": write_compatible,
        "sidebar_cleanup": write_compatible,
        # Editing or deleting only rollout/state rows would leave the new
        # paginated history projection stale. These operations must move to
        # the matching app-server protocol before they are re-enabled.
        "conversation_content": write_compatible and not paginated_history,
        "thread_lifecycle": write_compatible and not paginated_history,
        "conversation_import": write_compatible and not paginated_history,
        # Legacy global-state registration remains coherent only while the
        # first-class project tables have not started receiving records.
        "project_registry": write_compatible and native_project_count == 0,
    }
    operation_capabilities["full_project_delete"] = (
        operation_capabilities["project_registry"]
        and operation_capabilities["thread_lifecycle"]
    )
    capability_reasons = {
        "conversation_content": (
            "检测到分页历史库，直接改写 rollout 会留下过期历史投影"
            if paginated_history else ""
        ),
        "thread_lifecycle": (
            "检测到分页历史库，对话归档和删除必须改用同版本 App Server"
            if paginated_history else ""
        ),
        "conversation_import": (
            "检测到分页历史库，尚未验证导入后历史投影的官方重建流程"
            if paginated_history else ""
        ),
        "project_registry": (
            "检测到新版项目表已有项目，不能继续只修改全局项目注册表"
            if native_project_count else ""
        ),
    }
    capability_reasons["full_project_delete"] = "；".join(
        reason for reason in (
            capability_reasons["project_registry"],
            capability_reasons["thread_lifecycle"],
        ) if reason
    )
    status = "read_only" if not write_compatible else (
        "partial" if not all(operation_capabilities.values()) else "supported"
    )
    return {
        "status": status,
        "write_compatible": write_compatible,
        "blockers": blockers,
        "warnings": warnings,
        "state_database": str(database) if database else None,
        "state_schema_version": state_version,
        "known_state_schema_max": KNOWN_STATE_SCHEMA_MAX,
        "thread_columns": sorted(thread_columns),
        "native_project_schema": native_project_schema,
        "native_project_count": native_project_count,
        "global_project_count": global_project_count,
        "project_storage_mode": project_storage_mode,
        "history_database": str(history_database) if history_database.is_file() else None,
        "history_schema_version": history_version,
        "known_history_schema_max": KNOWN_HISTORY_SCHEMA_MAX,
        "history_turns": history_turns,
        "history_items": history_items,
        "features": {
            "project_id": "project_id" in thread_columns,
            "thread_pinning": "is_pinned" in thread_columns,
            "thread_sections": "thread_sections" in state_tables,
            "spawn_edges": "thread_spawn_edges" in state_tables,
            "paginated_history": paginated_history,
        },
        "operation_capabilities": operation_capabilities,
        "capability_reasons": capability_reasons,
        "preferred_mutation_backend": "app_server",
    }


def require_write_compatible(codex_home: Path, operation: str) -> dict[str, Any]:
    profile = inspect_codex_storage(codex_home)
    if not profile["write_compatible"]:
        detail = "；".join(profile["blockers"])
        raise RuntimeError(
            f"当前 Codex 存储协议尚未通过写入验证，已阻止“{operation}”：{detail}。"
            "仍可使用扫描、预览和一致性报告。"
        )
    return profile


def require_operation_supported(
    codex_home: Path,
    capability: str,
    operation: str,
) -> dict[str, Any]:
    profile = require_write_compatible(codex_home, operation)
    supported = profile.get("operation_capabilities", {}).get(capability, False)
    if not supported:
        reason = profile.get("capability_reasons", {}).get(capability) or "该写入路径尚未通过验证"
        raise RuntimeError(
            f"已阻止“{operation}”：{reason}。仍可使用扫描、预览和一致性报告。"
        )
    return profile

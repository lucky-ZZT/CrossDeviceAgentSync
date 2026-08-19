#!/usr/bin/env python3
"""Repair the confirmed 2026-08-18 half-archived Codex task."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import content_manager
import migration_bundle


TASK_ID = "019fcd5a-0060-7602-9c1e-decb68eff0b4"
EXPECTED_ROLLOUT_NAME = (
    "rollout-2026-08-04T23-17-35-019fcd5a-0060-7602-9c1e-decb68eff0b4.jsonl"
)
CODEX_PROCESS_NAMES = {"codex.exe", "codex++.exe"}


def _running_codex_processes() -> list[str]:
    if os.name != "nt":
        return []
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    names = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if row and row[0].strip().lower() in CODEX_PROCESS_NAMES:
            names.append(row[0].strip())
    return sorted(set(names), key=str.lower)


def wait_for_codex_exit(timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    announced = False
    while True:
        running = _running_codex_processes()
        if not running:
            return
        if not announced:
            print("检测到 Codex 正在运行。请从 Codex 菜单正常退出，不要强制结束进程。", flush=True)
            announced = True
        if time.monotonic() >= deadline:
            raise TimeoutError(f"等待 Codex 正常退出超时：{', '.join(running)}")
        time.sleep(2)


def _integrity_check(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute("pragma integrity_check").fetchall()
        return "\n".join(str(row[0]) for row in rows)
    finally:
        connection.close()


def _verify_repair(codex_home: Path) -> dict[str, Any]:
    rows = migration_bundle.read_sqlite_threads(codex_home, {TASK_ID})
    row = rows.get(TASK_ID)
    if row is None:
        raise ValueError(f"修复验证失败：线程记录不存在：{TASK_ID}")
    rollout = migration_bundle.resolve_local_path(row.get("rollout_path"), codex_home)
    relative = rollout.relative_to(codex_home)
    if not bool(row.get("archived")):
        raise ValueError("修复验证失败：archived 不是 1")
    if "archived_at" in row and row.get("archived_at") is None:
        raise ValueError("修复验证失败：archived_at 为空")
    if not relative.parts or relative.parts[0] != "archived_sessions":
        raise ValueError("修复验证失败：rollout 未位于 archived_sessions")
    if rollout.name != EXPECTED_ROLLOUT_NAME or not rollout.is_file():
        raise ValueError("修复验证失败：目标 rollout 文件缺失或文件名异常")
    catalog_database = content_manager._thread_catalog_database(codex_home)
    if catalog_database and content_manager._catalog_contains(catalog_database, {TASK_ID}):
        raise ValueError("修复验证失败：local_thread_catalog 仍有可见记录")
    state_database = migration_bundle.find_state_db(codex_home)
    if state_database is None:
        raise ValueError("修复验证失败：state SQLite 不存在")
    state_integrity = _integrity_check(state_database)
    if state_integrity != "ok":
        raise ValueError(f"修复验证失败：state SQLite integrity_check={state_integrity}")
    catalog_integrity = None
    if catalog_database:
        catalog_integrity = _integrity_check(catalog_database)
        if catalog_integrity != "ok":
            raise ValueError(f"修复验证失败：catalog SQLite integrity_check={catalog_integrity}")
    return {
        "task_id": TASK_ID,
        "rollout_path": str(rollout),
        "archived": int(bool(row.get("archived"))),
        "archived_at": row.get("archived_at"),
        "catalog_present": False,
        "state_integrity": state_integrity,
        "catalog_integrity": catalog_integrity,
    }


def repair(codex_home: Path, *, require_codex_closed: bool = True) -> dict[str, Any]:
    codex_home = codex_home.expanduser().resolve()
    if require_codex_closed and _running_codex_processes():
        raise ValueError("Codex 仍在运行，拒绝修改真实数据")
    rows = migration_bundle.read_sqlite_threads(codex_home, {TASK_ID})
    row = rows.get(TASK_ID)
    if row is None:
        raise ValueError(f"找不到固定任务：{TASK_ID}")
    rollout = migration_bundle.resolve_local_path(row.get("rollout_path"), codex_home)
    if rollout.name != EXPECTED_ROLLOUT_NAME:
        raise ValueError(f"固定任务 rollout 文件名与已确认现场不一致：{rollout.name}")
    result = content_manager.set_conversations_archived(
        codex_home,
        {TASK_ID},
        archived=True,
        require_codex_closed=require_codex_closed,
    )
    try:
        verification = _verify_repair(codex_home)
    except Exception:
        backup_path = result.get("backup_path")
        if backup_path:
            migration_bundle.restore_backup(
                Path(backup_path), codex_home, require_codex_closed=False
            )
        raise
    return {"operation": result, "verification": verification}


def _log_path() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    root = local_app_data / "CrossDeviceAgentSync" / "logs"
    root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return root / f"archive-incident-repair-{stamp}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="修复已确认的半归档任务，不处理其他任务")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--wait-seconds", type=int, default=1800)
    parser.add_argument("--confirm-task-id", required=True)
    args = parser.parse_args()
    if args.confirm_task_id != TASK_ID:
        parser.error(f"--confirm-task-id 必须精确等于 {TASK_ID}")

    log_path = _log_path()
    payload: dict[str, Any] = {
        "task_id": TASK_ID,
        "codex_home": str(args.codex_home.expanduser().resolve()),
        "started_at": migration_bundle.now_iso(),
        "status": "in_progress",
    }
    try:
        wait_for_codex_exit(args.wait_seconds)
        payload["result"] = repair(args.codex_home, require_codex_closed=True)
        payload["status"] = "committed"
        payload["completed_at"] = migration_bundle.now_iso()
        migration_bundle.atomic_write(
            log_path, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        )
        print(f"修复成功。日志：{log_path}")
        print("现在可以重新启动 Codex，确认该任务只出现在归档列表中。")
        return 0
    except Exception as error:
        payload["status"] = "failed"
        payload["error"] = f"{type(error).__name__}: {error}"
        payload["completed_at"] = migration_bundle.now_iso()
        migration_bundle.atomic_write(
            log_path, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        )
        print(f"修复失败：{error}", file=sys.stderr)
        print(f"日志：{log_path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

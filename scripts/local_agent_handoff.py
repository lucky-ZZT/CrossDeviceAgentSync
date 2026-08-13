#!/usr/bin/env python3
"""Clone selected local Codex conversations into another local agent identity."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

import migration_bundle
import session_merge_planner as planner


MAIN_AGENT = ""


def discover_agents(codex_home: Path) -> list[dict[str, Any]]:
    codex_home = codex_home.expanduser().resolve()
    database = migration_bundle.find_state_db(codex_home)
    if database is None:
        raise ValueError("Codex thread database was not found")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "select coalesce(agent_nickname,''), coalesce(agent_path,''), count(*) "
            "from threads group by agent_nickname, agent_path order by count(*) desc"
        ).fetchall()
    finally:
        connection.close()
    agents = []
    for nickname, agent_path, count in rows:
        label = "Main agent" if not nickname else nickname
        agents.append({"id": nickname, "label": label, "agent_path": agent_path, "count": count})
    return agents


def list_agent_threads(codex_home: Path, agent_id: str) -> list[dict[str, Any]]:
    codex_home = codex_home.expanduser().resolve()
    database = migration_bundle.find_state_db(codex_home)
    if database is None:
        raise ValueError("Codex thread database was not found")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "select id,title,updated_at,rollout_path,model_provider,agent_nickname,agent_path "
            "from threads where coalesce(agent_nickname,'')=? order by updated_at desc",
            (agent_id,),
        ).fetchall()
        threads = []
        for row in rows:
            try:
                path = migration_bundle.resolve_local_path(row["rollout_path"], codex_home)
            except (OSError, ValueError):
                continue
            if path.is_file() and path.is_relative_to(codex_home):
                threads.append(dict(row))
        return threads
    finally:
        connection.close()


def assign_agent(value: Any, nickname: str, agent_path: str) -> Any:
    if isinstance(value, dict):
        result = {key: assign_agent(item, nickname, agent_path) for key, item in value.items()}
        if value.get("type") == "session_meta" and isinstance(result.get("payload"), dict):
            result["payload"]["agent_nickname"] = nickname or None
            result["payload"]["agent_path"] = agent_path or None
        if "agent_nickname" in result:
            result["agent_nickname"] = nickname or None
        if "agent_path" in result:
            result["agent_path"] = agent_path or None
        return result
    if isinstance(value, list):
        return [assign_agent(item, nickname, agent_path) for item in value]
    return value


def assign_agent_jsonl(data: bytes, nickname: str, agent_path: str) -> bytes:
    output = []
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"))
            output.append(json.dumps(assign_agent(value, nickname, agent_path), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            output.append(line)
    return b"\n".join(output) + b"\n"


def filtered_inventory(inventory: dict[str, Any], selected_ids: set[str]) -> dict[str, Any]:
    filtered = {**inventory, "conversations": [item for item in inventory["conversations"] if item["task_id"] in selected_ids]}
    filtered["inventory_hash"] = planner.sha256_bytes(planner.canonical_json({
        "device_id": filtered["device_id"],
        "conversations": filtered["conversations"],
    }))
    return filtered


def handoff(
    codex_home: Path,
    source_agent: str,
    target_agent: str,
    target_agent_path: str,
    selected_ids: set[str],
    require_codex_closed: bool = True,
) -> dict[str, Any]:
    if not selected_ids:
        raise ValueError("No conversations were selected")
    codex_home = codex_home.expanduser().resolve()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = filtered_inventory(planner.inventory(codex_home, "local-source-agent"), selected_ids)
        if len(source["conversations"]) != len(selected_ids):
            raise ValueError("One or more selected conversations were not found")
        empty = {
            "schema_version": 1,
            "kind": "cross-device-agent-sync-inventory",
            "device_id": "local-target-agent",
            "codex_home": str(codex_home),
            "generated_at": "",
            "conversations": [],
        }
        empty["inventory_hash"] = planner.sha256_bytes(planner.canonical_json({"device_id": empty["device_id"], "conversations": []}))
        plan = planner.compare_inventories(source, empty, "left-to-right", set(), set())
        source_path, plan_path, base_bundle = root / "source.json", root / "plan.json", root / "base.zip"
        planner.write_json(source_path, source)
        planner.write_json(plan_path, plan)
        migration_bundle.create_bundle(source_path, plan_path, "left", base_bundle)
        manifest, payloads = migration_bundle.inspect_bundle(base_bundle)
        for conversation in manifest["conversations"]:
            source_id = conversation["source_task_id"]
            data = assign_agent_jsonl(payloads[conversation["payload"]], target_agent, target_agent_path)
            payloads[conversation["payload"]] = data
            digest = planner.sha256_bytes(data)
            conversation["content_hash"] = digest
            conversation["target_task_id"] = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"local-agent-handoff:{source_agent}:{target_agent}:{source_id}:{digest}",
            ))
            row = conversation.get("sqlite_thread_row")
            if isinstance(row, dict):
                row["agent_nickname"] = target_agent or None
                row["agent_path"] = target_agent_path or None
            conversation["title"] = f"{conversation['title']} -> {target_agent or 'Main agent'}"
        manifest["payload_checksums"] = {name: planner.sha256_bytes(data) for name, data in payloads.items()}
        modified = root / "handoff.zip"
        with zipfile.ZipFile(modified, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            for name, data in payloads.items():
                archive.writestr(name, data)
        return migration_bundle.restore_bundle(modified, codex_home, require_codex_closed=require_codex_closed)

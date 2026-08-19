#!/usr/bin/env python3
"""Read-only Codex session inventory and two-device merge planner."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 1
LOCAL_METADATA_KEYS = {
    "cwd",
    "workspace_root",
    "rollout_path",
    "session_path",
    "model_provider",
    "provider",
    "model",
}
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def normalize_semantic(value: Any, parent_key: str | None = None) -> Any:
    if parent_key in LOCAL_METADATA_KEYS:
        return f"<{parent_key}>"
    if isinstance(value, dict):
        return {key: normalize_semantic(item, key) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [normalize_semantic(item, parent_key) for item in value]
    return value


def first_uuid(*values: Any) -> str | None:
    for value in values:
        if not isinstance(value, str):
            continue
        match = UUID_RE.search(value)
        if match:
            return match.group(0).lower()
    return None


def text_candidate(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip().replace("\r", " ").replace("\n", " ")[:160]
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                candidate = text_candidate(item.get("text") or item.get("content"))
            else:
                candidate = text_candidate(item)
            if candidate:
                return candidate
    return None


def parse_session(path: Path, codex_home: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    exact_lines: list[str] = []
    semantic_lines: list[str] = []
    warnings: list[str] = []
    task_id: str | None = None
    title: str | None = None
    timestamp: str | None = None
    parent_task_id: str | None = None
    agent_path: str | None = None
    providers: set[str] = set()

    for number, raw_line in enumerate(raw.splitlines(), start=1):
        if not raw_line.strip():
            continue
        exact_lines.append(sha256_bytes(raw_line))
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            semantic_lines.append(sha256_bytes(raw_line))
            warnings.append(f"line {number} is not valid UTF-8 JSON")
            continue

        semantic_lines.append(sha256_bytes(canonical_json(normalize_semantic(value))))
        if not isinstance(value, dict):
            continue
        payload = value.get("payload") if isinstance(value.get("payload"), dict) else value
        event_type = value.get("type")
        if event_type == "session_meta" or task_id is None:
            task_id = task_id or first_uuid(
                payload.get("id"), payload.get("thread_id"), payload.get("conversation_id"), path.name
            )
            title = title or text_candidate(
                payload.get("thread_name") or payload.get("name") or payload.get("title")
            )
            timestamp = timestamp or payload.get("timestamp") or value.get("timestamp")
            parent_task_id = parent_task_id or first_uuid(payload.get("parent_task_id"))
            agent_path = agent_path or text_candidate(payload.get("agent_path"))
        for key in ("model_provider", "provider"):
            provider = payload.get(key)
            if isinstance(provider, str) and provider:
                providers.add(provider)
        if title is None and payload.get("role") == "user":
            title = text_candidate(payload.get("content") or payload.get("text"))

    task_id = task_id or first_uuid(path.name)
    if task_id is None:
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"unidentified:{sha256_bytes(raw)}"))
        warnings.append("task ID was derived from content because no UUID was found")
    relative = path.relative_to(codex_home).as_posix()
    archived = relative.startswith("archived_sessions/")
    updated = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).isoformat()
    return {
        "task_id": task_id,
        "title": title or task_id,
        "updated_at": timestamp or updated,
        "filesystem_updated_at": updated,
        "relative_path": relative,
        "archived": archived,
        "content_hash": sha256_bytes(raw),
        "size_bytes": len(raw),
        "line_count": len(exact_lines),
        "exact_line_hashes": exact_lines,
        "semantic_line_hashes": semantic_lines,
        "parent_task_id": parent_task_id,
        "agent_path": agent_path,
        "providers": sorted(providers),
        "warnings": warnings,
    }


def inventory(
    codex_home: Path,
    device_id: str,
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    codex_home = codex_home.expanduser().resolve()
    if not codex_home.is_dir():
        raise ValueError(f"Codex home does not exist: {codex_home}")
    paths: list[Path] = []
    for directory in ("sessions", "archived_sessions"):
        root = codex_home / directory
        if root.is_dir():
            paths.extend(path for path in root.rglob("*.jsonl") if path.is_file())
    paths = sorted(paths)
    total_bytes = sum(path.stat().st_size for path in paths)
    completed_bytes = 0
    conversations = []
    for index, path in enumerate(paths, start=1):
        size = path.stat().st_size
        if progress_callback:
            progress_callback(
                "scan",
                f"正在分析对话 {index}/{len(paths)}：{path.name}（{size / (1024 * 1024):.1f} MB）",
            )
        conversations.append(parse_session(path, codex_home))
        completed_bytes += size
        if progress_callback:
            progress_callback(
                "scan",
                f"已分析 {index}/{len(paths)} 个对话，{completed_bytes / (1024 * 1024):.1f}/"
                f"{total_bytes / (1024 * 1024):.1f} MB",
            )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "cross-device-agent-sync-inventory",
        "device_id": device_id,
        "codex_home": str(codex_home),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "conversations": conversations,
    }
    payload["inventory_hash"] = sha256_bytes(canonical_json({
        "device_id": device_id,
        "conversations": conversations,
    }))
    return payload


def load_inventory(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("kind") != "cross-device-agent-sync-inventory" or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported inventory: {path}")
    return data


def common_prefix(left: list[str], right: list[str]) -> int:
    count = 0
    for left_value, right_value in zip(left, right):
        if left_value != right_value:
            break
        count += 1
    return count


def relationship(left: dict[str, Any] | None, right: dict[str, Any] | None) -> tuple[str, int]:
    if left is None:
        return "right_only", 0
    if right is None:
        return "left_only", 0
    if left["content_hash"] == right["content_hash"]:
        return "identical", left["line_count"]
    left_events = left["semantic_line_hashes"]
    right_events = right["semantic_line_hashes"]
    prefix = common_prefix(left_events, right_events)
    if left_events == right_events:
        return "metadata_equivalent", prefix
    if prefix == len(right_events) and len(left_events) > len(right_events):
        return "left_ahead", prefix
    if prefix == len(left_events) and len(right_events) > len(left_events):
        return "right_ahead", prefix
    if prefix > 0:
        return "diverged", prefix
    return "id_collision", 0


def summary(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {key: item.get(key) for key in (
        "task_id", "title", "updated_at", "relative_path", "archived", "content_hash",
        "size_bytes", "line_count", "providers", "parent_task_id", "agent_path"
    )}


def branch_id(task_id: str, device_id: str, content_hash: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cross-device-agent-sync:{task_id}:{device_id}:{content_hash}"))


def action_for(kind: str, direction: str) -> tuple[str, str, bool]:
    if kind == "identical":
        return "skip", "skip", False
    if kind == "metadata_equivalent":
        return "reconcile_local_metadata_if_needed", "skip_content_copy", False
    if direction == "left-to-right":
        mapping = {
            "left_only": ("import_left_to_right", "import", False),
            "right_only": ("preserve_right", "skip", False),
            "left_ahead": ("fast_forward_left_to_right_candidate", "import_left_as_branch", True),
            "right_ahead": ("preserve_newer_right", "skip", False),
            "diverged": ("import_left_as_branch", "import_left_as_branch", True),
            "id_collision": ("import_left_as_branch", "import_left_as_branch", True),
        }
    elif direction == "right-to-left":
        mapping = {
            "left_only": ("preserve_left", "skip", False),
            "right_only": ("import_right_to_left", "import", False),
            "left_ahead": ("preserve_newer_left", "skip", False),
            "right_ahead": ("fast_forward_right_to_left_candidate", "import_right_as_branch", True),
            "diverged": ("import_right_as_branch", "import_right_as_branch", True),
            "id_collision": ("import_right_as_branch", "import_right_as_branch", True),
        }
    else:
        mapping = {
            "left_only": ("import_left_to_right", "import_left_to_right", False),
            "right_only": ("import_right_to_left", "import_right_to_left", False),
            "left_ahead": ("fast_forward_left_to_right_candidate", "import_left_as_branch", True),
            "right_ahead": ("fast_forward_right_to_left_candidate", "import_right_as_branch", True),
            "diverged": ("exchange_as_branches", "exchange_as_branches", True),
            "id_collision": ("exchange_as_branches", "exchange_as_branches", True),
        }
    return mapping[kind]


def read_ids(values: list[str], include_file: Path | None = None) -> set[str]:
    result = {item.strip().lower() for value in values for item in value.split(",") if item.strip()}
    if include_file:
        for line in include_file.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                result.add(value.lower())
    return result


def compare_inventories(
    left: dict[str, Any],
    right: dict[str, Any],
    direction: str,
    includes: set[str],
    excludes: set[str],
) -> dict[str, Any]:
    left_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    right_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in left["conversations"]:
        left_by_id[item["task_id"].lower()].append(item)
    for item in right["conversations"]:
        right_by_id[item["task_id"].lower()].append(item)

    entries = []
    for task_id in sorted(set(left_by_id) | set(right_by_id)):
        left_items = left_by_id.get(task_id, [])
        right_items = right_by_id.get(task_id, [])
        if len(left_items) > 1 or len(right_items) > 1:
            kind, prefix = "duplicate_local_id", 0
            recommendation, safe_action, confirmation = "manual_review", "stop", True
            left_item = left_items[0] if len(left_items) == 1 else None
            right_item = right_items[0] if len(right_items) == 1 else None
        else:
            left_item = left_items[0] if left_items else None
            right_item = right_items[0] if right_items else None
            kind, prefix = relationship(left_item, right_item)
            recommendation, safe_action, confirmation = action_for(kind, direction)
        actionable = safe_action not in {"skip", "skip_content_copy", "stop"}
        selected = actionable and (not includes or task_id in includes) and task_id not in excludes
        entry = {
            "task_id": task_id,
            "title": (left_item or right_item or {}).get("title", task_id),
            "classification": kind,
            "common_prefix_lines": prefix,
            "recommendation": recommendation,
            "safe_default_action": safe_action,
            "requires_confirmation": confirmation,
            "selected": selected,
            "left": summary(left_item),
            "right": summary(right_item),
        }
        if left_item:
            entry["proposed_left_branch_id"] = branch_id(
                task_id, left["device_id"], left_item["content_hash"]
            )
        if right_item:
            entry["proposed_right_branch_id"] = branch_id(
                task_id, right["device_id"], right_item["content_hash"]
            )
        if kind == "duplicate_local_id":
            entry["left_duplicate_count"] = len(left_items)
            entry["right_duplicate_count"] = len(right_items)
        entries.append(entry)

    plan_core = {
        "schema_version": SCHEMA_VERSION,
        "kind": "cross-device-agent-sync-plan",
        "direction": direction,
        "left_device_id": left["device_id"],
        "right_device_id": right["device_id"],
        "left_inventory_hash": left["inventory_hash"],
        "right_inventory_hash": right["inventory_hash"],
        "entries": entries,
    }
    return {
        **plan_core,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "plan_id": str(uuid.uuid5(uuid.NAMESPACE_URL, sha256_bytes(canonical_json(plan_core)))),
        "summary": {
            "total": len(entries),
            "selected": sum(1 for entry in entries if entry["selected"]),
            "by_classification": {
                kind: sum(1 for entry in entries if entry["classification"] == kind)
                for kind in sorted({entry["classification"] for entry in entries})
            },
        },
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory", help="Inventory a Codex home")
    inventory_parser.add_argument("--codex-home", type=Path, required=True)
    inventory_parser.add_argument("--device-id", required=True)
    inventory_parser.add_argument("--output", type=Path, required=True)

    compare_parser = subparsers.add_parser("compare", help="Compare two inventory files")
    compare_parser.add_argument("--left", type=Path, required=True)
    compare_parser.add_argument("--right", type=Path, required=True)
    compare_parser.add_argument(
        "--direction", choices=("left-to-right", "right-to-left", "bidirectional"), default="bidirectional"
    )
    compare_parser.add_argument("--include", action="append", default=[])
    compare_parser.add_argument("--include-file", type=Path)
    compare_parser.add_argument("--exclude", action="append", default=[])
    compare_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inventory":
            result = inventory(args.codex_home, args.device_id)
        else:
            left = load_inventory(args.left)
            right = load_inventory(args.right)
            includes = read_ids(args.include, args.include_file)
            excludes = read_ids(args.exclude)
            result = compare_inventories(left, right, args.direction, includes, excludes)
        write_json(args.output, result)
        print(json.dumps({
            "ok": True,
            "output": str(args.output.resolve()),
            "kind": result["kind"],
            "summary": result.get("summary"),
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

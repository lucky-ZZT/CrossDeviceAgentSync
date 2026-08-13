#!/usr/bin/env python3
"""Check reviewed upstream baselines and assess whether changes merit adoption."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


UPSTREAMS = (
    {
        "id": "codex-provider-sync",
        "label": "Codex Provider Sync",
        "owner": "Dailin521",
        "repo": "codex-provider-sync",
        "reviewed_commit": "75f45756cf732333e7c52f45c8cd1b183291a029",
    },
    {
        "id": "codex-rehome",
        "label": "Codex ReHome",
        "owner": "CalebYcj",
        "repo": "codex-rehome",
        "reviewed_commit": "24dd0a9611a757ec8f44d944557d710262e3db04",
    },
)

HIGH_IMPACT = {
    "provider", "sqlite", "session", "rollout", "backup", "restore", "migration",
    "manifest", "schema", "path", "transaction", "desktop/src-tauri/src/commands",
}
MEDIUM_IMPACT = {"security", "lock", "verify", "checksum", "archive", "bundle", "config"}


def state_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "CrossDeviceAgentSync"
    root.mkdir(parents=True, exist_ok=True)
    return root / "upstream-update-state.json"


def review_root() -> Path:
    root = state_path().parent / "upstream-reviews"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _request_json(url: str, timeout: int = 15) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "CrossDeviceAgentSync-update-checker",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _load_state() -> dict[str, Any]:
    path = state_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _assess(files: list[str], commits: list[dict[str, Any]]) -> tuple[str, list[str]]:
    searchable = "\n".join(files + [str(item.get("commit", {}).get("message", "")) for item in commits]).lower()
    high = sorted(keyword for keyword in HIGH_IMPACT if keyword in searchable)
    medium = sorted(keyword for keyword in MEDIUM_IMPACT if keyword in searchable)
    if high:
        return "建议复核并评估更新", [f"涉及核心迁移/状态关键词：{', '.join(high[:8])}"]
    if medium:
        return "建议人工复核", [f"涉及可靠性或安全关键词：{', '.join(medium[:8])}"]
    if files:
        return "暂不需要更新", ["变更未触及当前工具采用的核心迁移、Provider、SQLite或恢复逻辑。"]
    return "无需更新", ["已审查基线与最新提交一致。"]


def check_updates() -> dict[str, Any]:
    previous = _load_state()
    results = []
    for upstream in UPSTREAMS:
        base_url = f"https://api.github.com/repos/{upstream['owner']}/{upstream['repo']}"
        try:
            repository = _request_json(base_url)
            latest_commit = str(repository.get("default_branch") or "main")
            head = _request_json(f"{base_url}/commits/{urllib.parse.quote(latest_commit)}")
            head_sha = str(head.get("sha") or "")
            release = None
            try:
                release = _request_json(f"{base_url}/releases/latest")
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
            files: list[str] = []
            commits: list[dict[str, Any]] = []
            compare_status = "identical"
            ahead_by = 0
            if head_sha and head_sha != upstream["reviewed_commit"]:
                compare = _request_json(
                    f"{base_url}/compare/{upstream['reviewed_commit']}...{head_sha}"
                )
                compare_status = str(compare.get("status") or "unknown")
                ahead_by = int(compare.get("ahead_by") or 0)
                files = [str(item.get("filename") or "") for item in compare.get("files", []) if item.get("filename")]
                commits = list(compare.get("commits", []))
            recommendation, reasons = _assess(files, commits)
            old = previous.get(upstream["id"], {}) if isinstance(previous.get(upstream["id"]), dict) else {}
            results.append({
                **upstream,
                "url": repository.get("html_url"),
                "latest_commit": head_sha,
                "latest_commit_date": head.get("commit", {}).get("committer", {}).get("date", ""),
                "latest_release": release.get("tag_name") if isinstance(release, dict) else None,
                "latest_release_url": release.get("html_url") if isinstance(release, dict) else None,
                "compare_status": compare_status,
                "ahead_by": ahead_by,
                "changed_files": files,
                "commit_messages": [str(item.get("commit", {}).get("message", "")).splitlines()[0] for item in commits],
                "recommendation": recommendation,
                "reasons": reasons,
                "changed_since_last_check": old.get("latest_commit") not in (None, head_sha),
                "error": None,
            })
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as error:
            results.append({
                **upstream,
                "latest_commit": None,
                "latest_release": None,
                "ahead_by": 0,
                "changed_files": [],
                "commit_messages": [],
                "recommendation": "检查失败",
                "reasons": [str(error)],
                "changed_since_last_check": False,
                "error": str(error),
            })
    checked_at = datetime.now().astimezone().isoformat()
    state_path().write_text(
        json.dumps({item["id"]: {"latest_commit": item.get("latest_commit"), "checked_at": checked_at} for item in results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report = {"schema_version": 1, "checked_at": checked_at, "results": results, "state_path": str(state_path())}
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = review_root() / f"upstream-review-{timestamp}.json"
    latest_path = review_root() / "latest-upstream-review.json"
    report["report_path"] = str(report_path)
    report["latest_path"] = str(latest_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def handoff_prompt(report: dict[str, Any]) -> str:
    path = report.get("latest_path") or report.get("report_path") or str(
        review_root() / "latest-upstream-review.json"
    )
    projects = []
    for item in report.get("results", []):
        latest = item.get("latest_commit") or "检查失败"
        projects.append(
            f"- {item.get('label', item.get('id', '未知项目'))}\n"
            f"  已审查基线：{item.get('reviewed_commit', '未知')}\n"
            f"  最新提交：{latest}\n"
            f"  最新发布：{item.get('latest_release') or '无'}\n"
            f"  检查结果：{item.get('recommendation') or '未知'}"
        )
    project_summary = "\n".join(projects) or "- 报告中没有参考项目结果"
    return (
        "请一次性统一审查 CrossDeviceAgentSync 的两个参考项目，不要拆成两次交接。\n\n"
        f"审查报告：{path}\n\n"
        f"本次项目：\n{project_summary}\n\n"
        "请读取同一份报告中两个项目的最新提交、Release、变更文件和提交说明；必要时分别获取上游代码差异。"
        "不要直接执行上游代码。请由你判断变化是否值得本工具升级，不能把软件的关键词初筛当作最终结论。"
        "若值得升级，请统一修改当前 skill、运行完整测试并只重新封装一次 EXE；若不值得升级，不要为了版本号而改代码或重打包。"
        "完成后必须向用户提供一份明确的更新说明，不能只回复‘已更新’。更新说明必须分别列出："
        "1. 两个上游项目各自更新了什么；2. 本工具实际采用了哪些变化以及对应功能；"
        "3. 哪些变化未采用及原因；4. 对用户操作、数据安全和兼容性的实际影响；"
        "5. 若发生升级，给出完整测试结果、新 EXE 路径、版本号和 SHA-256；若未升级，明确说明未产生新 EXE。"
        "完成审查后，只把成功审查项目的最新提交写回 "
        "upstream_update_checker.py 的 reviewed_commit；单个项目检查失败时只跳过该项目，不影响另一个项目，"
        "且检查失败的项目不得推进基线。"
    )

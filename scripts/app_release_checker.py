#!/usr/bin/env python3
"""Read the latest CrossDeviceAgentSync GitHub Release without installing it."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any


GITHUB_REPOSITORY = "lucky-ZZT/CrossDeviceAgentSync"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def repository() -> str:
    value = os.environ.get("CDAS_GITHUB_REPOSITORY", GITHUB_REPOSITORY).strip()
    if not _REPOSITORY_RE.fullmatch(value):
        raise ValueError("项目 GitHub 仓库地址尚未配置，发布前需要在 app_release_checker.py 中填写 owner/repository。")
    return value


def is_configured() -> bool:
    try:
        repository()
        return True
    except ValueError:
        return False


def release_page_url() -> str:
    return f"https://github.com/{repository()}/releases"


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"无法识别版本号：{value}")
    return tuple(int(part) for part in match.groups())


def _request_json(url: str, timeout: int = 15) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "CrossDeviceAgentSync-release-checker",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def check_latest_release(current_version: str) -> dict[str, Any]:
    project = repository()
    try:
        release = _request_json(f"https://api.github.com/repos/{project}/releases/latest")
    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise RuntimeError("GitHub 暂时限制了匿名检查次数，请稍后再试或直接打开项目 Release 页面。") from error
        if error.code == 404:
            raise RuntimeError("没有找到项目的正式 GitHub Release，请检查仓库地址或等待首次发布。") from error
        raise RuntimeError(f"GitHub Release 检查失败（HTTP {error.code}）。") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"无法连接 GitHub：{error.reason}") from error
    if release.get("draft") or release.get("prerelease"):
        raise ValueError("GitHub 返回的最新 Release 不是正式版本。")
    tag = str(release.get("tag_name") or "").strip()
    current = parse_version(current_version)
    latest = parse_version(tag)
    assets = [
        {
            "name": str(item.get("name") or ""),
            "size": int(item.get("size") or 0),
            "download_url": str(item.get("browser_download_url") or ""),
        }
        for item in release.get("assets", [])
        if isinstance(item, dict)
    ]
    return {
        "repository": project,
        "current_version": current_version,
        "latest_version": tag.removeprefix("v"),
        "update_available": latest > current,
        "release_name": str(release.get("name") or tag),
        "release_notes": str(release.get("body") or ""),
        "release_url": str(release.get("html_url") or f"https://github.com/{project}/releases/latest"),
        "published_at": str(release.get("published_at") or ""),
        "assets": assets,
    }

#!/usr/bin/env python3
"""Read the latest CrossDeviceAgentSync GitHub Release without installing it."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as xml_tree
from html import unescape
from pathlib import Path
from typing import Any


GITHUB_REPOSITORY = "lucky-ZZT/CrossDeviceAgentSync"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ATOM_NAMESPACE = "{http://www.w3.org/2005/Atom}"
_CACHE_SCHEMA_VERSION = 1
_REQUEST_ATTEMPTS = 3


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


def _open_request(request: urllib.request.Request, timeout: int):
    for attempt in range(_REQUEST_ATTEMPTS):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.URLError:
            if attempt + 1 == _REQUEST_ATTEMPTS:
                raise
            time.sleep(0.4 * (attempt + 1))


def _request_json(url: str, timeout: int = 15) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "CrossDeviceAgentSync-release-checker",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with _open_request(request, timeout) as response:
        return json.load(response)


def _request_text(url: str, timeout: int = 15) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/atom+xml, application/xml;q=0.9, text/xml;q=0.8",
            "User-Agent": "CrossDeviceAgentSync-release-checker",
        },
    )
    with _open_request(request, timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _release_notes_from_html(value: str) -> str:
    decoded = unescape(value or "")
    decoded = re.sub(r"(?i)<br\s*/?>", "\n", decoded)
    decoded = re.sub(r"(?i)</?(?:p|h[1-6]|ul|ol|div)\b[^>]*>", "\n", decoded)
    decoded = re.sub(r"(?i)<li\b[^>]*>", "- ", decoded)
    decoded = re.sub(r"<[^>]+>", "", decoded)
    return re.sub(r"\n{3,}", "\n\n", decoded).strip()


def _latest_release_from_atom(project: str) -> dict[str, Any]:
    root = xml_tree.fromstring(_request_text(f"https://github.com/{project}/releases.atom"))
    entry = root.find(f"{_ATOM_NAMESPACE}entry")
    if entry is None:
        raise RuntimeError("没有找到项目的正式 GitHub Release，请检查仓库地址或等待首次发布。")
    release_id = (entry.findtext(f"{_ATOM_NAMESPACE}id") or "").rsplit("/", 1)[-1]
    title = (entry.findtext(f"{_ATOM_NAMESPACE}title") or release_id).strip()
    link = entry.find(f"{_ATOM_NAMESPACE}link[@rel='alternate']")
    release_url = (link.get("href") if link is not None else "") or f"https://github.com/{project}/releases/latest"
    return {
        "tag_name": release_id,
        "name": title,
        "body": _release_notes_from_html(entry.findtext(f"{_ATOM_NAMESPACE}content") or ""),
        "html_url": release_url,
        "published_at": (entry.findtext(f"{_ATOM_NAMESPACE}updated") or "").strip(),
        "draft": False,
        "prerelease": False,
        "assets": [],
        "assets_known": False,
        "source": "atom",
    }


def _latest_release(project: str) -> dict[str, Any]:
    try:
        return _latest_release_from_atom(project)
    except (urllib.error.HTTPError, urllib.error.URLError, xml_tree.ParseError):
        try:
            release = _request_json(f"https://api.github.com/repos/{project}/releases/latest")
        except urllib.error.HTTPError as error:
            if error.code in {403, 429}:
                raise RuntimeError("GitHub 暂时无法完成更新检查。请稍后重试或直接打开项目 Release 页面。") from error
            if error.code == 404:
                raise RuntimeError("没有找到项目的正式 GitHub Release，请检查仓库地址或等待首次发布。") from error
            raise RuntimeError(f"GitHub Release 检查失败（HTTP {error.code}）。") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"无法连接 GitHub：{error.reason}") from error
        if isinstance(release, dict):
            release.setdefault("assets_known", True)
            release.setdefault("source", "api")
        return release


def _release_artifact(project: str, tag: str, assets: list[dict[str, Any]]) -> dict[str, str]:
    version = tag.removeprefix("v")
    tag_name = f"v{version}"
    filename = f"CrossDeviceAgentSync-v{version}.exe"
    default_base = f"https://github.com/{project}/releases/download/{tag_name}"
    match = next((item for item in assets if item["name"] == filename), None)
    return {
        "name": filename,
        "download_url": str(match["download_url"] if match else f"{default_base}/{filename}"),
        "checksum_url": f"{default_base}/SHA256SUMS.txt",
    }


def _result_from_release(project: str, current_version: str, release: dict[str, Any]) -> dict[str, Any]:
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
        "artifact": _release_artifact(project, tag, assets),
        "assets_known": bool(release.get("assets_known", True)),
        "source": str(release.get("source") or "api"),
    }


def _cache_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "CrossDeviceAgentSync" / "release-cache.json"


def _write_cache(project: str, release: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "repository": project,
        "cached_at": time.time(),
        "release": release,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _read_cache(project: str) -> tuple[dict[str, Any], float] | None:
    path = _cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema_version") != _CACHE_SCHEMA_VERSION or payload.get("repository") != project:
        return None
    release = payload.get("release")
    cached_at = payload.get("cached_at")
    if not isinstance(release, dict) or not isinstance(cached_at, (int, float)):
        return None
    return release, float(cached_at)


def check_latest_release(current_version: str) -> dict[str, Any]:
    project = repository()
    try:
        release = _latest_release(project)
        result = _result_from_release(project, current_version, release)
        try:
            _write_cache(project, release)
        except OSError:
            pass
        result["using_cached_release"] = False
        return result
    except RuntimeError as error:
        cached = _read_cache(project)
        if cached is None:
            raise
        release, cached_at = cached
        result = _result_from_release(project, current_version, release)
        result["using_cached_release"] = True
        result["cached_at"] = cached_at
        result["network_error"] = str(error)
        return result

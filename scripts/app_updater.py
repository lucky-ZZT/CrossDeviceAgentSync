#!/usr/bin/env python3
"""Download, verify, and safely replace a published Windows EXE release."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path
from typing import Any


CHUNK_SIZE = 1024 * 1024


def update_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    root = base / "CrossDeviceAgentSync" / "updates"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "CrossDeviceAgentSync-updater"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        for chunk in iter(lambda: response.read(CHUNK_SIZE), b""):
            output.write(chunk)


def _expected_checksum(checksum_file: Path, filename: str) -> str:
    for line in checksum_file.read_text(encoding="ascii", errors="replace").splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        checksum, listed_name = parts
        if listed_name.lstrip("*") == filename and len(checksum) == 64:
            try:
                int(checksum, 16)
                return checksum.upper()
            except ValueError:
                continue
    raise ValueError(f"更新校验文件中缺少 {filename} 的 SHA-256。")


def download_and_verify(release: dict[str, Any], destination_root: Path | None = None) -> Path:
    artifact = release.get("artifact") or {}
    filename = str(artifact.get("name") or "")
    download_url = str(artifact.get("download_url") or "")
    checksum_url = str(artifact.get("checksum_url") or "")
    if not filename or not download_url or not checksum_url:
        raise ValueError("发布信息不完整，无法安全下载更新。")
    if Path(filename).name != filename or not filename.lower().endswith(".exe"):
        raise ValueError("发布的更新文件名不安全。")

    root = (destination_root or update_root()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    executable = root / f"{token}-{filename}"
    checksum_file = root / f"{token}-SHA256SUMS.txt"
    try:
        _download(checksum_url, checksum_file)
        expected = _expected_checksum(checksum_file, filename)
        _download(download_url, executable)
        actual = sha256_file(executable)
        if actual != expected:
            raise ValueError("下载的更新文件校验失败，旧版本未被修改。")
        return executable
    except Exception:
        executable.unlink(missing_ok=True)
        raise
    finally:
        checksum_file.unlink(missing_ok=True)


def running_executable() -> Path:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("当前通过 Python 源码运行，不能自动替换。请使用封装后的 EXE。")
    return Path(sys.executable).resolve()


def ensure_replaceable(executable: Path) -> None:
    if not executable.is_file():
        raise FileNotFoundError(f"当前 EXE 不存在：{executable}")
    probe = executable.with_name(f".{executable.name}.{os.getpid()}.update-test")
    try:
        probe.write_bytes(b"")
    except OSError as error:
        raise PermissionError("当前 EXE 所在目录不可写，无法自动更新。请将软件移动到可写目录后重试。") from error
    finally:
        probe.unlink(missing_ok=True)


def schedule_replacement(current_exe: Path, downloaded_exe: Path, process_id: int | None = None) -> Path:
    current = current_exe.resolve()
    downloaded = downloaded_exe.resolve()
    ensure_replaceable(current)
    if not downloaded.is_file():
        raise FileNotFoundError(f"已下载的更新文件不存在：{downloaded}")
    root = update_root()
    script = root / f"replace-{uuid.uuid4().hex}.ps1"
    log_path = root / "update.log"
    script.write_text(
        "param([int]$TargetProcessId, [string]$CurrentExe, [string]$DownloadedExe, [string]$LogPath)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        "  while (Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 200 }\n"
        "  for ($attempt = 0; $attempt -lt 30; $attempt++) {\n"
        "    try { Move-Item -LiteralPath $DownloadedExe -Destination $CurrentExe -Force -ErrorAction Stop; Start-Process -FilePath $CurrentExe; exit 0 }\n"
        "    catch { if ($attempt -eq 29) { throw }; Start-Sleep -Milliseconds 500 }\n"
        "  }\n"
        "} catch { $_ | Out-File -LiteralPath $LogPath -Encoding utf8; exit 1 }\n",
        encoding="utf-8",
    )
    subprocess.Popen(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-TargetProcessId", str(process_id or os.getpid()),
            "-CurrentExe", str(current),
            "-DownloadedExe", str(downloaded),
            "-LogPath", str(log_path),
        ],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return script

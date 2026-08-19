"""Retry transient Windows access failures while PyInstaller reads its archive."""

from __future__ import annotations

import time


RETRY_DELAYS = (0.05, 0.1, 0.15, 0.2, 0.25, 0.25, 0.5, 0.5, 1.0, 1.0)


def install_archive_retry(archive_module) -> bool:
    reader = archive_module.ZlibArchiveReader
    original = reader.extract
    if getattr(original, "_cdas_permission_retry", False):
        return False

    def extract_with_retry(self, name, raw=False):
        for delay in RETRY_DELAYS:
            try:
                return original(self, name, raw=raw)
            except PermissionError:
                time.sleep(delay)
        return original(self, name, raw=raw)

    extract_with_retry._cdas_permission_retry = True
    reader.extract = extract_with_retry
    return True


try:
    import pyimod01_archive
except ImportError:
    pyimod01_archive = None

if pyimod01_archive is not None:
    install_archive_retry(pyimod01_archive)

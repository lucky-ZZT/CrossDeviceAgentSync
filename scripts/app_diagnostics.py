#!/usr/bin/env python3
"""Persistent application logging without conversation content or credentials."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


_SENSITIVE_KEYS = {"content", "conversation", "payload", "auth", "token", "secret", "password"}


def log_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "CrossDeviceAgentSync" / "logs"


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if str(key).lower() in _SENSITIVE_KEYS else _safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class AppDiagnostics:
    def __init__(self, application: str, version: str) -> None:
        self.application = application
        self.version = version
        self.session_id = str(uuid.uuid4())
        self.root = log_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "application.log"
        self._lock = threading.Lock()
        self._logger = logging.getLogger(f"CrossDeviceAgentSync.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = logging.handlers.RotatingFileHandler(
            self.path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)
        self.event("application_start", platform=os.name)

    def event(self, event: str, **details: Any) -> None:
        payload = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "level": "INFO",
            "application": self.application,
            "version": self.version,
            "session_id": self.session_id,
            "event": event,
            **_safe(details),
        }
        with self._lock:
            self._logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def error(self, event: str, error: BaseException, traceback_text: str, **details: Any) -> Path:
        payload = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "level": "ERROR",
            "application": self.application,
            "version": self.version,
            "session_id": self.session_id,
            "event": event,
            "error_type": type(error).__name__,
            "message": str(error),
            "traceback": traceback_text,
            **_safe(details),
        }
        if hasattr(error, "report"):
            report = getattr(error, "report")
            if isinstance(report, dict):
                payload["preflight"] = _safe({key: value for key, value in report.items() if key != "operations"})
        with self._lock:
            self._logger.error(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return self.path

    def close(self) -> None:
        for handler in list(self._logger.handlers):
            handler.flush()
            handler.close()
            self._logger.removeHandler(handler)

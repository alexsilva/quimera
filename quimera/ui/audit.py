"""Auditoria de renderização do terminal."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


class RenderAuditLogger:
    """Grava eventos estruturados e o stream ANSI bruto de renderização."""

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.log_dir / "render.jsonl"
        self.ansi_path = self.log_dir / "render.ansi"
        self._lock = threading.RLock()
        self._closed = False
        self._events_handle = self.events_path.open("a", encoding="utf-8")
        self._ansi_handle = self.ansi_path.open("ab")

    def log_event(self, event: str, **payload: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
        }
        if payload:
            record.update({key: _json_safe(value) for key, value in payload.items()})
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            if self._closed:
                return
            self._events_handle.write(line)
            self._events_handle.write("\n")
            self._events_handle.flush()

    def write_ansi(self, data: bytes | str) -> None:
        if isinstance(data, str):
            payload = data.encode("utf-8", errors="replace")
        else:
            payload = data
        with self._lock:
            if self._closed:
                return
            self._ansi_handle.write(payload)
            self._ansi_handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._events_handle.close()
            self._ansi_handle.close()
            self._closed = True

    def __enter__(self) -> "RenderAuditLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

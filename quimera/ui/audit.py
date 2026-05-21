"""Auditoria de renderização do terminal."""

from __future__ import annotations

import json
import threading
import time
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

    _ANSI_BURST_DEDUP_WINDOW_SEC = 0.05
    _ANSI_BURST_MIN_BYTES = 64

    def __init__(self, events_path: Path, ansi_path: Path):
        self.events_path = Path(events_path)
        self.ansi_path = Path(ansi_path)
        self.log_dir = self.events_path.parent
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._events_handle = self.events_path.open("w", encoding="utf-8")
        self._ansi_handle = self.ansi_path.open("wb")
        self._last_ansi_payload: bytes | None = None
        self._last_ansi_ts: float = 0.0
        self._suppressed_ansi_burst_repeats: int = 0

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
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return
            if (
                self._last_ansi_payload is not None
                and payload == self._last_ansi_payload
                and len(payload) >= self._ANSI_BURST_MIN_BYTES
                and (now - self._last_ansi_ts) <= self._ANSI_BURST_DEDUP_WINDOW_SEC
            ):
                self._suppressed_ansi_burst_repeats += 1
                self._last_ansi_ts = now
                return
            self._flush_suppressed_ansi_burst_locked()
            self._ansi_handle.write(payload)
            self._ansi_handle.flush()
            self._last_ansi_payload = payload
            self._last_ansi_ts = now

    def _flush_suppressed_ansi_burst_locked(self) -> None:
        if self._suppressed_ansi_burst_repeats <= 0:
            return
        repeats = int(self._suppressed_ansi_burst_repeats)
        payload_size = len(self._last_ansi_payload or b"")
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": "ansi_duplicate_suppressed",
            "repeats": repeats,
            "payload_bytes": payload_size,
        }
        line = json.dumps(record, ensure_ascii=False)
        self._events_handle.write(line)
        self._events_handle.write("\n")
        self._events_handle.flush()
        self._suppressed_ansi_burst_repeats = 0

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._flush_suppressed_ansi_burst_locked()
            self._events_handle.close()
            self._ansi_handle.close()
            self._closed = True

    def __enter__(self) -> "RenderAuditLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

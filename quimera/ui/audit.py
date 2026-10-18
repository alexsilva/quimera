"""Auditoria de renderização: eventos estruturados por sessão (``--debug``)."""

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
    """Grava eventos estruturados de render em ``render-<sessão>.jsonl``.

    Os renderers em uso escrevem no terminal por conta própria (o Textual pelo
    driver, o de texto puro direto em stdout), então não há stream capturável:
    o audit é só o que cada renderer e os serviços de exibição registram com
    ``log_debug_event``.
    """

    def __init__(self, events_path: Path):
        self.events_path = Path(events_path)
        self.log_dir = self.events_path.parent
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._events_handle = self.events_path.open("w", encoding="utf-8")

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

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._events_handle.close()
            self._closed = True

    def __enter__(self) -> "RenderAuditLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

"""Atomic JSON configuration updates shared by CLI and TUI writers."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Callable

_logger = logging.getLogger(__name__)
_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()


def read_json_object(path: Path, *, strict: bool = False) -> dict:
    """Read settings without exposing their contents in diagnostics."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        message = f"Configuracao invalida ou ilegivel em {path}; corrija o arquivo antes de salvar."
        if strict:
            raise ValueError(message) from exc
        _logger.warning(message)
        return {}


def write_json_object(path: Path, data: dict) -> None:
    """Replace a complete JSON file only after its new contents are flushed."""
    payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _locked_config(path: Path):
    # flock coordinates processes; the RLock serializes threads in this process.
    path = path.resolve()
    with _locks_guard:
        lock = _locks.setdefault(path, threading.RLock())
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def update_json_object(path: Path, update: Callable[[dict], None]) -> None:
    """Serialize read/modify/write and preserve damaged files for recovery."""
    with _locked_config(path):
        data = read_json_object(path, strict=True)
        update(data)
        write_json_object(path, data)

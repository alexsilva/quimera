"""Primitivas comuns para sessões OAuth persistidas por CLIs externos."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from quimera.environment import RuntimeSecrets


class FileOAuthSession:
    """Base de lifecycle OAuth em arquivo, sem conhecer schema ou provedor.

    Os adapters definem caminhos, nomes de campos, expiração, payload e
    endpoints. Locking, secrets, I/O JSON, POST e persistência atômica ficam
    centralizados aqui.
    """

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        runtime_secrets: RuntimeSecrets | None = None,
    ) -> None:
        self._http_client = http_client
        self._runtime_secrets = runtime_secrets
        self._lock = threading.Lock()

    def _secret(self, name: str) -> str:
        value = (
            self._runtime_secrets.get(name)
            if self._runtime_secrets is not None
            else os.environ.get(name)
        )
        return str(value or "").strip()

    @staticmethod
    def _read_json(
        path: Path,
        *,
        error_type: type[Exception],
        missing_message: str,
    ) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise error_type(missing_message) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise error_type(f"Falha ao ler {path}: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _post_json(self, url: str, payload: dict) -> httpx.Response:
        if self._http_client is not None:
            return self._http_client.post(url, json=payload)
        return httpx.post(url, json=payload, timeout=30.0)

    @staticmethod
    def _persist_section(
        path: Path,
        section: str,
        value: dict,
        *,
        error_type: type[Exception],
        error_label: str,
    ) -> None:
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[section] = value
        data["last_refresh"] = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.chmod(tmp_path, 0o600)
            tmp_path.replace(path)
        except OSError as exc:
            raise error_type(f"Falha ao persistir {error_label} em {path}: {exc}") from exc

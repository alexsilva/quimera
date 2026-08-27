"""Compatibilidade de credenciais do ``mcp-remote`` usado pelo transporte remote.

O ``mcp-remote`` passou de stores por versão (``mcp-remote-0.x.y``) para um
store estável por layout (``mcp-remote-v1``). A mudança evita logout em upgrades
futuros, mas não migra automaticamente credenciais já válidas do layout antigo.

Este módulo faz somente a migração conservadora desse salto de layout. Ele não
interpreta tokens, não renova credenciais e não gerencia OAuth no lugar do
``mcp-remote``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_STABLE_STORE_NAME = "mcp-remote-v1"
_LEGACY_STORE_RE = re.compile(r"^mcp-remote-\d+\.\d+\.\d+(?:[-+].*)?$")
_DEFAULT_CALLBACK_PATH = "/oauth/callback"
_DEFAULT_CALLBACK_PORT_BASE = 3335
_DEFAULT_CALLBACK_PORT_SPAN = 45816


@dataclass(frozen=True, slots=True)
class LegacyCredentialMigration:
    """Resultado não sensível de uma migração de credenciais."""

    migrated: bool
    source_store: str | None = None
    destination_store: str | None = None
    reason: str = ""


def migrate_legacy_remote_credentials(
    endpoint: str,
    *,
    env: dict[str, str] | None = None,
    now_ms: int | None = None,
) -> LegacyCredentialMigration:
    """Migra o último par coerente de credenciais para o store estável.

    A migração é intencionalmente restrita ao formato simples ``remote:<url>``.
    Argumentos adicionais do ``mcp-remote`` podem alterar hash, callback ou
    metadata OAuth; nesses casos a função não tenta reproduzir parsing de uma
    dependência externa e deixa o próprio ``mcp-remote`` cuidar do fluxo.

    O store estável nunca é sobrescrito quando já possui ``tokens.json``. Isso
    torna a operação idempotente e evita substituir uma sessão mais nova.
    """
    raw = str(endpoint or "").strip()
    if not raw or any(character.isspace() for character in raw):
        return LegacyCredentialMigration(False, reason="endpoint_not_plain")

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return LegacyCredentialMigration(False, reason="endpoint_not_http")

    base_dir = _config_base_dir(env)
    server_hash = _server_url_hash(raw)
    stable_dir = base_dir / _STABLE_STORE_NAME
    stable_tokens = stable_dir / f"{server_hash}_tokens.json"
    if stable_tokens.is_file():
        return LegacyCredentialMigration(
            False,
            destination_store=stable_dir.name,
            reason="stable_tokens_present",
        )

    expected_redirect = _default_redirect_url(server_hash)
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    candidates = _legacy_candidates(
        base_dir,
        server_hash,
        expected_redirect=expected_redirect,
        now_ms=current_ms,
    )
    if not candidates:
        return LegacyCredentialMigration(
            False,
            destination_store=stable_dir.name,
            reason="legacy_credentials_not_found",
        )

    source_dir, client_info, tokens = candidates[0]
    stable_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        stable_dir.chmod(0o700)
    except OSError:
        pass

    _atomic_write_json(
        stable_dir / f"{server_hash}_client_info.json",
        client_info,
    )
    _atomic_write_json(stable_tokens, tokens)
    return LegacyCredentialMigration(
        True,
        source_store=source_dir.name,
        destination_store=stable_dir.name,
        reason="legacy_credentials_migrated",
    )


def _config_base_dir(env: dict[str, str] | None) -> Path:
    override = None
    if env:
        override = str(env.get("MCP_REMOTE_CONFIG_DIR") or "").strip() or None
    if override is None:
        override = str(os.environ.get("MCP_REMOTE_CONFIG_DIR") or "").strip() or None
    if override:
        return Path(override).expanduser()
    return Path.home() / ".mcp-auth"


def _server_url_hash(server_url: str) -> str:
    # Compatibilidade exata com getServerUrlHash() do mcp-remote para a forma
    # simples sem --resource/--header. MD5 aqui é apenas um identificador de
    # namespace; não é usado como primitiva de segurança.
    return hashlib.md5(server_url.encode("utf-8"), usedforsecurity=False).hexdigest()


def _default_redirect_url(server_hash: str) -> str:
    offset = int(server_hash[:4], 16)
    port = _DEFAULT_CALLBACK_PORT_BASE + offset % _DEFAULT_CALLBACK_PORT_SPAN
    host = "127.0.0.1" if sys.platform == "win32" else "localhost"
    return f"http://{host}:{port}{_DEFAULT_CALLBACK_PATH}"


def _legacy_candidates(
    base_dir: Path,
    server_hash: str,
    *,
    expected_redirect: str,
    now_ms: int,
) -> list[tuple[Path, dict[str, Any], dict[str, Any]]]:
    try:
        stores = [
            path
            for path in base_dir.iterdir()
            if path.is_dir() and _LEGACY_STORE_RE.fullmatch(path.name)
        ]
    except OSError:
        return []

    candidates: list[tuple[float, Path, dict[str, Any], dict[str, Any]]] = []
    for store in stores:
        client_path = store / f"{server_hash}_client_info.json"
        token_path = store / f"{server_hash}_tokens.json"
        client_info = _read_json_object(client_path)
        tokens = _read_json_object(token_path)
        if client_info is None or tokens is None:
            continue
        if not _client_info_matches_redirect(client_info, expected_redirect):
            continue
        if not _tokens_are_reusable(tokens, now_ms=now_ms):
            continue
        try:
            modified_at = token_path.stat().st_mtime
        except OSError:
            continue
        candidates.append((modified_at, store, client_info, tokens))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [
        (store, client_info, tokens)
        for _modified_at, store, client_info, tokens in candidates
    ]


def _client_info_matches_redirect(
    client_info: dict[str, Any],
    expected_redirect: str,
) -> bool:
    client_id = client_info.get("client_id")
    redirect_uris = client_info.get("redirect_uris")
    return (
        isinstance(client_id, str)
        and bool(client_id.strip())
        and isinstance(redirect_uris, list)
        and expected_redirect in redirect_uris
    )


def _tokens_are_reusable(tokens: dict[str, Any], *, now_ms: int) -> bool:
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return False

    expires_at = tokens.get("expires_at")
    if isinstance(expires_at, (int, float)) and now_ms >= int(expires_at):
        refresh_token = tokens.get("refresh_token")
        return isinstance(refresh_token, str) and bool(refresh_token.strip())
    return True


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

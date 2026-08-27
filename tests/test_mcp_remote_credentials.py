from __future__ import annotations

import hashlib
import json
from pathlib import Path

from quimera.runtime.mcp.remote_credentials import migrate_legacy_remote_credentials


def _server_hash(url: str) -> str:
    return hashlib.md5(url.encode("utf-8"), usedforsecurity=False).hexdigest()


def _redirect_url(server_hash: str) -> str:
    port = 3335 + int(server_hash[:4], 16) % 45816
    return f"http://localhost:{port}/oauth/callback"


def _write_credentials(
    root: Path,
    store: str,
    url: str,
    *,
    client_id: str,
    access_token: str,
    refresh_token: str | None = "refresh",
    expires_at: int | None = None,
    redirect_uri: str | None = None,
) -> Path:
    server_hash = _server_hash(url)
    store_root = root / store
    store_root.mkdir(parents=True, exist_ok=True)
    client_info = {
        "client_id": client_id,
        "redirect_uris": [redirect_uri or _redirect_url(server_hash)],
        "token_endpoint_auth_method": "none",
    }
    tokens = {
        "access_token": access_token,
        "token_type": "bearer",
    }
    if refresh_token is not None:
        tokens["refresh_token"] = refresh_token
    if expires_at is not None:
        tokens["expires_at"] = expires_at
    (store_root / f"{server_hash}_client_info.json").write_text(
        json.dumps(client_info),
        encoding="utf-8",
    )
    token_path = store_root / f"{server_hash}_tokens.json"
    token_path.write_text(json.dumps(tokens), encoding="utf-8")
    return token_path


def _read_pair(root: Path, url: str) -> tuple[dict, dict]:
    server_hash = _server_hash(url)
    store = root / "mcp-remote-v1"
    client_info = json.loads(
        (store / f"{server_hash}_client_info.json").read_text(encoding="utf-8")
    )
    tokens = json.loads(
        (store / f"{server_hash}_tokens.json").read_text(encoding="utf-8")
    )
    return client_info, tokens


def test_migrates_latest_coherent_legacy_pair_to_stable_store(tmp_path: Path):
    url = "https://mcp.example.test/mcp"
    older = _write_credentials(
        tmp_path,
        "mcp-remote-0.2.5",
        url,
        client_id="older-client",
        access_token="older-token",
        expires_at=2_000_000,
    )
    newer = _write_credentials(
        tmp_path,
        "mcp-remote-0.3.0",
        url,
        client_id="newer-client",
        access_token="newer-token",
        expires_at=2_000_000,
    )
    older.touch()
    newer.touch()
    # Torna a escolha determinística sem depender da resolução do filesystem.
    older_stat = older.stat()
    newer_stat = newer.stat()
    older.touch()
    newer.touch()
    # O store 0.3.0 é deliberadamente o mais recente.
    import os

    os.utime(older, (older_stat.st_atime, 1000))
    os.utime(newer, (newer_stat.st_atime, 2000))

    result = migrate_legacy_remote_credentials(
        url,
        env={"MCP_REMOTE_CONFIG_DIR": str(tmp_path)},
        now_ms=1_000_000,
    )

    assert result.migrated is True
    assert result.source_store == "mcp-remote-0.3.0"
    assert result.destination_store == "mcp-remote-v1"
    client_info, tokens = _read_pair(tmp_path, url)
    assert client_info["client_id"] == "newer-client"
    assert tokens["access_token"] == "newer-token"


def test_does_not_overwrite_existing_stable_tokens(tmp_path: Path):
    url = "https://mcp.example.test/mcp"
    _write_credentials(
        tmp_path,
        "mcp-remote-0.3.0",
        url,
        client_id="legacy-client",
        access_token="legacy-token",
    )
    _write_credentials(
        tmp_path,
        "mcp-remote-v1",
        url,
        client_id="stable-client",
        access_token="stable-token",
    )

    result = migrate_legacy_remote_credentials(
        url,
        env={"MCP_REMOTE_CONFIG_DIR": str(tmp_path)},
    )

    assert result.migrated is False
    assert result.reason == "stable_tokens_present"
    client_info, tokens = _read_pair(tmp_path, url)
    assert client_info["client_id"] == "stable-client"
    assert tokens["access_token"] == "stable-token"


def test_expired_access_token_is_reusable_when_refresh_token_exists(tmp_path: Path):
    url = "https://mcp.example.test/mcp"
    _write_credentials(
        tmp_path,
        "mcp-remote-0.3.0",
        url,
        client_id="legacy-client",
        access_token="expired-access",
        refresh_token="valid-refresh",
        expires_at=900_000,
    )

    result = migrate_legacy_remote_credentials(
        url,
        env={"MCP_REMOTE_CONFIG_DIR": str(tmp_path)},
        now_ms=1_000_000,
    )

    assert result.migrated is True
    _client_info, tokens = _read_pair(tmp_path, url)
    assert tokens["refresh_token"] == "valid-refresh"


def test_expired_access_token_without_refresh_is_not_migrated(tmp_path: Path):
    url = "https://mcp.example.test/mcp"
    _write_credentials(
        tmp_path,
        "mcp-remote-0.3.0",
        url,
        client_id="legacy-client",
        access_token="expired-access",
        refresh_token=None,
        expires_at=900_000,
    )

    result = migrate_legacy_remote_credentials(
        url,
        env={"MCP_REMOTE_CONFIG_DIR": str(tmp_path)},
        now_ms=1_000_000,
    )

    assert result.migrated is False
    assert result.reason == "legacy_credentials_not_found"


def test_mismatched_redirect_registration_is_not_migrated(tmp_path: Path):
    url = "https://mcp.example.test/mcp"
    _write_credentials(
        tmp_path,
        "mcp-remote-0.3.0",
        url,
        client_id="legacy-client",
        access_token="legacy-token",
        redirect_uri="http://localhost:9999/oauth/callback",
    )

    result = migrate_legacy_remote_credentials(
        url,
        env={"MCP_REMOTE_CONFIG_DIR": str(tmp_path)},
    )

    assert result.migrated is False
    assert result.reason == "legacy_credentials_not_found"


def test_endpoint_with_mcp_remote_arguments_is_not_migrated(tmp_path: Path):
    url = "https://mcp.example.test/mcp"
    _write_credentials(
        tmp_path,
        "mcp-remote-0.3.0",
        url,
        client_id="legacy-client",
        access_token="legacy-token",
    )

    result = migrate_legacy_remote_credentials(
        f"{url} --transport http-only",
        env={"MCP_REMOTE_CONFIG_DIR": str(tmp_path)},
    )

    assert result.migrated is False
    assert result.reason == "endpoint_not_plain"
    assert not (tmp_path / "mcp-remote-v1").exists()


def test_migrated_files_are_private(tmp_path: Path):
    url = "https://mcp.example.test/mcp"
    _write_credentials(
        tmp_path,
        "mcp-remote-0.3.0",
        url,
        client_id="legacy-client",
        access_token="legacy-token",
    )

    result = migrate_legacy_remote_credentials(
        url,
        env={"MCP_REMOTE_CONFIG_DIR": str(tmp_path)},
    )

    assert result.migrated is True
    server_hash = _server_hash(url)
    store = tmp_path / "mcp-remote-v1"
    assert (store.stat().st_mode & 0o777) == 0o700
    assert ((store / f"{server_hash}_client_info.json").stat().st_mode & 0o777) == 0o600
    assert ((store / f"{server_hash}_tokens.json").stat().st_mode & 0o777) == 0o600

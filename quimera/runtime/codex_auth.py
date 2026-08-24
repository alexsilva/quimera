"""Autenticação com a conta do Codex CLI (`~/.codex/auth.json`).

Reusa os tokens OAuth gravados pelo `codex login` para falar diretamente com
o backend Codex da OpenAI, sem executar o binário do Codex CLI. O refresh
segue o mesmo fluxo do CLI (grant `refresh_token` no auth.openai.com) e
persiste os tokens atualizados de volta no arquivo, mantendo as duas
ferramentas logadas com a mesma conta.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

_logger = logging.getLogger(__name__)

CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"

# Margem antes do `exp` do JWT para renovar proativamente.
_REFRESH_MARGIN_SECONDS = 300.0


class CodexAuthError(Exception):
    """Falha ao carregar ou renovar as credenciais do Codex CLI."""


def default_codex_home() -> Path:
    """Retorna o diretório do Codex CLI, respeitando CODEX_HOME."""
    override = (os.environ.get("CODEX_HOME") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".codex"


def _jwt_expiry(token: str) -> float | None:
    """Extrai o claim `exp` de um JWT sem validar assinatura."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, (int, float)):
        return float(exp)
    return None


class CodexCloudAuth:
    """Fornece access token + account id do Codex CLI com refresh automático.

    Thread-safe: múltiplos agentes podem pedir credenciais concorrentemente e
    apenas um refresh acontece por expiração. O arquivo é relido antes de cada
    refresh para aproveitar tokens renovados por outro processo.
    """

    def __init__(
        self,
        codex_home: Path | str | None = None,
        *,
        token_url: str = CODEX_OAUTH_TOKEN_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._codex_home = Path(codex_home) if codex_home else default_codex_home()
        self._token_url = token_url
        self._http_client = http_client
        self._lock = threading.Lock()
        self._tokens: dict | None = None

    @property
    def auth_file(self) -> Path:
        """Retorna o caminho do auth.json do Codex CLI."""
        return self._codex_home / "auth.json"

    def credentials(self, *, force_refresh: bool = False) -> tuple[str, str]:
        """Retorna (access_token, account_id) válidos, renovando se preciso."""
        with self._lock:
            tokens = self._load_tokens()
            access_token = str(tokens.get("access_token") or "")
            if force_refresh or self._needs_refresh(access_token):
                tokens = self._refresh_locked(tokens)
                access_token = str(tokens.get("access_token") or "")
            account_id = str(tokens.get("account_id") or "")
            if not access_token:
                raise CodexAuthError(
                    f"access_token ausente em {self.auth_file}. Rode `codex login` novamente."
                )
            if not account_id:
                raise CodexAuthError(
                    f"account_id ausente em {self.auth_file}. Rode `codex login` novamente."
                )
            return access_token, account_id

    def _load_tokens(self) -> dict:
        """Lê os tokens do auth.json, exigindo login via ChatGPT."""
        path = self.auth_file
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CodexAuthError(
                f"Arquivo de login do Codex CLI não encontrado: {path}. "
                "Rode `codex login` para autenticar."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexAuthError(f"Falha ao ler {path}: {exc}") from exc
        tokens = data.get("tokens")
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            raise CodexAuthError(
                f"{path} não tem tokens de login ChatGPT. Rode `codex login` "
                "(login com conta ChatGPT, não API key)."
            )
        self._tokens = tokens
        return tokens

    @staticmethod
    def _needs_refresh(access_token: str) -> bool:
        if not access_token:
            return True
        expiry = _jwt_expiry(access_token)
        if expiry is None:
            # Sem exp legível: usa como está; o driver força refresh em 401.
            return False
        return time.time() >= (expiry - _REFRESH_MARGIN_SECONDS)

    def _refresh_locked(self, tokens: dict) -> dict:
        """Renova os tokens via OAuth e persiste no auth.json."""
        refresh_token = str(tokens.get("refresh_token") or "")
        if not refresh_token:
            raise CodexAuthError(
                f"refresh_token ausente em {self.auth_file}; impossível renovar. "
                "Rode `codex login` novamente."
            )
        client_id = (os.environ.get("CODEX_OAUTH_CLIENT_ID") or "").strip()
        if not client_id:
            raise CodexAuthError(
                "Variável de ambiente CODEX_OAUTH_CLIENT_ID ausente; "
                "configure-a para renovar o login do Codex."
            )
        payload = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        }
        try:
            if self._http_client is not None:
                response = self._http_client.post(self._token_url, json=payload)
            else:
                response = httpx.post(self._token_url, json=payload, timeout=30.0)
        except httpx.HTTPError as exc:
            raise CodexAuthError(f"Falha de rede ao renovar token do Codex: {exc}") from exc
        if response.status_code != 200:
            raise CodexAuthError(
                f"Refresh do token do Codex falhou (HTTP {response.status_code}). "
                "Rode `codex login` para reautenticar."
            )
        try:
            refreshed = response.json()
        except ValueError as exc:
            raise CodexAuthError(f"Resposta inválida do endpoint de refresh: {exc}") from exc

        new_tokens = dict(tokens)
        for key in ("access_token", "id_token"):
            value = refreshed.get(key)
            if value:
                new_tokens[key] = value
        if refreshed.get("refresh_token"):
            new_tokens["refresh_token"] = refreshed["refresh_token"]
        self._persist(new_tokens)
        self._tokens = new_tokens
        _logger.info("codexcloud: token OAuth renovado com sucesso")
        return new_tokens

    def _persist(self, tokens: dict) -> None:
        """Grava os tokens renovados preservando o restante do auth.json."""
        path = self.auth_file
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["tokens"] = tokens
        data["last_refresh"] = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        tmp_path = path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.chmod(tmp_path, 0o600)
            tmp_path.replace(path)
        except OSError as exc:
            raise CodexAuthError(f"Falha ao persistir tokens renovados em {path}: {exc}") from exc

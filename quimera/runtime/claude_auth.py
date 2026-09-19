"""Autenticação com a conta do Claude Code (`~/.claude/.credentials.json`).

Reusa os tokens OAuth gravados pelo `claude login` (subscription Pro/Max/Team)
para falar diretamente com a Anthropic Messages API, sem executar o binário
`claude`. O refresh segue o fluxo do Claude Code (grant `refresh_token` no
endpoint OAuth da Anthropic) e persiste os tokens atualizados de volta no
arquivo, mantendo as duas ferramentas logadas com a mesma conta.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from quimera.environment import RuntimeSecrets

import httpx

_logger = logging.getLogger(__name__)

CLAUDE_OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLAUDE_OAUTH_TOKEN_URL_FALLBACK = "https://platform.claude.com/v1/oauth/token"

# Margem antes de `expiresAt` para renovar proativamente (ms -> s).
_REFRESH_MARGIN_SECONDS = 300.0


class ClaudeAuthError(Exception):
    """Falha ao carregar ou renovar as credenciais do Claude Code."""


def default_claude_home() -> Path:
    """Retorna o diretório do Claude Code, respeitando CLAUDE_CONFIG_DIR."""
    for env in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME"):
        override = (os.environ.get(env) or "").strip()
        if override:
            return Path(override)
    return Path.home() / ".claude"


class ClaudeCloudAuth:
    """Fornece access token OAuth do Claude Code com refresh automático.

    Thread-safe: múltiplos agentes podem pedir credenciais concorrentemente e
    apenas um refresh acontece por expiração. O arquivo é relido antes de cada
    refresh para aproveitar tokens renovados por outro processo.
    """

    def __init__(
        self,
        claude_home: Path | str | None = None,
        *,
        token_url: str = CLAUDE_OAUTH_TOKEN_URL,
        http_client: httpx.Client | None = None,
        runtime_secrets: RuntimeSecrets | None = None,
    ) -> None:
        self._claude_home = Path(claude_home) if claude_home else default_claude_home()
        self._token_url = token_url
        self._http_client = http_client
        self._runtime_secrets = runtime_secrets
        self._lock = threading.Lock()
        self._oauth: dict | None = None

    @property
    def credentials_file(self) -> Path:
        """Retorna o caminho do .credentials.json do Claude Code."""
        return self._claude_home / ".credentials.json"

    def credentials(self, *, force_refresh: bool = False) -> str:
        """Retorna um access token válido, renovando se preciso."""
        with self._lock:
            oauth = self._load_oauth()
            access_token = str(oauth.get("accessToken") or "")
            if force_refresh or self._needs_refresh(oauth):
                oauth = self._refresh_locked(oauth)
                access_token = str(oauth.get("accessToken") or "")
            if not access_token:
                raise ClaudeAuthError(
                    f"accessToken ausente em {self.credentials_file}. "
                    "Rode `claude login` novamente."
                )
            return access_token

    def _load_oauth(self) -> dict:
        """Lê o bloco claudeAiOauth do .credentials.json."""
        path = self.credentials_file
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ClaudeAuthError(
                f"Arquivo de login do Claude Code não encontrado: {path}. "
                "Rode `claude login` para autenticar."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ClaudeAuthError(f"Falha ao ler {path}: {exc}") from exc
        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict) or not oauth.get("accessToken"):
            raise ClaudeAuthError(
                f"{path} não tem tokens OAuth (claudeAiOauth). Rode `claude login` "
                "(login com subscription, não API key)."
            )
        self._oauth = oauth
        return oauth

    @staticmethod
    def _needs_refresh(oauth: dict) -> bool:
        expires_at = oauth.get("expiresAt")
        try:
            expires_s = float(expires_at) / 1000.0
        except (TypeError, ValueError):
            # Sem expiração legível: usa como está; o driver força refresh em 401.
            return False
        return time.time() >= (expires_s - _REFRESH_MARGIN_SECONDS)

    def _refresh_locked(self, oauth: dict) -> dict:
        """Renova os tokens via OAuth e persiste no .credentials.json."""
        refresh_token = str(oauth.get("refreshToken") or "")
        if not refresh_token:
            raise ClaudeAuthError(
                f"refreshToken ausente em {self.credentials_file}; impossível renovar. "
                "Rode `claude login` novamente."
            )
        client_id = (
            self._runtime_secrets.get("CLAUDE_OAUTH_CLIENT_ID")
            if self._runtime_secrets is not None
            else os.environ.get("CLAUDE_OAUTH_CLIENT_ID")
        )
        client_id = (client_id or "").strip()
        if not client_id:
            raise ClaudeAuthError(
                "Variável de ambiente CLAUDE_OAUTH_CLIENT_ID ausente; "
                "configure-a para renovar o login do Claude."
            )
        payload = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        last_error: Exception | None = None
        for url in (self._token_url, CLAUDE_OAUTH_TOKEN_URL_FALLBACK):
            try:
                if self._http_client is not None:
                    response = self._http_client.post(url, json=payload)
                else:
                    response = httpx.post(url, json=payload, timeout=30.0)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if response.status_code != 200:
                last_error = ClaudeAuthError(
                    f"Refresh do token do Claude falhou (HTTP {response.status_code}). "
                    "Rode `claude login` para reautenticar."
                )
                continue
            try:
                refreshed = response.json()
            except ValueError as exc:
                last_error = ClaudeAuthError(
                    f"Resposta inválida do endpoint de refresh: {exc}"
                )
                continue
            new_oauth = dict(oauth)
            if refreshed.get("access_token"):
                new_oauth["accessToken"] = refreshed["access_token"]
            if refreshed.get("refresh_token"):
                new_oauth["refreshToken"] = refreshed["refresh_token"]
            expires_in = refreshed.get("expires_in")
            try:
                if expires_in:
                    new_oauth["expiresAt"] = int(time.time() * 1000) + int(expires_in) * 1000
            except (TypeError, ValueError):
                pass
            self._persist(new_oauth)
            self._oauth = new_oauth
            _logger.info("claudecloud: token OAuth renovado com sucesso")
            return new_oauth
        if isinstance(last_error, ClaudeAuthError):
            raise last_error
        raise ClaudeAuthError(
            f"Falha de rede ao renovar token do Claude: {last_error}"
        ) from last_error

    def _persist(self, oauth: dict) -> None:
        """Grava os tokens renovados preservando o restante do arquivo."""
        path = self.credentials_file
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["claudeAiOauth"] = oauth
        data["last_refresh"] = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        tmp_path = path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.chmod(tmp_path, 0o600)
            tmp_path.replace(path)
        except OSError as exc:
            raise ClaudeAuthError(
                f"Falha ao persistir tokens renovados em {path}: {exc}"
            ) from exc

"""Authorization Server OAuth 2.1 embutido para o servidor MCP HTTP do Quimera.

Implementa o conjunto de RFCs exigido pela especificação MCP de autorização:

* RFC 6749 / OAuth 2.1 — ``authorization_code`` com PKCE, ``refresh_token`` e
  ``client_credentials``.
* RFC 7636 — PKCE (``S256`` obrigatório por padrão).
* RFC 7591 — Dynamic Client Registration (``POST /oauth/register``).
* RFC 7009 — Token Revocation (``POST /oauth/revoke``).
* RFC 7662 — Token Introspection (``POST /oauth/introspect``).
* RFC 8414 — Authorization Server Metadata.
* RFC 8707 — Resource Indicators (``resource`` audience binding).
* RFC 9728 — Protected Resource Metadata + ``WWW-Authenticate``.

O provider é intencionalmente auto-contido: tokens são opacos, guardados em
memória, e apenas clients registrados dinamicamente e refresh tokens são
persistidos em disco (JSON atômico). Sem ``QUIMERA_MCP_OAUTH_STORE_KEY`` o
arquivo fica em texto claro (``client_secret`` e refresh tokens legíveis;
protegido só por permissões ``0600``). Com a chave definida, o payload é
criptografado com Fernet (pacote opcional ``cryptography``).

Uso mínimo::

    provider = OAuthProvider(OAuthConfig(enabled=True))
    httpd = MCP_HTTPServer(mcp, oauth=provider)

O esquema legado de token estático em header (``Authorization: Bearer <token>``
ou ``X-Quimera-MCP-Token``) continua válido em paralelo — ver
``MCP_HTTPServer._authenticate``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse

_logger = logging.getLogger(__name__)

ENV_OAUTH_ENABLED = "QUIMERA_MCP_OAUTH"
ENV_OAUTH_ISSUER = "QUIMERA_MCP_OAUTH_ISSUER"
ENV_OAUTH_CLIENTS = "QUIMERA_MCP_OAUTH_CLIENTS"
ENV_OAUTH_REDIRECT_URIS = "QUIMERA_MCP_OAUTH_REDIRECT_URIS"
ENV_OAUTH_PASSCODE = "QUIMERA_MCP_OAUTH_PASSCODE"
ENV_OAUTH_AUTO_APPROVE = "QUIMERA_MCP_OAUTH_AUTO_APPROVE"
ENV_OAUTH_ALLOW_REGISTER = "QUIMERA_MCP_OAUTH_ALLOW_REGISTER"
ENV_OAUTH_STORE = "QUIMERA_MCP_OAUTH_STORE"
ENV_OAUTH_ACCESS_TTL = "QUIMERA_MCP_OAUTH_ACCESS_TTL"
ENV_OAUTH_REFRESH_TTL = "QUIMERA_MCP_OAUTH_REFRESH_TTL"
ENV_OAUTH_STORE_KEY = "QUIMERA_MCP_OAUTH_STORE_KEY"

GRANT_AUTHORIZATION_CODE = "authorization_code"
GRANT_REFRESH_TOKEN = "refresh_token"
GRANT_CLIENT_CREDENTIALS = "client_credentials"

#: Escopo neutro: herda o perfil de tools configurado no transporte HTTP.
SCOPE_DEFAULT = "mcp"

#: Escopos que restringem o perfil de tools do transporte para o token emitido.
SCOPE_TOOL_PROFILES: dict[str, str] = {
    "mcp": "",
    "mcp:read-local": "read-local",
    "mcp:read": "read",
    "mcp:agent": "agent",
    "mcp:all": "all",
}

SUPPORTED_SCOPES: tuple[str, ...] = tuple(SCOPE_TOOL_PROFILES)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str, default: bool = False) -> bool:
    """Lê uma variável de ambiente booleana tolerante a formatos comuns."""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in _TRUE_VALUES


def _env_int(name: str, default: int) -> int:
    """Lê uma variável de ambiente inteira, ignorando valores inválidos."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _logger.warning("MCP OAuth: valor inválido em %s=%r; usando %d", name, raw, default)
        return default
    return value if value > 0 else default


def _split_csv(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normaliza CSV ou iterável de strings em tupla sem itens vazios."""
    if value is None:
        return ()
    items: Iterable[str]
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = value
    return tuple(str(item).strip() for item in items if str(item).strip())


def normalize_scope(scope: str | Iterable[str] | None) -> str:
    """Normaliza um escopo OAuth para string separada por espaços, sem duplicatas."""
    if scope is None:
        return ""
    if isinstance(scope, str):
        parts = scope.replace(",", " ").split()
    else:
        parts = [str(item) for item in scope]
    seen: list[str] = []
    for part in parts:
        token = part.strip()
        if token and token not in seen:
            seen.append(token)
    return " ".join(seen)


class OAuthError(Exception):
    """Erro OAuth serializável como resposta ``application/json``.

    Attributes:
        error: Código de erro OAuth (ex: ``invalid_grant``).
        description: Mensagem legível para o desenvolvedor.
        status: Status HTTP associado.
    """

    def __init__(self, error: str, description: str = "", status: int = 400) -> None:
        """Inicializa o erro OAuth."""
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description
        self.status = status

    def to_dict(self) -> dict[str, str]:
        """Retorna o corpo JSON padrão de erro OAuth."""
        payload = {"error": self.error}
        if self.description:
            payload["error_description"] = self.description
        return payload


class OAuthRedirectError(Exception):
    """Erro do endpoint de autorização que deve virar redirect para o client.

    Usado quando ``client_id`` e ``redirect_uri`` já foram validados e, portanto,
    devolver o erro ao client é seguro (RFC 6749 §4.1.2.1).
    """

    def __init__(self, redirect_uri: str, state: str, error: str, description: str = "") -> None:
        """Inicializa o erro com o destino do redirect."""
        super().__init__(f"{error}: {description}" if description else error)
        self.redirect_uri = redirect_uri
        self.state = state
        self.error = error
        self.description = description


@dataclass
class OAuthClient:
    """Client OAuth registrado (estático via configuração ou dinâmico via RFC 7591)."""

    client_id: str
    client_secret: str | None = None
    client_name: str = ""
    redirect_uris: tuple[str, ...] = ()
    grant_types: tuple[str, ...] = (GRANT_AUTHORIZATION_CODE, GRANT_REFRESH_TOKEN)
    scope: str = SCOPE_DEFAULT
    created_at: float = 0.0
    dynamic: bool = False

    @property
    def is_public(self) -> bool:
        """``True`` quando o client não possui secret (PKCE é obrigatório)."""
        return not self.client_secret

    @property
    def token_endpoint_auth_method(self) -> str:
        """Método de autenticação anunciado para este client."""
        return "none" if self.is_public else "client_secret_post"

    def allows_grant(self, grant_type: str) -> bool:
        """Indica se o *grant_type* foi habilitado para este client."""
        return grant_type in self.grant_types

    def allows_redirect_uri(self, redirect_uri: str) -> bool:
        """Valida o ``redirect_uri`` por comparação exata, com exceção de loopback.

        Conforme RFC 8252 §7.3, redirects em ``127.0.0.1``/``[::1]`` podem usar
        porta efêmera; comparamos ignorando a porta nesse caso.
        """
        if redirect_uri in self.redirect_uris:
            return True
        candidate = urlparse(redirect_uri)
        if candidate.hostname not in ("127.0.0.1", "::1", "localhost"):
            return False
        for registered in self.redirect_uris:
            known = urlparse(registered)
            if known.hostname not in ("127.0.0.1", "::1", "localhost"):
                continue
            if known.scheme == candidate.scheme and known.path == candidate.path:
                return True
        return False

    def check_secret(self, provided: str | None) -> bool:
        """Compara o secret informado em tempo constante."""
        if self.is_public:
            return not provided
        return hmac.compare_digest(str(self.client_secret), str(provided or ""))

    def to_dict(self) -> dict:
        """Serializa o client para persistência JSON."""
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "client_name": self.client_name,
            "redirect_uris": list(self.redirect_uris),
            "grant_types": list(self.grant_types),
            "scope": self.scope,
            "created_at": self.created_at,
            "dynamic": self.dynamic,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> OAuthClient:
        """Reconstrói um client a partir do JSON persistido."""
        return cls(
            client_id=str(data.get("client_id") or ""),
            client_secret=data.get("client_secret") or None,
            client_name=str(data.get("client_name") or ""),
            redirect_uris=tuple(str(uri) for uri in data.get("redirect_uris") or ()),
            grant_types=tuple(str(grant) for grant in data.get("grant_types") or ())
            or (GRANT_AUTHORIZATION_CODE, GRANT_REFRESH_TOKEN),
            scope=str(data.get("scope") or SCOPE_DEFAULT),
            created_at=float(data.get("created_at") or 0.0),
            dynamic=bool(data.get("dynamic", False)),
        )

    def registration_response(self) -> dict:
        """Resposta RFC 7591 para ``POST /oauth/register``."""
        payload: dict = {
            "client_id": self.client_id,
            "client_id_issued_at": int(self.created_at),
            "client_name": self.client_name or self.client_id,
            "redirect_uris": list(self.redirect_uris),
            "grant_types": list(self.grant_types),
            "response_types": ["code"],
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
            "scope": self.scope,
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret
            payload["client_secret_expires_at"] = 0
        return payload


@dataclass
class AuthorizationRequest:
    """Pedido de autorização validado, aguardando consentimento humano."""

    request_id: str
    client_id: str
    client_name: str
    redirect_uri: str
    scope: str
    state: str
    code_challenge: str
    code_challenge_method: str
    resource: str
    expires_at: float

    def is_expired(self, now: float | None = None) -> bool:
        """Indica se o pedido de autorização já expirou."""
        return (now if now is not None else time.time()) >= self.expires_at


@dataclass
class AuthorizationCode:
    """Código de autorização de uso único vinculado ao PKCE do client."""

    code: str
    client_id: str
    redirect_uri: str
    scope: str
    code_challenge: str
    code_challenge_method: str
    resource: str
    expires_at: float

    def is_expired(self, now: float | None = None) -> bool:
        """Indica se o código já expirou."""
        return (now if now is not None else time.time()) >= self.expires_at


@dataclass
class IssuedToken:
    """Access ou refresh token opaco emitido pelo provider."""

    token: str
    client_id: str
    scope: str
    resource: str
    expires_at: float
    kind: str = "access"

    def is_expired(self, now: float | None = None) -> bool:
        """Indica se o token já expirou."""
        return (now if now is not None else time.time()) >= self.expires_at

    def to_dict(self) -> dict:
        """Serializa o token para persistência JSON."""
        return {
            "token": self.token,
            "client_id": self.client_id,
            "scope": self.scope,
            "resource": self.resource,
            "expires_at": self.expires_at,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> IssuedToken:
        """Reconstrói um token a partir do JSON persistido."""
        return cls(
            token=str(data.get("token") or ""),
            client_id=str(data.get("client_id") or ""),
            scope=str(data.get("scope") or SCOPE_DEFAULT),
            resource=str(data.get("resource") or ""),
            expires_at=float(data.get("expires_at") or 0.0),
            kind=str(data.get("kind") or "access"),
        )


@dataclass
class AuthContext:
    """Resultado da autenticação de uma requisição HTTP no transporte MCP."""

    authenticated: bool
    mode: str = "anonymous"
    client_id: str = ""
    scope: str = ""
    tool_profile: str = ""
    error: str = ""
    error_description: str = ""
    status: int = 401


@dataclass
class OAuthConfig:
    """Configuração declarativa do Authorization Server embutido.

    Attributes:
        enabled: Liga o fluxo OAuth. Quando ``False``, apenas o token estático
            de header é aceito.
        issuer: URL pública do issuer. Vazio ⇒ derivada da requisição (suporta
            túneis como ngrok/cloudflared via ``X-Forwarded-*``).
        clients: Clients estáticos pré-registrados.
        allow_dynamic_registration: Habilita RFC 7591 (padrão ``True``), o que
            permite que clients MCP se conectem sem configuração manual.
        require_pkce: Exige ``code_challenge`` com ``S256`` (padrão ``True``).
        auto_approve: Emite o código sem tela de consentimento (uso local/dev).
        passcode: Código exigido na tela de consentimento; vazio ⇒ sem passcode.
        access_token_ttl: Validade do access token, em segundos.
        refresh_token_ttl: Validade do refresh token, em segundos.
        code_ttl: Validade do código de autorização, em segundos.
        store_path: Arquivo JSON de persistência; ``None`` ⇒ somente memória.
        store_key: Passphrase para criptografar o store em disco (Fernet).
            Vazio ⇒ arquivo em texto claro (``client_secret`` e refresh tokens
            legíveis). Requer o pacote opcional ``cryptography`` quando definida.
    """

    enabled: bool = False
    issuer: str = ""
    clients: tuple[OAuthClient, ...] = ()
    allow_dynamic_registration: bool = True
    require_pkce: bool = True
    auto_approve: bool = False
    passcode: str = ""
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 30 * 24 * 3600
    code_ttl: int = 300
    store_path: Path | None = None
    store_key: str = ""

    @classmethod
    def from_env(cls, **overrides) -> OAuthConfig:
        """Monta a configuração a partir das variáveis ``QUIMERA_MCP_OAUTH*``.

        Argumentos em *overrides* têm precedência sobre o ambiente, permitindo
        que flags de CLI sobreponham o ambiente sem código extra.
        """
        redirect_uris = _split_csv(os.environ.get(ENV_OAUTH_REDIRECT_URIS))
        resolved: dict = {
            "enabled": _env_flag(ENV_OAUTH_ENABLED),
            "issuer": (os.environ.get(ENV_OAUTH_ISSUER) or "").strip(),
            "clients": parse_client_specs(
                os.environ.get(ENV_OAUTH_CLIENTS), redirect_uris=redirect_uris
            ),
            "allow_dynamic_registration": _env_flag(ENV_OAUTH_ALLOW_REGISTER, True),
            "auto_approve": _env_flag(ENV_OAUTH_AUTO_APPROVE),
            "passcode": (os.environ.get(ENV_OAUTH_PASSCODE) or "").strip(),
            "access_token_ttl": _env_int(ENV_OAUTH_ACCESS_TTL, 3600),
            "refresh_token_ttl": _env_int(ENV_OAUTH_REFRESH_TTL, 30 * 24 * 3600),
        }
        store_raw = (os.environ.get(ENV_OAUTH_STORE) or "").strip()
        if store_raw:
            resolved["store_path"] = Path(store_raw).expanduser()
        store_key = (os.environ.get(ENV_OAUTH_STORE_KEY) or "").strip()
        if store_key:
            resolved["store_key"] = store_key
        resolved.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**resolved)


def parse_client_specs(
    specs: str | Iterable[str] | None,
    *,
    redirect_uris: Iterable[str] = (),
) -> tuple[OAuthClient, ...]:
    """Converte especificações ``id[:secret]`` em clients estáticos.

    Um client sem secret é público (exige PKCE); com secret ele também aceita
    ``client_credentials``, servindo como substituto rotativo do token estático.

    Args:
        specs: CSV ou iterável de ``client_id`` ou ``client_id:client_secret``.
        redirect_uris: Redirects permitidos para todos os clients estáticos.

    Returns:
        Tupla de ``OAuthClient`` na ordem informada.
    """
    uris = tuple(_split_csv(redirect_uris))
    clients: list[OAuthClient] = []
    now = time.time()
    for spec in _split_csv(specs):
        client_id, _, secret = spec.partition(":")
        client_id = client_id.strip()
        if not client_id:
            continue
        secret = secret.strip()
        grants = [GRANT_AUTHORIZATION_CODE, GRANT_REFRESH_TOKEN]
        if secret:
            grants.append(GRANT_CLIENT_CREDENTIALS)
        clients.append(
            OAuthClient(
                client_id=client_id,
                client_secret=secret or None,
                client_name=client_id,
                redirect_uris=uris,
                grant_types=tuple(grants),
                created_at=now,
            )
        )
    return tuple(clients)


class OAuthStore:
    """Persistência JSON atômica do estado OAuth durável.

    Clients dinâmicos, access tokens ainda válidos e refresh tokens sobrevivem
    a reinícios do servidor. Códigos de autorização e pedidos pendentes são
    deliberadamente voláteis porque pertencem ao fluxo interativo em andamento.

    **Segurança em disco:** sem ``store_key``, o arquivo contém ``client_secret``
    de clients dinâmicos, access tokens e refresh tokens em texto claro (apenas permissões
    ``0600`` protegem o conteúdo). Com ``store_key`` definida, o payload é
    criptografado com Fernet (requer o pacote opcional ``cryptography``).
    """

    _VERSION = 1
    _ENCRYPTED_PREFIX = "quimera-oauth-fernet:v1:"
    _PBKDF2_SALT = b"quimera-mcp-oauth-store-v1"
    _PBKDF2_ITERATIONS = 390_000

    def __init__(self, path: Path | None = None, store_key: str = "") -> None:
        """Inicializa o store; ``path`` ``None`` mantém tudo apenas em memória.

        Args:
            path: Caminho do arquivo JSON (ou criptografado).
            store_key: Passphrase de criptografia. Vazia ⇒ persistência em claro.
        """
        self._path = Path(path).expanduser() if path else None
        self._store_key = (store_key or "").strip()
        self._lock = threading.Lock()
        self._fernet = self._build_fernet(self._store_key) if self._store_key else None

    @property
    def path(self) -> Path | None:
        """Arquivo de persistência ativo, ou ``None`` no modo memória."""
        return self._path

    @property
    def encrypted(self) -> bool:
        """``True`` quando o store grava e lê payload criptografado."""
        return self._fernet is not None

    @classmethod
    def _build_fernet(cls, passphrase: str):
        """Deriva uma chave Fernet a partir da passphrase do usuário.

        Returns:
            Instância ``Fernet`` ou ``None`` se ``cryptography`` não estiver
            instalado (nesse caso a criptografia fica desabilitada com warning).
        """
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            _logger.warning(
                "MCP OAuth: %s definida, mas o pacote 'cryptography' não está "
                "instalado; o store permanecerá em texto claro. Instale com: "
                "pip install cryptography",
                ENV_OAUTH_STORE_KEY,
            )
            return None
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            passphrase.encode("utf-8"),
            cls._PBKDF2_SALT,
            cls._PBKDF2_ITERATIONS,
            dklen=32,
        )
        return Fernet(base64.urlsafe_b64encode(derived))

    def _encode_payload(self, payload: dict) -> str:
        """Serializa o payload JSON, opcionalmente criptografado."""
        raw = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        if self._fernet is None:
            return raw
        token = self._fernet.encrypt(raw.encode("utf-8")).decode("ascii")
        return f"{self._ENCRYPTED_PREFIX}{token}\n"

    def _decode_payload(self, text: str) -> dict:
        """Deserializa o conteúdo do arquivo (claro ou criptografado)."""
        stripped = text.lstrip()
        if stripped.startswith(self._ENCRYPTED_PREFIX):
            if self._fernet is None:
                raise ValueError(
                    "store criptografado, mas nenhuma chave válida está configurada "
                    f"({ENV_OAUTH_STORE_KEY}) ou 'cryptography' está ausente"
                )
            token = stripped[len(self._ENCRYPTED_PREFIX) :].strip()
            plain = self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
            return json.loads(plain)
        if self._fernet is not None:
            _logger.warning(
                "MCP OAuth: store em %s está em texto claro, mas %s está definida; "
                "a próxima gravação será criptografada",
                self._path,
                ENV_OAUTH_STORE_KEY,
            )
        return json.loads(text)

    def load_state(
        self,
    ) -> tuple[
        dict[str, OAuthClient],
        dict[str, IssuedToken],
        dict[str, IssuedToken],
    ]:
        """Carrega clients, access e refresh tokens válidos, tolerando corrupção."""
        if not self._path or not self._path.exists():
            return {}, {}, {}
        try:
            data = self._decode_payload(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            _logger.warning("MCP OAuth: store ilegível em %s (%s); recomeçando vazio", self._path, exc)
            return {}, {}, {}
        except Exception as exc:  # noqa: BLE001 — InvalidToken e erros do Fernet
            _logger.warning("MCP OAuth: store ilegível em %s (%s); recomeçando vazio", self._path, exc)
            return {}, {}, {}
        clients = {}
        for raw in data.get("clients") or []:
            client = OAuthClient.from_dict(raw)
            if client.client_id:
                clients[client.client_id] = client
        access_tokens = {}
        for raw in data.get("access_tokens") or []:
            token = IssuedToken.from_dict(raw)
            if token.token and not token.is_expired():
                access_tokens[token.token] = token
        refresh_tokens = {}
        for raw in data.get("refresh_tokens") or []:
            token = IssuedToken.from_dict(raw)
            if token.token and not token.is_expired():
                refresh_tokens[token.token] = token
        return clients, access_tokens, refresh_tokens

    def load(self) -> tuple[dict[str, OAuthClient], dict[str, IssuedToken]]:
        """Compatibilidade: carrega clients e refresh tokens como nas versões anteriores."""
        clients, _, refresh_tokens = self.load_state()
        return clients, refresh_tokens

    def save(
        self,
        clients: Mapping[str, OAuthClient],
        refresh_tokens: Mapping[str, IssuedToken],
        access_tokens: Mapping[str, IssuedToken] | None = None,
    ) -> None:
        """Grava o estado de forma atômica, ignorando falhas de I/O não fatais."""
        if not self._path:
            return
        payload = {
            "version": self._VERSION,
            "clients": [client.to_dict() for client in clients.values() if client.dynamic],
            "access_tokens": [
                token.to_dict()
                for token in (access_tokens or {}).values()
                if not token.is_expired()
            ],
            "refresh_tokens": [
                token.to_dict() for token in refresh_tokens.values() if not token.is_expired()
            ],
        }
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
                tmp_path.write_text(self._encode_payload(payload), encoding="utf-8")
                os.replace(tmp_path, self._path)
                os.chmod(self._path, 0o600)
            except OSError as exc:
                _logger.warning("MCP OAuth: falha ao persistir store em %s (%s)", self._path, exc)


class OAuthProvider:
    """Authorization Server OAuth 2.1 em processo para o transporte MCP HTTP.

    Todos os métodos são seguros para uso concorrente pelo ``ThreadingHTTPServer``.
    """

    #: Caminhos servidos pelo provider, relativos à raiz do servidor HTTP.
    AUTHORIZE_PATH = "/oauth/authorize"
    TOKEN_PATH = "/oauth/token"
    REGISTER_PATH = "/oauth/register"
    REVOKE_PATH = "/oauth/revoke"
    INTROSPECT_PATH = "/oauth/introspect"
    RESOURCE_PATH = "/mcp"
    METADATA_AS_PATH = "/.well-known/oauth-authorization-server"
    METADATA_PR_PATH = "/.well-known/oauth-protected-resource"

    def __init__(self, config: OAuthConfig | None = None) -> None:
        """Inicializa o provider e restaura o estado persistido."""
        self._config = config or OAuthConfig()
        self._store = OAuthStore(self._config.store_path, store_key=self._config.store_key)
        self._lock = threading.RLock()
        self._clients: dict[str, OAuthClient] = {}
        self._pending: dict[str, AuthorizationRequest] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, IssuedToken] = {}
        self._refresh_tokens: dict[str, IssuedToken] = {}
        stored_clients, stored_access, stored_refresh = self._store.load_state()
        self._clients.update(stored_clients)
        for client in self._config.clients:
            self._clients[client.client_id] = client
        known_clients = set(self._clients)
        self._access_tokens = {
            token.token: token
            for token in stored_access.values()
            if token.client_id in known_clients
        }
        self._refresh_tokens = {
            token.token: token
            for token in stored_refresh.values()
            if token.client_id in known_clients
        }

    # ------------------------------------------------------------------
    # Estado e configuração
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Indica se o fluxo OAuth está ativo."""
        return self._config.enabled

    @property
    def config(self) -> OAuthConfig:
        """Configuração efetiva do provider."""
        return self._config

    @property
    def clients(self) -> dict[str, OAuthClient]:
        """Cópia do mapa de clients conhecidos."""
        with self._lock:
            return dict(self._clients)

    def issuer_for(self, base_url: str) -> str:
        """Retorna o issuer configurado ou o derivado da requisição."""
        return (self._config.issuer or base_url).rstrip("/")

    @staticmethod
    def profile_for_scope(scope: str) -> str:
        """Mapeia o escopo concedido ao perfil de tools mais restritivo pedido.

        Retorna string vazia quando o escopo não restringe o perfil configurado
        no transporte HTTP.
        """
        for token in normalize_scope(scope).split():
            profile = SCOPE_TOOL_PROFILES.get(token)
            if profile:
                return profile
        return ""

    def resolve_scope(self, requested: str, client: OAuthClient) -> str:
        """Valida o escopo pedido contra os suportados e o escopo do client."""
        allowed = normalize_scope(client.scope) or SCOPE_DEFAULT
        allowed_set = set(allowed.split())
        requested_scope = normalize_scope(requested)
        if not requested_scope:
            return allowed
        granted: list[str] = []
        for token in requested_scope.split():
            if token not in SCOPE_TOOL_PROFILES:
                raise OAuthError("invalid_scope", f"Escopo desconhecido: {token}")
            if allowed == SCOPE_DEFAULT or token in allowed_set or SCOPE_DEFAULT in allowed_set:
                granted.append(token)
            else:
                raise OAuthError("invalid_scope", f"Escopo não autorizado para o client: {token}")
        return " ".join(granted)

    # ------------------------------------------------------------------
    # Metadados (RFC 8414 / RFC 9728)
    # ------------------------------------------------------------------

    def authorization_server_metadata(self, base_url: str) -> dict:
        """Documento RFC 8414 publicado em ``/.well-known/oauth-authorization-server``."""
        issuer = self.issuer_for(base_url)
        grant_types = [GRANT_AUTHORIZATION_CODE, GRANT_REFRESH_TOKEN]
        if any(
            client.allows_grant(GRANT_CLIENT_CREDENTIALS) for client in self.clients.values()
        ):
            grant_types.append(GRANT_CLIENT_CREDENTIALS)
        metadata: dict = {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}{self.AUTHORIZE_PATH}",
            "token_endpoint": f"{issuer}{self.TOKEN_PATH}",
            "revocation_endpoint": f"{issuer}{self.REVOKE_PATH}",
            "introspection_endpoint": f"{issuer}{self.INTROSPECT_PATH}",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": grant_types,
            "code_challenge_methods_supported": ["S256"] if self._config.require_pkce else ["S256", "plain"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "scopes_supported": list(SUPPORTED_SCOPES),
            "service_documentation": "https://modelcontextprotocol.io/specification/basic/authorization",
        }
        if self._config.allow_dynamic_registration:
            metadata["registration_endpoint"] = f"{issuer}{self.REGISTER_PATH}"
        return metadata

    def protected_resource_metadata(self, base_url: str) -> dict:
        """Documento RFC 9728 do recurso protegido ``/mcp``."""
        issuer = self.issuer_for(base_url)
        metadata: dict = {
            "resource": f"{issuer}{self.RESOURCE_PATH}",
            "bearer_methods_supported": ["header"],
            "scopes_supported": list(SUPPORTED_SCOPES),
        }
        if self.enabled:
            metadata["authorization_servers"] = [issuer]
        return metadata

    def www_authenticate(
        self,
        base_url: str,
        *,
        error: str = "",
        description: str = "",
    ) -> str:
        """Monta o header ``WWW-Authenticate`` que aponta ao metadata do recurso."""
        issuer = self.issuer_for(base_url)
        parts = [f'Bearer realm="{issuer}{self.RESOURCE_PATH}"']
        if self.enabled:
            parts.append(
                f'resource_metadata="{issuer}{self.METADATA_PR_PATH}{self.RESOURCE_PATH}"'
            )
        if error:
            parts.append(f'error="{error}"')
        if description:
            parts.append(f'error_description="{description}"')
        return ", ".join(parts)

    # ------------------------------------------------------------------
    # Dynamic Client Registration (RFC 7591)
    # ------------------------------------------------------------------

    def register_client(self, payload: Mapping) -> OAuthClient:
        """Registra um client dinamicamente conforme RFC 7591.

        Raises:
            OAuthError: Se o registro dinâmico estiver desabilitado ou o payload
                for inválido.
        """
        if not self.enabled:
            raise OAuthError("invalid_request", "OAuth não está habilitado", status=404)
        if not self._config.allow_dynamic_registration:
            raise OAuthError(
                "invalid_client_metadata",
                "Registro dinâmico desabilitado; configure --mcp-oauth-client",
                status=403,
            )
        redirect_uris = tuple(
            str(uri).strip() for uri in payload.get("redirect_uris") or () if str(uri).strip()
        )
        grant_types = tuple(
            str(grant).strip()
            for grant in payload.get("grant_types") or ()
            if str(grant).strip()
        ) or (GRANT_AUTHORIZATION_CODE, GRANT_REFRESH_TOKEN)
        unsupported = set(grant_types) - {
            GRANT_AUTHORIZATION_CODE,
            GRANT_REFRESH_TOKEN,
            GRANT_CLIENT_CREDENTIALS,
        }
        if unsupported:
            raise OAuthError(
                "invalid_client_metadata",
                f"grant_types não suportados: {', '.join(sorted(unsupported))}",
            )
        if GRANT_AUTHORIZATION_CODE in grant_types and not redirect_uris:
            raise OAuthError(
                "invalid_redirect_uri",
                "redirect_uris é obrigatório para authorization_code",
            )
        for uri in redirect_uris:
            self._validate_redirect_uri_format(uri)
        auth_method = str(payload.get("token_endpoint_auth_method") or "none").strip()
        if auth_method not in ("none", "client_secret_post", "client_secret_basic"):
            raise OAuthError(
                "invalid_client_metadata",
                f"token_endpoint_auth_method não suportado: {auth_method}",
            )
        requested_scope = normalize_scope(payload.get("scope")) or SCOPE_DEFAULT
        for token in requested_scope.split():
            if token not in SCOPE_TOOL_PROFILES:
                raise OAuthError("invalid_client_metadata", f"Escopo desconhecido: {token}")
        client = OAuthClient(
            client_id=f"quimera-{secrets.token_urlsafe(12)}",
            client_secret=None if auth_method == "none" else secrets.token_urlsafe(32),
            client_name=str(payload.get("client_name") or "").strip(),
            redirect_uris=redirect_uris,
            grant_types=grant_types,
            scope=requested_scope,
            created_at=time.time(),
            dynamic=True,
        )
        with self._lock:
            self._clients[client.client_id] = client
            self._persist_locked()
        _logger.info(
            "MCP OAuth: client registrado dinamicamente id=%s name=%r",
            client.client_id,
            client.client_name,
        )
        return client

    @staticmethod
    def _validate_redirect_uri_format(uri: str) -> None:
        """Rejeita redirects sem esquema ou com fragmento (RFC 6749 §3.1.2)."""
        parsed = urlparse(uri)
        if not parsed.scheme:
            raise OAuthError("invalid_redirect_uri", f"redirect_uri sem esquema: {uri}")
        if parsed.fragment:
            raise OAuthError("invalid_redirect_uri", f"redirect_uri não pode ter fragmento: {uri}")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise OAuthError(
                "invalid_redirect_uri",
                f"redirect_uri http só é permitido em loopback: {uri}",
            )

    # ------------------------------------------------------------------
    # Authorization endpoint
    # ------------------------------------------------------------------

    def begin_authorization(self, params: Mapping[str, list[str] | str]) -> AuthorizationRequest:
        """Valida ``GET /oauth/authorize`` e cria o pedido pendente.

        Erros que não podem ser redirecionados com segurança (client ou redirect
        inválidos) sobem como ``OAuthError``; os demais são convertidos em
        redirect de erro por ``error_redirect``.
        """
        if not self.enabled:
            raise OAuthError("invalid_request", "OAuth não está habilitado", status=404)
        get = lambda key: self._single(params, key)  # noqa: E731 - acesso local curto
        client_id = get("client_id")
        if not client_id:
            raise OAuthError("invalid_request", "client_id é obrigatório")
        client = self.find_client(client_id)
        if client is None:
            raise OAuthError("invalid_client", f"client_id desconhecido: {client_id}")
        if not client.allows_grant(GRANT_AUTHORIZATION_CODE):
            raise OAuthError(
                "unauthorized_client", "client não permite authorization_code"
            )
        redirect_uri = get("redirect_uri")
        if not redirect_uri:
            if len(client.redirect_uris) != 1:
                raise OAuthError("invalid_request", "redirect_uri é obrigatório")
            redirect_uri = client.redirect_uris[0]
        if not client.allows_redirect_uri(redirect_uri):
            raise OAuthError("invalid_request", f"redirect_uri não registrado: {redirect_uri}")

        state = get("state")
        response_type = get("response_type")
        if response_type != "code":
            raise OAuthRedirectError(
                redirect_uri, state, "unsupported_response_type", "apenas response_type=code"
            )
        code_challenge = get("code_challenge")
        method = get("code_challenge_method") or ("plain" if code_challenge else "")
        if self._config.require_pkce or code_challenge:
            if not code_challenge:
                raise OAuthRedirectError(
                    redirect_uri, state, "invalid_request", "code_challenge é obrigatório (PKCE)"
                )
            if method != "S256" and self._config.require_pkce:
                raise OAuthRedirectError(
                    redirect_uri, state, "invalid_request", "code_challenge_method deve ser S256"
                )
            if method not in ("S256", "plain"):
                raise OAuthRedirectError(
                    redirect_uri, state, "invalid_request", f"code_challenge_method inválido: {method}"
                )
        try:
            scope = self.resolve_scope(get("scope"), client)
        except OAuthError as exc:
            raise OAuthRedirectError(redirect_uri, state, exc.error, exc.description) from exc

        request = AuthorizationRequest(
            request_id=secrets.token_urlsafe(24),
            client_id=client.client_id,
            client_name=client.client_name or client.client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=method,
            resource=get("resource"),
            expires_at=time.time() + self._config.code_ttl,
        )
        with self._lock:
            self._purge_expired_locked()
            self._pending[request.request_id] = request
        return request

    def pending_request(self, request_id: str) -> AuthorizationRequest:
        """Recupera um pedido pendente válido pelo ``request_id``."""
        with self._lock:
            self._purge_expired_locked()
            request = self._pending.get(request_id)
        if request is None or request.is_expired():
            raise OAuthError("invalid_request", "Pedido de autorização expirado ou inválido")
        return request

    def check_passcode(self, provided: str) -> bool:
        """Compara o passcode de consentimento em tempo constante."""
        expected = self._config.passcode
        if not expected:
            return True
        return hmac.compare_digest(expected, str(provided or ""))

    def approve_authorization(self, request: AuthorizationRequest) -> str:
        """Consome o pedido pendente, emite o código e retorna a URL de redirect."""
        with self._lock:
            stored = self._pending.pop(request.request_id, None)
            if stored is None or stored.is_expired():
                raise OAuthError("invalid_request", "Pedido de autorização expirado ou inválido")
            code = AuthorizationCode(
                code=secrets.token_urlsafe(32),
                client_id=stored.client_id,
                redirect_uri=stored.redirect_uri,
                scope=stored.scope,
                code_challenge=stored.code_challenge,
                code_challenge_method=stored.code_challenge_method,
                resource=stored.resource,
                expires_at=time.time() + self._config.code_ttl,
            )
            self._codes[code.code] = code
        _logger.info(
            "MCP OAuth: autorização concedida client=%s scope=%r", stored.client_id, stored.scope
        )
        return self._redirect_with(stored.redirect_uri, {"code": code.code, "state": stored.state})

    def deny_authorization(self, request: AuthorizationRequest) -> str:
        """Descarta o pedido pendente e retorna o redirect de ``access_denied``."""
        with self._lock:
            self._pending.pop(request.request_id, None)
        return self.error_redirect(
            request.redirect_uri, request.state, "access_denied", "Autorização negada pelo usuário"
        )

    def error_redirect(self, redirect_uri: str, state: str, error: str, description: str = "") -> str:
        """Monta o redirect de erro do endpoint de autorização."""
        params: dict[str, str] = {"error": error}
        if description:
            params["error_description"] = description
        if state:
            params["state"] = state
        return self._redirect_with(redirect_uri, params)

    @staticmethod
    def _redirect_with(redirect_uri: str, params: Mapping[str, str]) -> str:
        """Anexa *params* à query do ``redirect_uri`` preservando a query original."""
        parsed = urlparse(redirect_uri)
        clean = {key: value for key, value in params.items() if value}
        query = "&".join(filter(None, [parsed.query, urlencode(clean)]))
        return urlunparse(parsed._replace(query=query))

    def render_consent_page(
        self,
        request: AuthorizationRequest,
        *,
        error: str = "",
    ) -> str:
        """Renderiza a tela de consentimento em HTML autocontido."""
        scope_items = "".join(
            f"<li><code>{html.escape(item)}</code>{html.escape(self._scope_hint(item))}</li>"
            for item in (request.scope or SCOPE_DEFAULT).split()
        )
        passcode_field = ""
        if self._config.passcode:
            passcode_field = (
                '<label for="passcode">Código de acesso</label>'
                '<input id="passcode" name="passcode" type="password" autocomplete="one-time-code" '
                'required autofocus>'
            )
        error_block = (
            f'<p class="error">{html.escape(error)}</p>' if error else ""
        )
        return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Autorizar acesso ao Quimera MCP</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0; display: grid; place-items: center;
        min-height: 100vh; background: #101014; color: #e8e8ea; }}
main {{ width: min(30rem, 92vw); background: #1a1a20; border: 1px solid #2c2c34;
        border-radius: 12px; padding: 1.75rem; }}
h1 {{ font-size: 1.2rem; margin: 0 0 .35rem; }}
p.sub {{ color: #a0a0aa; margin: 0 0 1.25rem; }}
ul {{ margin: 0 0 1.25rem; padding-left: 1.2rem; }}
code {{ background: #26262e; padding: .1rem .35rem; border-radius: 4px; }}
label {{ display: block; font-size: .85rem; color: #a0a0aa; margin-bottom: .35rem; }}
input {{ width: 100%; box-sizing: border-box; padding: .6rem .7rem; margin-bottom: 1.1rem;
         border-radius: 8px; border: 1px solid #2c2c34; background: #101014; color: inherit; }}
.actions {{ display: flex; gap: .6rem; }}
button {{ flex: 1; padding: .65rem 1rem; border-radius: 8px; border: 0; font-weight: 600;
          cursor: pointer; font-size: .95rem; }}
button.allow {{ background: #4f7cff; color: #fff; }}
button.deny {{ background: #26262e; color: #e8e8ea; }}
p.error {{ background: #3a1d22; border: 1px solid #6b2a34; color: #ffb4b4;
           padding: .6rem .75rem; border-radius: 8px; margin: 0 0 1rem; }}
</style>
</head>
<body>
<main>
<h1>Autorizar acesso ao Quimera MCP</h1>
<p class="sub"><strong>{html.escape(request.client_name)}</strong> quer acessar as ferramentas
deste workspace.</p>
{error_block}
<ul>{scope_items}</ul>
<form method="post" action="{html.escape(self.AUTHORIZE_PATH)}">
<input type="hidden" name="request_id" value="{html.escape(request.request_id)}">
{passcode_field}
<div class="actions">
<button class="allow" type="submit" name="decision" value="allow">Autorizar</button>
<button class="deny" type="submit" name="decision" value="deny">Negar</button>
</div>
</form>
</main>
</body>
</html>
"""

    @staticmethod
    def _scope_hint(scope: str) -> str:
        """Descrição legível de um escopo para a tela de consentimento."""
        hints = {
            "mcp": " — perfil de ferramentas configurado no servidor",
            "mcp:read-local": " — leitura local, sem acesso à rede",
            "mcp:read": " — leitura local e busca/fetch na web",
            "mcp:agent": " — leitura, escrita assistida, git e delegação",
            "mcp:all": " — todas as ferramentas registradas",
        }
        return hints.get(scope, "")

    # ------------------------------------------------------------------
    # Token endpoint
    # ------------------------------------------------------------------

    def issue_token(
        self,
        form: Mapping[str, list[str] | str],
        *,
        basic_auth: tuple[str, str] | None = None,
    ) -> dict:
        """Processa ``POST /oauth/token`` para todos os grants suportados.

        Args:
            form: Corpo ``application/x-www-form-urlencoded`` já parseado.
            basic_auth: Par ``(client_id, client_secret)`` de ``Authorization: Basic``.

        Returns:
            Resposta de token conforme RFC 6749 §5.1.

        Raises:
            OAuthError: Para qualquer condição de erro OAuth.
        """
        if not self.enabled:
            raise OAuthError("invalid_request", "OAuth não está habilitado", status=404)
        grant_type = self._single(form, "grant_type")
        client = self._authenticate_client(form, basic_auth)
        if grant_type == GRANT_AUTHORIZATION_CODE:
            return self._grant_authorization_code(form, client)
        if grant_type == GRANT_REFRESH_TOKEN:
            return self._grant_refresh_token(form, client)
        if grant_type == GRANT_CLIENT_CREDENTIALS:
            return self._grant_client_credentials(form, client)
        raise OAuthError("unsupported_grant_type", f"grant_type não suportado: {grant_type!r}")

    def _authenticate_client(
        self,
        form: Mapping[str, list[str] | str],
        basic_auth: tuple[str, str] | None,
    ) -> OAuthClient:
        """Autentica o client por Basic, ``client_secret_post`` ou ``none``."""
        if basic_auth is not None:
            client_id, client_secret = basic_auth
        else:
            client_id = self._single(form, "client_id")
            client_secret = self._single(form, "client_secret")
        if not client_id:
            raise OAuthError("invalid_client", "client_id é obrigatório", status=401)
        client = self.find_client(client_id)
        if client is None:
            raise OAuthError("invalid_client", f"client_id desconhecido: {client_id}", status=401)
        if not client.check_secret(client_secret):
            raise OAuthError("invalid_client", "client_secret inválido", status=401)
        return client

    def _grant_authorization_code(
        self, form: Mapping[str, list[str] | str], client: OAuthClient
    ) -> dict:
        """Troca o código de autorização por tokens, validando PKCE e audience."""
        if not client.allows_grant(GRANT_AUTHORIZATION_CODE):
            raise OAuthError("unauthorized_client", "client não permite authorization_code")
        code_value = self._single(form, "code")
        if not code_value:
            raise OAuthError("invalid_request", "code é obrigatório")
        with self._lock:
            self._purge_expired_locked()
            code = self._codes.pop(code_value, None)
        if code is None or code.is_expired():
            raise OAuthError("invalid_grant", "code expirado ou já utilizado")
        if code.client_id != client.client_id:
            raise OAuthError("invalid_grant", "code emitido para outro client")
        redirect_uri = self._single(form, "redirect_uri")
        if redirect_uri and redirect_uri != code.redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri divergente do usado na autorização")
        self._verify_pkce(code, self._single(form, "code_verifier"))
        resource = self._single(form, "resource") or code.resource
        return self._build_token_response(client, code.scope, resource, refresh=True)

    def _verify_pkce(self, code: AuthorizationCode, verifier: str) -> None:
        """Valida o ``code_verifier`` contra o ``code_challenge`` armazenado."""
        if not code.code_challenge:
            if self._config.require_pkce:
                raise OAuthError("invalid_grant", "PKCE ausente na autorização")
            return
        if not verifier:
            raise OAuthError("invalid_request", "code_verifier é obrigatório")
        if code.code_challenge_method == "S256":
            digest = hashlib.sha256(verifier.encode("ascii")).digest()
            computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        else:
            computed = verifier
        if not hmac.compare_digest(computed, code.code_challenge):
            raise OAuthError("invalid_grant", "code_verifier não corresponde ao code_challenge")

    def _grant_refresh_token(
        self, form: Mapping[str, list[str] | str], client: OAuthClient
    ) -> dict:
        """Rotaciona um refresh token válido, emitindo novo par de tokens."""
        if not client.allows_grant(GRANT_REFRESH_TOKEN):
            raise OAuthError("unauthorized_client", "client não permite refresh_token")
        provided = self._single(form, "refresh_token")
        if not provided:
            raise OAuthError("invalid_request", "refresh_token é obrigatório")
        with self._lock:
            self._purge_expired_locked()
            stored = self._refresh_tokens.pop(provided, None)
            if stored is not None:
                self._persist_locked()
        if stored is None or stored.is_expired():
            raise OAuthError("invalid_grant", "refresh_token expirado ou revogado")
        if stored.client_id != client.client_id:
            raise OAuthError("invalid_grant", "refresh_token emitido para outro client")
        requested = self._single(form, "scope")
        scope = stored.scope
        if requested:
            requested_scope = normalize_scope(requested)
            if not set(requested_scope.split()) <= set(stored.scope.split()):
                raise OAuthError("invalid_scope", "refresh não pode ampliar o escopo original")
            scope = requested_scope
        resource = self._single(form, "resource") or stored.resource
        return self._build_token_response(client, scope, resource, refresh=True)

    def _grant_client_credentials(
        self, form: Mapping[str, list[str] | str], client: OAuthClient
    ) -> dict:
        """Emite access token direto para clients confidenciais (máquina-a-máquina)."""
        if not client.allows_grant(GRANT_CLIENT_CREDENTIALS):
            raise OAuthError("unauthorized_client", "client não permite client_credentials")
        if client.is_public:
            raise OAuthError("invalid_client", "client_credentials exige client_secret", status=401)
        scope = self.resolve_scope(self._single(form, "scope"), client)
        resource = self._single(form, "resource")
        return self._build_token_response(client, scope, resource, refresh=False)

    def _build_token_response(
        self,
        client: OAuthClient,
        scope: str,
        resource: str,
        *,
        refresh: bool,
    ) -> dict:
        """Emite os tokens e monta a resposta JSON do endpoint de token."""
        now = time.time()
        access = IssuedToken(
            token=secrets.token_urlsafe(32),
            client_id=client.client_id,
            scope=scope or SCOPE_DEFAULT,
            resource=resource,
            expires_at=now + self._config.access_token_ttl,
            kind="access",
        )
        response: dict = {
            "access_token": access.token,
            "token_type": "Bearer",
            "expires_in": self._config.access_token_ttl,
            "scope": access.scope,
        }
        refresh_token = None
        if refresh and client.allows_grant(GRANT_REFRESH_TOKEN):
            refresh_token = IssuedToken(
                token=secrets.token_urlsafe(32),
                client_id=client.client_id,
                scope=access.scope,
                resource=resource,
                expires_at=now + self._config.refresh_token_ttl,
                kind="refresh",
            )
            response["refresh_token"] = refresh_token.token
        with self._lock:
            self._access_tokens[access.token] = access
            if refresh_token is not None:
                self._refresh_tokens[refresh_token.token] = refresh_token
            self._purge_expired_locked()
            self._persist_locked()
        return response

    # ------------------------------------------------------------------
    # Revogação e introspecção
    # ------------------------------------------------------------------

    def revoke(
        self,
        form: Mapping[str, list[str] | str],
        *,
        basic_auth: tuple[str, str] | None = None,
    ) -> None:
        """Revoga um access ou refresh token (RFC 7009; idempotente)."""
        if not self.enabled:
            raise OAuthError("invalid_request", "OAuth não está habilitado", status=404)
        client = self._authenticate_client(form, basic_auth)
        token = self._single(form, "token")
        if not token:
            return
        with self._lock:
            changed = False
            access = self._access_tokens.get(token)
            if access is not None and access.client_id == client.client_id:
                self._access_tokens.pop(token, None)
                changed = True
            refresh = self._refresh_tokens.get(token)
            if refresh is not None and refresh.client_id == client.client_id:
                self._refresh_tokens.pop(token, None)
                changed = True
            if changed:
                self._persist_locked()

    def introspect(
        self,
        form: Mapping[str, list[str] | str],
        *,
        basic_auth: tuple[str, str] | None = None,
    ) -> dict:
        """Introspecção de token (RFC 7662) restrita ao client autenticado."""
        if not self.enabled:
            raise OAuthError("invalid_request", "OAuth não está habilitado", status=404)
        client = self._authenticate_client(form, basic_auth)
        token = self._single(form, "token")
        with self._lock:
            found = self._access_tokens.get(token) or self._refresh_tokens.get(token)
        if found is None or found.is_expired() or found.client_id != client.client_id:
            return {"active": False}
        payload = {
            "active": True,
            "client_id": found.client_id,
            "scope": found.scope,
            "token_type": "Bearer",
            "exp": int(found.expires_at),
        }
        if found.resource:
            payload["aud"] = found.resource
        return payload

    # ------------------------------------------------------------------
    # Validação de acesso
    # ------------------------------------------------------------------

    def find_client(self, client_id: str) -> OAuthClient | None:
        """Retorna o client registrado com *client_id*, se existir."""
        with self._lock:
            return self._clients.get(client_id)

    def validate_access_token(self, token: str) -> IssuedToken | None:
        """Retorna o access token válido correspondente, ou ``None``."""
        if not token:
            return None
        with self._lock:
            found = self._access_tokens.get(token)
            if found is None:
                return None
            if found.is_expired():
                self._access_tokens.pop(token, None)
                return None
            return found

    def authenticate_bearer(self, token: str) -> AuthContext:
        """Autentica um Bearer token OAuth e devolve o contexto resultante."""
        found = self.validate_access_token(token)
        if found is None:
            return AuthContext(
                authenticated=False,
                mode="oauth",
                error="invalid_token",
                error_description="Access token inválido ou expirado",
                status=401,
            )
        return AuthContext(
            authenticated=True,
            mode="oauth",
            client_id=found.client_id,
            scope=found.scope,
            tool_profile=self.profile_for_scope(found.scope),
        )

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    @staticmethod
    def _single(params: Mapping[str, list[str] | str], key: str) -> str:
        """Extrai um valor escalar de um mapa de query/form possivelmente multivalorado."""
        value = params.get(key)
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return str(value[0]).strip() if value else ""
        return str(value).strip()

    def _purge_expired_locked(self) -> None:
        """Remove códigos, pedidos e tokens expirados. Requer ``self._lock``."""
        now = time.time()
        for store in (self._pending, self._codes, self._access_tokens, self._refresh_tokens):
            expired = [key for key, item in store.items() if item.is_expired(now)]
            for key in expired:
                store.pop(key, None)

    def _persist_locked(self) -> None:
        """Persiste o estado durável. Requer ``self._lock``."""
        self._store.save(
            self._clients,
            self._refresh_tokens,
            access_tokens=self._access_tokens,
        )


def build_provider_from_cli(
    *,
    enabled: bool = False,
    issuer: str | None = None,
    client_specs: Iterable[str] | None = None,
    redirect_uris: Iterable[str] | None = None,
    passcode_env: str | None = None,
    auto_approve: bool | None = None,
    allow_dynamic_registration: bool | None = None,
    store_path: Path | str | None = None,
) -> OAuthProvider:
    """Constrói o provider a partir de flags de CLI, com fallback no ambiente.

    Args:
        enabled: Liga o OAuth (``--mcp-oauth``). Se ``False``, o ambiente ainda
            pode habilitá-lo via ``QUIMERA_MCP_OAUTH=1``.
        issuer: URL pública do issuer (necessária atrás de túnel/proxy).
        client_specs: Clients estáticos ``id[:secret]``.
        redirect_uris: Redirects permitidos aos clients estáticos.
        passcode_env: Variável de ambiente com o passcode de consentimento.
        auto_approve: Dispensa a tela de consentimento.
        allow_dynamic_registration: Habilita/desabilita RFC 7591.
        store_path: Arquivo JSON de persistência.

    Returns:
        ``OAuthProvider`` pronto para uso pelo ``MCP_HTTPServer``.
    """
    overrides: dict = {}
    if enabled:
        overrides["enabled"] = True
    if issuer:
        overrides["issuer"] = issuer.rstrip("/")
    static_clients = parse_client_specs(client_specs, redirect_uris=redirect_uris or ())
    if static_clients:
        overrides["clients"] = static_clients
    if passcode_env:
        passcode = (os.environ.get(passcode_env) or "").strip()
        if passcode:
            overrides["passcode"] = passcode
    if auto_approve is not None:
        overrides["auto_approve"] = auto_approve
    if allow_dynamic_registration is not None:
        overrides["allow_dynamic_registration"] = allow_dynamic_registration
    if store_path:
        overrides["store_path"] = Path(store_path).expanduser()
    return OAuthProvider(OAuthConfig.from_env(**overrides))

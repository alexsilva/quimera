"""Testes do Authorization Server OAuth 2.1 embutido no MCP HTTP do Quimera.

Cobre o fluxo completo ponta a ponta contra um servidor HTTP real (sem mocks de
rede): descoberta de metadados, registro dinâmico, consentimento, PKCE, troca de
código, refresh, client_credentials, revogação, introspecção, restrição de
escopo e coexistência com o token estático de header.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import time
from http.client import HTTPConnection
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from quimera.runtime.mcp.http_server import HTTP_READ_LOCAL_TOOLS, MCP_HTTPServer
from quimera.runtime.mcp.oauth import (
    GRANT_CLIENT_CREDENTIALS,
    OAuthConfig,
    OAuthError,
    OAuthProvider,
    OAuthRedirectError,
    normalize_scope,
    parse_client_specs,
)
from quimera.runtime.mcp.server import MCPServer
from quimera.runtime.models import ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(tool_names=None):
    """Cria um executor mínimo com registry previsível."""
    executor = MagicMock()
    executor.registry.names.return_value = tool_names or sorted(HTTP_READ_LOCAL_TOOLS)
    executor.config.db_path = None
    executor.policy.blocked_tools = set()
    executor.execute.return_value = ToolResult(ok=True, tool_name="read_file", content="ok")
    return executor


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    """Aguarda o bind do servidor HTTP ficar aceitando conexões."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)


class _Response:
    """Resposta HTTP simplificada para asserções nos testes."""

    def __init__(self, status: int, headers: dict, data: bytes) -> None:
        """Guarda status, headers e corpo bruto."""
        self.status = status
        self.headers = headers
        self.data = data

    def header(self, name: str) -> str | None:
        """Retorna o header *name* ignorando caixa."""
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None

    def json(self) -> dict:
        """Decodifica o corpo como JSON."""
        return json.loads(self.data.decode("utf-8"))

    @property
    def text(self) -> str:
        """Corpo decodificado como texto."""
        return self.data.decode("utf-8")


def _request(
    httpd: MCP_HTTPServer,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict | None = None,
) -> _Response:
    """Executa uma requisição HTTP contra o servidor de teste."""
    conn = HTTPConnection(httpd.host, httpd.port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return _Response(resp.status, dict(resp.getheaders()), resp.read())
    finally:
        conn.close()


def _post_form(httpd: MCP_HTTPServer, path: str, form: dict, headers: dict | None = None) -> _Response:
    """POST ``application/x-www-form-urlencoded``."""
    payload = urlencode(form).encode("utf-8")
    merged = {"Content-Type": "application/x-www-form-urlencoded"}
    merged.update(headers or {})
    return _request(httpd, "POST", path, body=payload, headers=merged)


def _post_json(httpd: MCP_HTTPServer, path: str, payload: dict) -> _Response:
    """POST ``application/json``."""
    body = json.dumps(payload).encode("utf-8")
    return _request(httpd, "POST", path, body=body, headers={"Content-Type": "application/json"})


def _pkce_pair() -> tuple[str, str]:
    """Gera um par ``(code_verifier, code_challenge)`` no método S256."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _start_server(
    oauth: OAuthConfig | OAuthProvider | None = None,
    *,
    auth_token: str | None = None,
    allowed_tools=HTTP_READ_LOCAL_TOOLS,
    executor=None,
) -> MCP_HTTPServer:
    """Inicia um ``MCP_HTTPServer`` real em porta efêmera."""
    mcp = MCPServer(executor or _make_executor(), auth_token=auth_token)
    httpd = MCP_HTTPServer(
        mcp,
        host="127.0.0.1",
        port=0,
        allowed_tools=allowed_tools,
        cors_origins="*",
        oauth=oauth,
    )
    httpd.start_background()
    _wait_for_server(httpd.host, httpd.port)
    return httpd


@pytest.fixture
def oauth_server(tmp_path):
    """Servidor com OAuth habilitado, registro dinâmico e sem passcode."""
    provider = OAuthProvider(
        OAuthConfig(enabled=True, store_path=tmp_path / "mcp_oauth.json")
    )
    httpd = _start_server(provider)
    yield httpd
    httpd.shutdown()


def _register_client(httpd: MCP_HTTPServer, redirect_uri: str = "http://127.0.0.1:5599/cb") -> dict:
    """Registra um client público via RFC 7591 e retorna a resposta."""
    resp = _post_json(
        httpd,
        "/oauth/register",
        {"client_name": "cliente-teste", "redirect_uris": [redirect_uri]},
    )
    assert resp.status == 201, resp.text
    return resp.json()


def _authorize_and_get_code(
    httpd: MCP_HTTPServer,
    client_id: str,
    challenge: str,
    *,
    redirect_uri: str = "http://127.0.0.1:5599/cb",
    scope: str = "",
    state: str = "estado-123",
    passcode: str | None = None,
) -> str:
    """Percorre consentimento e retorna o ``code`` do redirect."""
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if scope:
        query["scope"] = scope
    page = _request(httpd, "GET", f"/oauth/authorize?{urlencode(query)}")
    assert page.status == 200, page.text
    request_id = page.text.split('name="request_id" value="')[1].split('"')[0]
    form = {"request_id": request_id, "decision": "allow"}
    if passcode is not None:
        form["passcode"] = passcode
    decision = _post_form(httpd, "/oauth/authorize", form)
    assert decision.status == 302, decision.text
    location = decision.header("Location")
    assert location is not None
    params = parse_qs(urlparse(location).query)
    assert params.get("state") == [state]
    return params["code"][0]


# ---------------------------------------------------------------------------
# Descoberta de metadados
# ---------------------------------------------------------------------------

class TestMetadataDiscovery:
    def test_protected_resource_anuncia_authorization_server(self, oauth_server):
        """RFC 9728 deve apontar o AS embutido quando OAuth está habilitado."""
        resp = _request(oauth_server, "GET", "/.well-known/oauth-protected-resource/mcp")

        assert resp.status == 200
        metadata = resp.json()
        base = f"http://{oauth_server.host}:{oauth_server.port}"
        assert metadata["resource"] == f"{base}/mcp"
        assert metadata["authorization_servers"] == [base]
        assert metadata["bearer_methods_supported"] == ["header"]

    def test_protected_resource_sem_sufixo_tambem_responde(self, oauth_server):
        """Clientes que consultam a raiz do metadata devem ser atendidos."""
        resp = _request(oauth_server, "GET", "/.well-known/oauth-protected-resource")

        assert resp.status == 200
        assert "authorization_servers" in resp.json()

    def test_authorization_server_metadata_publica_endpoints(self, oauth_server):
        """RFC 8414 deve listar authorize, token, register e PKCE S256."""
        resp = _request(oauth_server, "GET", "/.well-known/oauth-authorization-server")

        metadata = resp.json()
        base = f"http://{oauth_server.host}:{oauth_server.port}"
        assert metadata["issuer"] == base
        assert metadata["authorization_endpoint"] == f"{base}/oauth/authorize"
        assert metadata["token_endpoint"] == f"{base}/oauth/token"
        assert metadata["registration_endpoint"] == f"{base}/oauth/register"
        assert metadata["revocation_endpoint"] == f"{base}/oauth/revoke"
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert "authorization_code" in metadata["grant_types_supported"]

    def test_metadata_respeita_forwarded_headers_de_tunel(self, oauth_server):
        """Atrás de túnel, o issuer deve ser a URL pública encaminhada."""
        resp = _request(
            oauth_server,
            "GET",
            "/.well-known/oauth-authorization-server",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "quimera.exemplo.dev",
            },
        )

        metadata = resp.json()
        assert metadata["issuer"] == "https://quimera.exemplo.dev"
        assert metadata["token_endpoint"] == "https://quimera.exemplo.dev/oauth/token"

    def test_issuer_explicito_prevalece_sobre_requisicao(self, tmp_path):
        """``--mcp-oauth-issuer`` deve sobrepor o Host da requisição."""
        provider = OAuthProvider(
            OAuthConfig(
                enabled=True,
                issuer="https://fixo.exemplo.dev",
                store_path=tmp_path / "s.json",
            )
        )
        httpd = _start_server(provider)
        try:
            metadata = _request(
                httpd, "GET", "/.well-known/oauth-authorization-server"
            ).json()
        finally:
            httpd.shutdown()

        assert metadata["issuer"] == "https://fixo.exemplo.dev"

    def test_metadata_legado_quando_oauth_desabilitado(self):
        """Sem OAuth, o AS metadata não deve anunciar endpoints de fluxo."""
        httpd = _start_server(None)
        try:
            as_metadata = _request(
                httpd, "GET", "/.well-known/oauth-authorization-server"
            ).json()
            pr_metadata = _request(
                httpd, "GET", "/.well-known/oauth-protected-resource/mcp"
            ).json()
        finally:
            httpd.shutdown()

        assert "authorization_endpoint" not in as_metadata
        assert "token_endpoint" not in as_metadata
        assert "authorization_servers" not in pr_metadata
        assert pr_metadata["bearer_methods_supported"] == ["header"]


# ---------------------------------------------------------------------------
# Registro dinâmico (RFC 7591)
# ---------------------------------------------------------------------------

class TestDynamicRegistration:
    def test_registro_dinamico_cria_client_publico(self, oauth_server):
        """Client sem auth method recebe client_id sem secret."""
        payload = _register_client(oauth_server)

        assert payload["client_id"].startswith("quimera-")
        assert "client_secret" not in payload
        assert payload["token_endpoint_auth_method"] == "none"
        assert payload["redirect_uris"] == ["http://127.0.0.1:5599/cb"]

    def test_registro_confidencial_recebe_secret(self, oauth_server):
        """``client_secret_post`` deve gerar secret de client confidencial."""
        resp = _post_json(
            oauth_server,
            "/oauth/register",
            {
                "client_name": "servico",
                "redirect_uris": ["https://app.exemplo.dev/cb"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        )

        payload = resp.json()
        assert resp.status == 201
        assert payload["client_secret"]
        assert payload["client_secret_expires_at"] == 0

    def test_registro_rejeita_redirect_http_externo(self, oauth_server):
        """``http`` só é aceito em loopback (RFC 8252)."""
        resp = _post_json(
            oauth_server,
            "/oauth/register",
            {"client_name": "ruim", "redirect_uris": ["http://exemplo.dev/cb"]},
        )

        assert resp.status == 400
        assert resp.json()["error"] == "invalid_redirect_uri"

    def test_registro_rejeita_authorization_code_sem_redirect(self, oauth_server):
        """``authorization_code`` exige ``redirect_uris``."""
        resp = _post_json(oauth_server, "/oauth/register", {"client_name": "sem-redirect"})

        assert resp.status == 400
        assert resp.json()["error"] == "invalid_redirect_uri"

    def test_registro_desabilitado_retorna_403(self, tmp_path):
        """``--mcp-oauth-no-register`` deve bloquear RFC 7591."""
        provider = OAuthProvider(
            OAuthConfig(
                enabled=True,
                allow_dynamic_registration=False,
                store_path=tmp_path / "s.json",
            )
        )
        httpd = _start_server(provider)
        try:
            resp = _post_json(
                httpd,
                "/oauth/register",
                {"client_name": "x", "redirect_uris": ["http://127.0.0.1:1/cb"]},
            )
        finally:
            httpd.shutdown()

        assert resp.status == 403
        assert resp.json()["error"] == "invalid_client_metadata"

    def test_clients_dinamicos_sobrevivem_a_restart(self, tmp_path):
        """Clients registrados devem ser recarregados do store no restart."""
        store = tmp_path / "mcp_oauth.json"
        first = OAuthProvider(OAuthConfig(enabled=True, store_path=store))
        client = first.register_client(
            {"client_name": "persistente", "redirect_uris": ["http://127.0.0.1:5599/cb"]}
        )

        second = OAuthProvider(OAuthConfig(enabled=True, store_path=store))

        restored = second.find_client(client.client_id)
        assert restored is not None
        assert restored.client_name == "persistente"
        assert restored.redirect_uris == ("http://127.0.0.1:5599/cb",)


# ---------------------------------------------------------------------------
# Fluxo authorization_code + PKCE
# ---------------------------------------------------------------------------

class TestAuthorizationCodeFlow:
    def test_fluxo_completo_emite_access_e_refresh(self, oauth_server):
        """Consentimento + PKCE devem render access_token e refresh_token."""
        client = _register_client(oauth_server)
        verifier, challenge = _pkce_pair()

        code = _authorize_and_get_code(oauth_server, client["client_id"], challenge)
        resp = _post_form(
            oauth_server,
            "/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client["client_id"],
                "code_verifier": verifier,
                "redirect_uri": "http://127.0.0.1:5599/cb",
            },
        )

        payload = resp.json()
        assert resp.status == 200, resp.text
        assert payload["token_type"] == "Bearer"
        assert payload["access_token"]
        assert payload["refresh_token"]
        assert payload["expires_in"] == 3600
        assert payload["scope"] == "mcp"

    def test_tela_de_consentimento_exibe_client_e_escopo(self, oauth_server):
        """A tela deve identificar o client e os escopos pedidos."""
        client = _register_client(oauth_server)
        _, challenge = _pkce_pair()

        page = _request(
            oauth_server,
            "GET",
            "/oauth/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client["client_id"],
                    "redirect_uri": "http://127.0.0.1:5599/cb",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "scope": "mcp:read-local",
                }
            ),
        )

        assert page.status == 200
        assert "cliente-teste" in page.text
        assert "mcp:read-local" in page.text
        assert 'value="allow"' in page.text

    def test_negar_consentimento_redireciona_access_denied(self, oauth_server):
        """Negar deve redirecionar com ``error=access_denied``."""
        client = _register_client(oauth_server)
        _, challenge = _pkce_pair()
        page = _request(
            oauth_server,
            "GET",
            "/oauth/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client["client_id"],
                    "redirect_uri": "http://127.0.0.1:5599/cb",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "s1",
                }
            ),
        )
        request_id = page.text.split('name="request_id" value="')[1].split('"')[0]

        resp = _post_form(
            oauth_server,
            "/oauth/authorize",
            {"request_id": request_id, "decision": "deny"},
        )

        params = parse_qs(urlparse(resp.header("Location")).query)
        assert resp.status == 302
        assert params["error"] == ["access_denied"]
        assert params["state"] == ["s1"]

    def test_code_verifier_invalido_e_rejeitado(self, oauth_server):
        """PKCE divergente deve falhar com ``invalid_grant``."""
        client = _register_client(oauth_server)
        _, challenge = _pkce_pair()
        code = _authorize_and_get_code(oauth_server, client["client_id"], challenge)

        resp = _post_form(
            oauth_server,
            "/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client["client_id"],
                "code_verifier": "verificador-errado-mas-longo-o-suficiente",
            },
        )

        assert resp.status == 400
        assert resp.json()["error"] == "invalid_grant"

    def test_code_e_de_uso_unico(self, oauth_server):
        """Reuso de código deve ser rejeitado."""
        client = _register_client(oauth_server)
        verifier, challenge = _pkce_pair()
        code = _authorize_and_get_code(oauth_server, client["client_id"], challenge)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client["client_id"],
            "code_verifier": verifier,
        }
        assert _post_form(oauth_server, "/oauth/token", form).status == 200

        resp = _post_form(oauth_server, "/oauth/token", form)

        assert resp.status == 400
        assert resp.json()["error"] == "invalid_grant"

    def test_authorize_sem_pkce_redireciona_erro(self, oauth_server):
        """PKCE é obrigatório: authorize sem ``code_challenge`` deve falhar."""
        client = _register_client(oauth_server)

        resp = _request(
            oauth_server,
            "GET",
            "/oauth/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client["client_id"],
                    "redirect_uri": "http://127.0.0.1:5599/cb",
                    "state": "s2",
                }
            ),
        )

        params = parse_qs(urlparse(resp.header("Location")).query)
        assert resp.status == 302
        assert params["error"] == ["invalid_request"]

    def test_redirect_uri_nao_registrado_e_bloqueado(self, oauth_server):
        """Redirect fora da allowlist não pode receber redirect de erro."""
        client = _register_client(oauth_server)
        _, challenge = _pkce_pair()

        resp = _request(
            oauth_server,
            "GET",
            "/oauth/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client["client_id"],
                    "redirect_uri": "https://atacante.exemplo/cb",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            ),
        )

        assert resp.status == 400
        assert resp.json()["error"] == "invalid_request"

    def test_client_desconhecido_retorna_invalid_client(self, oauth_server):
        """``client_id`` inexistente não deve gerar redirect."""
        resp = _request(
            oauth_server,
            "GET",
            "/oauth/authorize?" + urlencode({"response_type": "code", "client_id": "fantasma"}),
        )

        assert resp.status == 400
        assert resp.json()["error"] == "invalid_client"

    def test_redirect_loopback_aceita_porta_efemera(self, oauth_server):
        """RFC 8252: porta do redirect loopback pode variar."""
        client = _register_client(oauth_server, redirect_uri="http://127.0.0.1:5599/cb")
        verifier, challenge = _pkce_pair()

        code = _authorize_and_get_code(
            oauth_server,
            client["client_id"],
            challenge,
            redirect_uri="http://127.0.0.1:41234/cb",
        )
        resp = _post_form(
            oauth_server,
            "/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client["client_id"],
                "code_verifier": verifier,
                "redirect_uri": "http://127.0.0.1:41234/cb",
            },
        )

        assert resp.status == 200, resp.text


# ---------------------------------------------------------------------------
# Refresh, client_credentials, revogação e introspecção
# ---------------------------------------------------------------------------

class TestOtherGrants:
    def _issue_tokens(self, httpd: MCP_HTTPServer) -> tuple[dict, dict]:
        """Executa o fluxo de código e retorna ``(client, tokens)``."""
        client = _register_client(httpd)
        verifier, challenge = _pkce_pair()
        code = _authorize_and_get_code(httpd, client["client_id"], challenge)
        tokens = _post_form(
            httpd,
            "/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client["client_id"],
                "code_verifier": verifier,
            },
        ).json()
        return client, tokens

    def test_refresh_rotaciona_tokens(self, oauth_server):
        """Refresh deve emitir novo par e invalidar o refresh anterior."""
        client, tokens = self._issue_tokens(oauth_server)

        renewed = _post_form(
            oauth_server,
            "/oauth/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
            },
        ).json()
        reused = _post_form(
            oauth_server,
            "/oauth/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
            },
        )

        assert renewed["access_token"] != tokens["access_token"]
        assert renewed["refresh_token"] != tokens["refresh_token"]
        assert reused.status == 400
        assert reused.json()["error"] == "invalid_grant"

    def test_client_credentials_para_client_confidencial(self, tmp_path):
        """Client estático com secret deve obter token sem browser."""
        provider = OAuthProvider(
            OAuthConfig(
                enabled=True,
                clients=parse_client_specs("robo:s3cr3t"),
                store_path=tmp_path / "s.json",
            )
        )
        httpd = _start_server(provider)
        try:
            resp = _post_form(
                httpd,
                "/oauth/token",
                {
                    "grant_type": "client_credentials",
                    "client_id": "robo",
                    "client_secret": "s3cr3t",
                    "scope": "mcp:read-local",
                },
            )
        finally:
            httpd.shutdown()

        payload = resp.json()
        assert resp.status == 200, resp.text
        assert payload["access_token"]
        assert payload["scope"] == "mcp:read-local"
        assert "refresh_token" not in payload

    def test_client_credentials_com_basic_auth(self, tmp_path):
        """``Authorization: Basic`` deve autenticar o client no token endpoint."""
        provider = OAuthProvider(
            OAuthConfig(
                enabled=True,
                clients=parse_client_specs("robo:s3cr3t"),
                store_path=tmp_path / "s.json",
            )
        )
        httpd = _start_server(provider)
        credentials = base64.b64encode(b"robo:s3cr3t").decode("ascii")
        try:
            resp = _post_form(
                httpd,
                "/oauth/token",
                {"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {credentials}"},
            )
        finally:
            httpd.shutdown()

        assert resp.status == 200, resp.text
        assert resp.json()["access_token"]

    def test_client_secret_invalido_retorna_401(self, tmp_path):
        """Secret errado deve resultar em ``invalid_client``."""
        provider = OAuthProvider(
            OAuthConfig(
                enabled=True,
                clients=parse_client_specs("robo:s3cr3t"),
                store_path=tmp_path / "s.json",
            )
        )
        httpd = _start_server(provider)
        try:
            resp = _post_form(
                httpd,
                "/oauth/token",
                {
                    "grant_type": "client_credentials",
                    "client_id": "robo",
                    "client_secret": "errado",
                },
            )
        finally:
            httpd.shutdown()

        assert resp.status == 401
        assert resp.json()["error"] == "invalid_client"

    def test_revoke_invalida_access_token(self, oauth_server):
        """Após revogação, o token não deve mais autenticar em /mcp."""
        client, tokens = self._issue_tokens(oauth_server)

        revoked = _post_form(
            oauth_server,
            "/oauth/revoke",
            {"token": tokens["access_token"], "client_id": client["client_id"]},
        )
        probe = _request(
            oauth_server,
            "POST",
            "/mcp",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {tokens['access_token']}",
                "MCP-Protocol-Version": MCPServer.PROTOCOL_VERSION,
            },
        )

        assert revoked.status == 200
        assert probe.status == 401

    def test_introspect_descreve_token_ativo(self, oauth_server):
        """Introspecção deve devolver ``active`` e escopo do token."""
        client, tokens = self._issue_tokens(oauth_server)

        resp = _post_form(
            oauth_server,
            "/oauth/introspect",
            {"token": tokens["access_token"], "client_id": client["client_id"]},
        )

        payload = resp.json()
        assert payload["active"] is True
        assert payload["client_id"] == client["client_id"]
        assert payload["scope"] == "mcp"

    def test_introspect_de_token_alheio_retorna_inativo(self, oauth_server):
        """Um client não pode inspecionar token de outro."""
        _, tokens = self._issue_tokens(oauth_server)
        outro = _register_client(oauth_server, redirect_uri="http://127.0.0.1:5600/cb")

        resp = _post_form(
            oauth_server,
            "/oauth/introspect",
            {"token": tokens["access_token"], "client_id": outro["client_id"]},
        )

        assert resp.json() == {"active": False}

    def test_grant_type_desconhecido_retorna_erro(self, oauth_server):
        """Grant não suportado deve responder ``unsupported_grant_type``."""
        client = _register_client(oauth_server)

        resp = _post_form(
            oauth_server,
            "/oauth/token",
            {"grant_type": "password", "client_id": client["client_id"]},
        )

        assert resp.status == 400
        assert resp.json()["error"] == "unsupported_grant_type"


# ---------------------------------------------------------------------------
# Passcode de consentimento e auto-approve
# ---------------------------------------------------------------------------

class TestConsentModes:
    def test_passcode_incorreto_bloqueia_autorizacao(self, tmp_path):
        """Passcode errado deve devolver 401 e reexibir a tela."""
        provider = OAuthProvider(
            OAuthConfig(enabled=True, passcode="abrete-sesamo", store_path=tmp_path / "s.json")
        )
        httpd = _start_server(provider)
        try:
            client = _register_client(httpd)
            _, challenge = _pkce_pair()
            page = _request(
                httpd,
                "GET",
                "/oauth/authorize?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": client["client_id"],
                        "redirect_uri": "http://127.0.0.1:5599/cb",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    }
                ),
            )
            assert 'id="passcode"' in page.text
            request_id = page.text.split('name="request_id" value="')[1].split('"')[0]

            resp = _post_form(
                httpd,
                "/oauth/authorize",
                {"request_id": request_id, "decision": "allow", "passcode": "errado"},
            )
        finally:
            httpd.shutdown()

        assert resp.status == 401
        assert "incorreto" in resp.text

    def test_passcode_correto_libera_codigo(self, tmp_path):
        """Passcode correto deve emitir o código de autorização."""
        provider = OAuthProvider(
            OAuthConfig(enabled=True, passcode="abrete-sesamo", store_path=tmp_path / "s.json")
        )
        httpd = _start_server(provider)
        try:
            client = _register_client(httpd)
            _, challenge = _pkce_pair()
            code = _authorize_and_get_code(
                httpd, client["client_id"], challenge, passcode="abrete-sesamo"
            )
        finally:
            httpd.shutdown()

        assert code

    def test_auto_approve_dispensa_tela(self, tmp_path):
        """``auto_approve`` deve redirecionar direto com o código."""
        provider = OAuthProvider(
            OAuthConfig(enabled=True, auto_approve=True, store_path=tmp_path / "s.json")
        )
        httpd = _start_server(provider)
        try:
            client = _register_client(httpd)
            _, challenge = _pkce_pair()
            resp = _request(
                httpd,
                "GET",
                "/oauth/authorize?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": client["client_id"],
                        "redirect_uri": "http://127.0.0.1:5599/cb",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    }
                ),
            )
        finally:
            httpd.shutdown()

        assert resp.status == 302
        assert "code=" in resp.header("Location")

    def test_auto_approve_com_passcode_ainda_pede_confirmacao(self, tmp_path):
        """Passcode configurado tem precedência sobre auto_approve."""
        provider = OAuthProvider(
            OAuthConfig(
                enabled=True,
                auto_approve=True,
                passcode="segredo",
                store_path=tmp_path / "s.json",
            )
        )
        httpd = _start_server(provider)
        try:
            client = _register_client(httpd)
            _, challenge = _pkce_pair()
            resp = _request(
                httpd,
                "GET",
                "/oauth/authorize?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": client["client_id"],
                        "redirect_uri": "http://127.0.0.1:5599/cb",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    }
                ),
            )
        finally:
            httpd.shutdown()

        assert resp.status == 200
        assert 'id="passcode"' in resp.text


# ---------------------------------------------------------------------------
# Acesso ao transporte MCP
# ---------------------------------------------------------------------------

class TestMCPAccess:
    def _mcp_call(self, httpd: MCP_HTTPServer, headers: dict, payload: dict | None = None) -> _Response:
        """Envia uma mensagem JSON-RPC para ``POST /mcp``."""
        body = json.dumps(payload or {"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        merged = {
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCPServer.PROTOCOL_VERSION,
        }
        merged.update(headers)
        return _request(httpd, "POST", "/mcp", body=body, headers=merged)

    def _initialize_session(self, httpd: MCP_HTTPServer, headers: dict) -> str:
        """Executa o handshake MCP e devolve o ``MCP-Session-Id`` da sessão."""
        init = self._mcp_call(
            httpd,
            headers,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCPServer.PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            },
        )
        assert init.status == 200, init.text
        session_id = init.header("MCP-Session-Id")
        assert session_id
        with_session = dict(headers)
        with_session["MCP-Session-Id"] = session_id
        self._mcp_call(
            httpd, with_session, {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        return session_id

    def test_access_token_autoriza_chamada_mcp(self, oauth_server):
        """Token OAuth válido deve ser aceito no transporte MCP."""
        client = _register_client(oauth_server)
        verifier, challenge = _pkce_pair()
        code = _authorize_and_get_code(oauth_server, client["client_id"], challenge)
        tokens = _post_form(
            oauth_server,
            "/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client["client_id"],
                "code_verifier": verifier,
            },
        ).json()

        resp = self._mcp_call(
            oauth_server, {"Authorization": f"Bearer {tokens['access_token']}"}
        )

        assert resp.status == 200
        assert resp.json()["result"] == {}

    def test_sem_token_retorna_401_com_www_authenticate(self, oauth_server):
        """401 deve apontar o metadata do recurso (RFC 9728) para descoberta."""
        resp = self._mcp_call(oauth_server, {})

        challenge = resp.header("WWW-Authenticate") or ""
        base = f"http://{oauth_server.host}:{oauth_server.port}"
        assert resp.status == 401
        assert challenge.startswith("Bearer ")
        assert f'resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"' in challenge

    def test_token_invalido_retorna_invalid_token(self, oauth_server):
        """Bearer desconhecido deve gerar ``error="invalid_token"``."""
        resp = self._mcp_call(oauth_server, {"Authorization": "Bearer token-falso"})

        assert resp.status == 401
        assert 'error="invalid_token"' in (resp.header("WWW-Authenticate") or "")

    def test_token_estatico_de_header_continua_valido_com_oauth(self, tmp_path):
        """Compatibilidade: o token fixo funciona junto com o OAuth habilitado."""
        provider = OAuthProvider(OAuthConfig(enabled=True, store_path=tmp_path / "s.json"))
        httpd = _start_server(provider, auth_token="token-fixo")
        try:
            via_bearer = self._mcp_call(httpd, {"Authorization": "Bearer token-fixo"})
            via_header = self._mcp_call(httpd, {"X-Quimera-MCP-Token": "token-fixo"})
            sem_token = self._mcp_call(httpd, {})
        finally:
            httpd.shutdown()

        assert via_bearer.status == 200
        assert via_header.status == 200
        assert sem_token.status == 401

    def test_escopo_restritivo_remove_tools_de_rede(self, tmp_path):
        """``mcp:read-local`` deve esconder as tools de rede do perfil ``read``."""
        provider = OAuthProvider(OAuthConfig(enabled=True, store_path=tmp_path / "s.json"))
        executor = _make_executor(["read_file", "web_search", "web_fetch"])
        httpd = _start_server(
            provider,
            allowed_tools={"read_file", "web_search", "web_fetch"},
            executor=executor,
        )
        try:
            client = _register_client(httpd)
            verifier, challenge = _pkce_pair()
            code = _authorize_and_get_code(
                httpd, client["client_id"], challenge, scope="mcp:read-local"
            )
            tokens = _post_form(
                httpd,
                "/oauth/token",
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client["client_id"],
                    "code_verifier": verifier,
                },
            ).json()
            auth_headers = {"Authorization": f"Bearer {tokens['access_token']}"}
            session_id = self._initialize_session(httpd, auth_headers)
            listed = self._mcp_call(
                httpd,
                {**auth_headers, "MCP-Session-Id": session_id},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
        finally:
            httpd.shutdown()

        assert tokens["scope"] == "mcp:read-local"
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        assert "read_file" in names
        assert "web_search" not in names
        assert "web_fetch" not in names

    def test_endpoints_oauth_retornam_404_quando_desabilitado(self):
        """Sem OAuth, authorize/token não devem operar."""
        httpd = _start_server(None)
        try:
            authorize = _request(httpd, "GET", "/oauth/authorize?client_id=x")
            token = _post_form(httpd, "/oauth/token", {"grant_type": "client_credentials"})
            register = _post_json(httpd, "/oauth/register", {"redirect_uris": []})
        finally:
            httpd.shutdown()

        assert authorize.status == 404
        assert token.status == 404
        assert register.status == 404


# ---------------------------------------------------------------------------
# Unidades do provider
# ---------------------------------------------------------------------------

class TestProviderUnit:
    def test_parse_client_specs_gera_publico_e_confidencial(self):
        """Spec sem secret é pública; com secret habilita client_credentials."""
        clients = parse_client_specs("publico,servico:abc", redirect_uris=["http://127.0.0.1:1/cb"])

        assert clients[0].is_public
        assert not clients[0].allows_grant(GRANT_CLIENT_CREDENTIALS)
        assert clients[1].client_secret == "abc"
        assert clients[1].allows_grant(GRANT_CLIENT_CREDENTIALS)
        assert clients[1].redirect_uris == ("http://127.0.0.1:1/cb",)

    def test_profile_for_scope_mapeia_perfis(self):
        """Escopos devem mapear para os perfis de tools do transporte."""
        assert OAuthProvider.profile_for_scope("mcp") == ""
        assert OAuthProvider.profile_for_scope("mcp:read-local") == "read-local"
        assert OAuthProvider.profile_for_scope("mcp:agent") == "agent"
        assert OAuthProvider.profile_for_scope("") == ""

    def test_normalize_scope_remove_duplicatas(self):
        """Normalização deve preservar ordem e eliminar repetições."""
        assert normalize_scope("mcp mcp:read mcp") == "mcp mcp:read"
        assert normalize_scope(["mcp:agent", "mcp:agent"]) == "mcp:agent"
        assert normalize_scope(None) == ""

    def test_escopo_desconhecido_e_rejeitado(self):
        """Escopo fora dos suportados deve virar ``invalid_scope``."""
        provider = OAuthProvider(OAuthConfig(enabled=True))
        client = provider.register_client(
            {"client_name": "c", "redirect_uris": ["http://127.0.0.1:1/cb"]}
        )

        with pytest.raises(OAuthRedirectError) as excinfo:
            provider.begin_authorization(
                {
                    "response_type": "code",
                    "client_id": client.client_id,
                    "redirect_uri": "http://127.0.0.1:1/cb",
                    "code_challenge": "abc",
                    "code_challenge_method": "S256",
                    "scope": "admin:root",
                }
            )

        assert excinfo.value.error == "invalid_scope"

    def test_pedido_de_autorizacao_expirado_e_rejeitado(self):
        """Pedido pendente expirado não pode ser aprovado."""
        provider = OAuthProvider(OAuthConfig(enabled=True, code_ttl=0))
        client = provider.register_client(
            {"client_name": "c", "redirect_uris": ["http://127.0.0.1:1/cb"]}
        )
        request = provider.begin_authorization(
            {
                "response_type": "code",
                "client_id": client.client_id,
                "redirect_uri": "http://127.0.0.1:1/cb",
                "code_challenge": "abc",
                "code_challenge_method": "S256",
            }
        )

        with pytest.raises(OAuthError, match="expirado"):
            provider.approve_authorization(request)

    def test_provider_desabilitado_rejeita_operacoes(self):
        """Provider desabilitado não emite tokens nem registra clients."""
        provider = OAuthProvider(OAuthConfig(enabled=False))

        with pytest.raises(OAuthError) as register_error:
            provider.register_client({"redirect_uris": ["http://127.0.0.1:1/cb"]})
        with pytest.raises(OAuthError) as token_error:
            provider.issue_token({"grant_type": "client_credentials"})

        assert register_error.value.status == 404
        assert token_error.value.status == 404

    def test_refresh_nao_pode_ampliar_escopo(self, tmp_path):
        """Refresh com escopo maior que o original deve falhar."""
        provider = OAuthProvider(OAuthConfig(enabled=True, store_path=tmp_path / "s.json"))
        client = provider.register_client(
            {"client_name": "c", "redirect_uris": ["http://127.0.0.1:5599/cb"]}
        )
        verifier, challenge = _pkce_pair()
        request = provider.begin_authorization(
            {
                "response_type": "code",
                "client_id": client.client_id,
                "redirect_uri": "http://127.0.0.1:5599/cb",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "mcp:read-local",
            }
        )
        code = parse_qs(urlparse(provider.approve_authorization(request)).query)["code"][0]
        tokens = provider.issue_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client.client_id,
                "code_verifier": verifier,
            }
        )

        with pytest.raises(OAuthError, match="ampliar"):
            provider.issue_token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                    "client_id": client.client_id,
                    "scope": "mcp:all",
                }
            )

    def test_client_estatico_sem_redirect_exige_redirect_uri(self, tmp_path):
        """Client estático sem redirect registrado não pode autorizar por browser."""
        provider = OAuthProvider(
            OAuthConfig(
                enabled=True,
                clients=parse_client_specs("robo:s3cr3t"),
                store_path=tmp_path / "s.json",
            )
        )

        with pytest.raises(OAuthError, match="redirect_uri"):
            provider.begin_authorization(
                {
                    "response_type": "code",
                    "client_id": "robo",
                    "redirect_uri": "http://127.0.0.1:5599/cb",
                }
            )

    def test_config_from_env_le_variaveis(self, monkeypatch):
        """``OAuthConfig.from_env`` deve montar a configuração pelo ambiente."""
        monkeypatch.setenv("QUIMERA_MCP_OAUTH", "1")
        monkeypatch.setenv("QUIMERA_MCP_OAUTH_ISSUER", "https://x.exemplo.dev")
        monkeypatch.setenv("QUIMERA_MCP_OAUTH_CLIENTS", "robo:abc")
        monkeypatch.setenv("QUIMERA_MCP_OAUTH_PASSCODE", "pin")
        monkeypatch.setenv("QUIMERA_MCP_OAUTH_ACCESS_TTL", "120")

        config = OAuthConfig.from_env()

        assert config.enabled is True
        assert config.issuer == "https://x.exemplo.dev"
        assert config.clients[0].client_id == "robo"
        assert config.passcode == "pin"
        assert config.access_token_ttl == 120


class TestOAuthStoreEncryption:
    """Persistência do store com e sem criptografia em disco."""

    def test_store_sem_chave_grava_texto_claro(self, tmp_path):
        """Sem store_key o JSON permanece legível (documentado como risco)."""
        from quimera.runtime.mcp.oauth import OAuthClient, OAuthStore

        path = tmp_path / "mcp_oauth.json"
        store = OAuthStore(path)
        client = OAuthClient(
            client_id="quimera-dyn",
            client_secret="super-secret",
            dynamic=True,
            created_at=time.time(),
        )
        store.save({client.client_id: client}, {})

        raw = path.read_text(encoding="utf-8")
        assert "super-secret" in raw
        assert not raw.lstrip().startswith(OAuthStore._ENCRYPTED_PREFIX)

        loaded_clients, _ = OAuthStore(path).load()
        assert loaded_clients["quimera-dyn"].client_secret == "super-secret"

    def test_store_com_chave_criptografa_e_restaura(self, tmp_path):
        """Com store_key o arquivo não expõe secrets e sobrevive ao reload."""
        pytest.importorskip("cryptography")
        from quimera.runtime.mcp.oauth import IssuedToken, OAuthClient, OAuthStore

        path = tmp_path / "mcp_oauth.json"
        key = "passphrase-forte-de-teste"
        store = OAuthStore(path, store_key=key)
        assert store.encrypted is True

        client = OAuthClient(
            client_id="quimera-dyn",
            client_secret="super-secret",
            dynamic=True,
            created_at=time.time(),
        )
        refresh = IssuedToken(
            token="refresh-value",
            client_id=client.client_id,
            scope="mcp",
            resource="",
            expires_at=time.time() + 3600,
            kind="refresh",
        )
        store.save({client.client_id: client}, {refresh.token: refresh})

        raw = path.read_text(encoding="utf-8")
        assert "super-secret" not in raw
        assert "refresh-value" not in raw
        assert raw.lstrip().startswith(OAuthStore._ENCRYPTED_PREFIX)

        loaded_clients, loaded_refresh = OAuthStore(path, store_key=key).load()
        assert loaded_clients["quimera-dyn"].client_secret == "super-secret"
        assert "refresh-value" in loaded_refresh

    def test_store_criptografado_sem_chave_retorna_vazio(self, tmp_path):
        """Arquivo cifrado não é legível sem a passphrase correta."""
        pytest.importorskip("cryptography")
        from quimera.runtime.mcp.oauth import OAuthClient, OAuthStore

        path = tmp_path / "mcp_oauth.json"
        client = OAuthClient(
            client_id="quimera-dyn",
            client_secret="super-secret",
            dynamic=True,
            created_at=time.time(),
        )
        OAuthStore(path, store_key="chave-a").save({client.client_id: client}, {})

        clients, tokens = OAuthStore(path, store_key="chave-errada").load()
        assert clients == {}
        assert tokens == {}

        clients, tokens = OAuthStore(path).load()
        assert clients == {}
        assert tokens == {}

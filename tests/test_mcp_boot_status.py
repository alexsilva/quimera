"""Bloco dinâmico de status MCP no cabeçalho de boot."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from quimera.app.mcp_status import (
    MCP_AUTH_PENDING_LABEL,
    MCP_DETAIL_LIMIT,
    MCP_HUB_HINT,
    MCP_LOG_HINT,
    MCPBootStatusPresenter,
    build_mcp_boot_status_lines,
    install_mcp_boot_status,
)
from quimera.runtime.drivers.tool_schemas import set_bridge_schemas
from quimera.runtime.mcp.client import MCPClientBridge, MCPClientSession
from quimera.runtime.tools.mcp_clients import set_bridge
from quimera.ui.base import RendererBase
from quimera.ui.boot_status import (
    BootStatusLine,
    coerce_boot_status_line,
    format_boot_status_line,
)


@pytest.fixture(autouse=True)
def _sem_navegador(monkeypatch):
    """Nenhum teste deste módulo pode abrir um navegador de verdade."""
    monkeypatch.setenv("QUIMERA_OPEN_BROWSER", "0")


def teardown_function() -> None:
    set_bridge(None)
    set_bridge_schemas([])


class RecordingRenderer(RendererBase):
    def __init__(self) -> None:
        self.blocks: list[tuple[str, list[BootStatusLine]]] = []
        self.notifications: list[tuple[str, str]] = []
        self.system: list[str] = []

    def show_system(self, message):
        self.system.append(message)

    def show_boot_status(self, key, lines):
        self.blocks.append((key, list(lines)))

    def show_notification(self, message, *, severity="information", timeout=None):
        self.notifications.append((severity, message))


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def _connect(monkeypatch, bridge: MCPClientBridge, name: str, transport_type: str = "stdio"):
    monkeypatch.setattr(MCPClientSession, "connect", lambda self: None)
    transport = MagicMock()
    transport.transport_type = transport_type
    bridge.add_connection(name, transport)


def test_build_lines_cobre_servidores_e_fases_dos_clients():
    bridge = MCPClientBridge()
    bridge.mark_pending("a", "remote")
    bridge.mark_connecting("b", "http")
    bridge.set_auth_url("b", "https://auth.example.test/authorize?x=1")
    bridge.mark_failed("c", "Connection refused", transport="stdio")
    set_bridge_schemas([_schema("x_1"), _schema("x_2")])
    app = SimpleNamespace(
        mcp_socket_path="/tmp/q.sock",
        mcp_http_url="http://localhost:9096/mcp",
    )

    lines = build_mcp_boot_status_lines(app, bridge)

    assert lines[0] == BootStatusLine("ok", "MCP interno iniciado em /tmp/q.sock")
    assert lines[1] == BootStatusLine(
        "ok", "MCP HTTP externo iniciado em http://localhost:9096/mcp"
    )
    assert lines[2] == BootStatusLine(
        "pending", "MCP client 'a' (remote): aguardando conexão…"
    )
    assert lines[3] == BootStatusLine(
        "busy", f"MCP client 'b' (http): conectando… · {MCP_AUTH_PENDING_LABEL}"
    )
    # A URL de autorização tem linha própria e nunca é cortada.
    assert lines[4] == BootStatusLine(
        "info",
        "autorize 'b' no navegador:",
        url="https://auth.example.test/authorize?x=1",
    )
    assert lines[5] == BootStatusLine(
        "error",
        f"MCP client 'c' (stdio): falha — Connection refused · {MCP_HUB_HINT}",
    )
    assert lines[6] == BootStatusLine("info", MCP_LOG_HINT.capitalize())
    assert len(lines) == 7


def test_build_lines_nao_tem_linha_de_resumo(monkeypatch):
    """A contagem "N/M conexões ativas" confundia com o estado real; saiu."""
    bridge = MCPClientBridge()
    _connect(monkeypatch, bridge, "jira")
    set_bridge_schemas([_schema("jira_search")])
    app = SimpleNamespace(mcp_socket_path=None, mcp_http_url=None)

    lines = build_mcp_boot_status_lines(app, bridge)
    assert lines == [BootStatusLine("ok", "MCP client 'jira' (stdio): conectado · 0 tool(s)")]

    bridge.mark_failed("wiki", "timeout", transport="http")
    lines = build_mcp_boot_status_lines(app, bridge)
    assert [line.status for line in lines] == ["ok", "error", "info"]
    assert not any("conexão(ões)" in line.text or "disponíveis" in line.text for line in lines)

    bridge.disconnect_connection("jira")
    bridge.forget_connection("wiki")
    lines = build_mcp_boot_status_lines(app, bridge)
    assert lines == [BootStatusLine("off", "MCP client 'jira' (stdio): desconectado")]


def test_build_lines_url_de_autorizacao_longa_nao_e_cortada(monkeypatch):
    bridge = MCPClientBridge()
    url = "https://mcp.atlassian.com/v1/authorize?" + "&".join(
        f"p{i}={'x' * 40}" for i in range(20)
    )
    bridge.mark_connecting("jira", "stdio")
    bridge.set_auth_url("jira", url)
    app = SimpleNamespace(mcp_socket_path=None, mcp_http_url=None)

    lines = build_mcp_boot_status_lines(app, bridge)

    assert len(url) > MCP_DETAIL_LIMIT
    assert lines[1].url == url
    assert "…" not in lines[1].text
    assert format_boot_status_line(lines[1]) == f"· autorize 'jira' no navegador: {url}"

    # Sessão viva que pede renovação de token também expõe o link.
    _connect(monkeypatch, bridge, "wiki")
    bridge.set_auth_url("wiki", "https://auth/renew")
    lines = build_mcp_boot_status_lines(app, bridge)
    assert lines[2].text == (
        f"MCP client 'wiki' (stdio): conectado · 0 tool(s) · {MCP_AUTH_PENDING_LABEL}"
    )
    assert lines[3].url == "https://auth/renew"


def test_boot_status_line_url_sobrevive_a_serializacao():
    line = BootStatusLine("info", "autorize 'jira' no navegador:", url="https://auth")

    assert line.as_payload() == {
        "status": "info",
        "text": "autorize 'jira' no navegador:",
        "url": "https://auth",
    }
    assert coerce_boot_status_line(line.as_payload()) == line
    assert coerce_boot_status_line(("info", "texto", "https://auth")).url == "https://auth"
    assert BootStatusLine("ok", "sem link").as_payload() == {"status": "ok", "text": "sem link"}
    assert format_boot_status_line(BootStatusLine("ok", "sem link")) == "● sem link"


def test_build_lines_aponta_o_log_do_app_quando_ha_falha(tmp_path):
    """O stderr do processo vai para o log; o bloco diz onde ele está."""
    bridge = MCPClientBridge()
    bridge.mark_failed("jira", "conexão fechada pelo servidor", transport="stdio")
    session_paths = SimpleNamespace(
        app_log_path_for=lambda session_id: tmp_path / f"app-{session_id}.log"
    )
    app = SimpleNamespace(
        mcp_socket_path=None,
        mcp_http_url=None,
        session_paths=session_paths,
        storage=SimpleNamespace(session_id="sessao-1"),
    )

    lines = build_mcp_boot_status_lines(app, bridge)

    assert lines[1] == BootStatusLine(
        "info", f"Detalhes das falhas em {tmp_path / 'app-sessao-1.log'}"
    )
    assert lines[0].status == "error"
    assert len(lines) == 2


def test_build_lines_corta_detalhes_longos_para_nao_vazar_stderr():
    """Um erro com stack trace nunca ocupa mais que uma linha curta no bloco."""
    bridge = MCPClientBridge()
    noise = "Fatal error: EROFS " + "\n    at async open (node:internal) " * 40
    bridge.mark_failed("jira", noise, transport="stdio")
    bridge.mark_connecting("wiki", "http")
    bridge.set_state_detail("wiki", "x" * 500)
    app = SimpleNamespace(mcp_socket_path=None, mcp_http_url=None)

    lines = build_mcp_boot_status_lines(app, bridge)

    failure = lines[0].text
    assert failure.startswith("MCP client 'jira' (stdio): falha — Fatal error: EROFS at async open")
    assert "\n" not in failure
    assert "…" in failure
    assert failure.endswith(MCP_HUB_HINT)
    assert len(failure) < len("MCP client 'jira' (stdio): falha —  · ") + MCP_DETAIL_LIMIT + len(
        MCP_HUB_HINT
    ) + 2
    assert lines[1].text == f"MCP client 'wiki' (http): conectando… · {'x' * (MCP_DETAIL_LIMIT - 1)}…"


def test_build_lines_trata_sessoes_sem_estado_como_conectadas():
    bridge = MCPClientBridge()
    bridge._sessions["legacy"] = MagicMock()
    app = SimpleNamespace(mcp_socket_path=None, mcp_http_url=None)

    lines = build_mcp_boot_status_lines(app, bridge)

    assert lines[0] == BootStatusLine("ok", "MCP client 'legacy': conectado · 0 tool(s)")


def test_build_lines_sem_mcp_retorna_vazio():
    app = SimpleNamespace(mcp_socket_path=None, mcp_http_url=None)

    assert build_mcp_boot_status_lines(app, MCPClientBridge()) == []
    assert build_mcp_boot_status_lines(app, None) == []


def test_presenter_rerenderiza_a_cada_transicao_e_notifica_falhas(monkeypatch):
    bridge = MCPClientBridge()
    bridge.mark_pending("jira", "stdio")
    bridge.mark_pending("wiki", "http")
    renderer = RecordingRenderer()
    app = SimpleNamespace(mcp_socket_path="/tmp/q.sock", mcp_http_url=None)
    presenter = MCPBootStatusPresenter(app, renderer, bridge=bridge)

    presenter.start()

    assert len(renderer.blocks) == 1
    assert renderer.blocks[0][0] == "mcp"
    assert renderer.blocks[0][1][0] == BootStatusLine("ok", "MCP interno iniciado em /tmp/q.sock")
    assert renderer.notifications == []

    _connect(monkeypatch, bridge, "jira")
    assert renderer.blocks[-1][1][1] == BootStatusLine(
        "ok", "MCP client 'jira' (stdio): conectado · 0 tool(s)"
    )

    bridge.mark_failed("wiki", "Connection refused", transport="http")
    assert [line.status for line in renderer.blocks[-1][1]] == ["ok", "ok", "error", "info"]
    assert [severity for severity, _ in renderer.notifications] == ["warning"]
    assert "MCP client 'wiki' falhou ao conectar: Connection refused" in renderer.notifications[0][1]
    assert MCP_HUB_HINT in renderer.notifications[0][1]

    bridge.mark_connecting("wiki", "http")
    bridge.set_auth_url("wiki", "https://auth")
    assert renderer.notifications[-1][0] == "warning"
    assert renderer.notifications[-1][1].startswith(
        f"MCP client 'wiki': {MCP_AUTH_PENDING_LABEL}"
    )
    assert renderer.blocks[-1][1][-1] == BootStatusLine(
        "info", "autorize 'wiki' no navegador:", url="https://auth"
    )

    presenter.stop()
    rendered = len(renderer.blocks)
    bridge.mark_failed("wiki", "de novo", transport="http")
    assert len(renderer.blocks) == rendered


def test_presenter_so_notifica_autorizacao_oauth_durante_a_conexao():
    """Detalhes de progresso (ex.: linhas de erro do stderr) não viram toast."""
    bridge = MCPClientBridge()
    bridge.mark_connecting("jira", "stdio")
    renderer = RecordingRenderer()
    opened: list[str] = []
    presenter = MCPBootStatusPresenter(
        SimpleNamespace(mcp_socket_path=None, mcp_http_url=None),
        renderer,
        bridge=bridge,
        browser_opener=lambda url: opened.append(url) or True,
    )

    presenter.start()
    bridge.set_state_detail("jira", "Proactive token refresh failed, falling back")
    assert renderer.notifications == []
    assert opened == []

    bridge.set_auth_url("jira", "https://auth")
    assert opened == ["https://auth"]
    assert renderer.notifications == [
        (
            "information",
            "MCP client 'jira': autorização aberta no navegador. "
            "Se nada abriu, clique no link do bloco de status ou use F9.",
        )
    ]

    bridge.mark_failed("jira", "conexão fechada pelo servidor · Fatal error: EROFS", transport="stdio")
    assert len(renderer.notifications) == 2
    assert renderer.notifications[-1][0] == "warning"
    assert "Fatal error: EROFS" in renderer.notifications[-1][1]


def test_presenter_abre_o_navegador_uma_vez_por_url():
    bridge = MCPClientBridge()
    bridge.mark_connecting("jira", "stdio")
    renderer = RecordingRenderer()
    opened: list[str] = []
    presenter = MCPBootStatusPresenter(
        SimpleNamespace(mcp_socket_path=None, mcp_http_url=None),
        renderer,
        bridge=bridge,
        browser_opener=lambda url: opened.append(url) or True,
    )

    presenter.start()
    bridge.set_auth_url("jira", "https://auth/1")
    bridge.set_state_detail("jira", "outro progresso")
    presenter.refresh()
    assert opened == ["https://auth/1"]
    assert len(renderer.notifications) == 1

    # Nova URL (ex.: reconectar pelo hub) abre de novo; a mesma URL, não.
    bridge.mark_connecting("jira", "stdio")
    bridge.set_auth_url("jira", "https://auth/2")
    bridge.set_auth_url("jira", "https://auth/2")
    assert opened == ["https://auth/1", "https://auth/2"]
    assert len(renderer.notifications) == 2


def test_presenter_avisa_quando_o_navegador_nao_abre():
    bridge = MCPClientBridge()
    bridge.mark_connecting("jira", "stdio")
    renderer = RecordingRenderer()
    presenter = MCPBootStatusPresenter(
        SimpleNamespace(mcp_socket_path=None, mcp_http_url=None),
        renderer,
        bridge=bridge,
        browser_opener=lambda url: False,
    )

    presenter.start()
    bridge.set_auth_url("jira", "https://auth")

    assert renderer.notifications == [
        (
            "warning",
            f"MCP client 'jira': {MCP_AUTH_PENDING_LABEL} — "
            "clique no link do bloco de status ou use F9 → Autorizar.",
        )
    ]


def test_presenter_padrao_respeita_kill_switch_do_navegador(monkeypatch):
    """Sem opener injetado o presenter usa open_in_browser, desligado por env."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "quimera.ui.browser.subprocess.Popen", lambda cmd, **kw: calls.append(cmd)
    )
    bridge = MCPClientBridge()
    bridge.mark_connecting("jira", "stdio")
    renderer = RecordingRenderer()
    presenter = MCPBootStatusPresenter(
        SimpleNamespace(mcp_socket_path=None, mcp_http_url=None), renderer, bridge=bridge
    )

    presenter.start()
    bridge.set_auth_url("jira", "https://auth")

    assert calls == []
    assert renderer.notifications[-1][0] == "warning"


def test_presenter_nao_repete_notificacao_da_mesma_falha():
    bridge = MCPClientBridge()
    bridge.mark_failed("wiki", "Connection refused", transport="http")
    renderer = RecordingRenderer()
    presenter = MCPBootStatusPresenter(
        SimpleNamespace(mcp_socket_path=None, mcp_http_url=None), renderer, bridge=bridge
    )

    presenter.start()
    presenter.refresh()
    bridge.set_state_detail("wiki", "Connection refused")

    assert len(renderer.notifications) == 1


def test_presenter_imprime_linhas_novas_em_renderer_sem_bloco():
    class PlainRenderer:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def show_boot_message(self, message):
            self.lines.append(message)

    bridge = MCPClientBridge()
    bridge.mark_connecting("jira", "stdio")
    renderer = PlainRenderer()
    presenter = MCPBootStatusPresenter(
        SimpleNamespace(mcp_socket_path="/tmp/q.sock", mcp_http_url=None),
        renderer,
        bridge=bridge,
    )

    presenter.start()
    bridge.mark_failed("jira", "boom", transport="stdio")

    assert renderer.lines[0] == "● MCP interno iniciado em /tmp/q.sock"
    assert renderer.lines[1] == "◌ MCP client 'jira' (stdio): conectando…"
    assert any(
        line.startswith("✗ MCP client 'jira' (stdio): falha — boom") for line in renderer.lines
    )
    assert renderer.lines.count("● MCP interno iniciado em /tmp/q.sock") == 1


def test_presenter_tolera_renderer_sem_canal_de_boot():
    bridge = MCPClientBridge()
    bridge.mark_connecting("jira", "stdio")
    presenter = MCPBootStatusPresenter(
        SimpleNamespace(mcp_socket_path=None, mcp_http_url=None), object(), bridge=bridge
    )

    presenter.start()
    bridge.mark_failed("jira", "boom", transport="stdio")
    presenter.stop()


def test_install_mcp_boot_status_registra_no_app_e_substitui_anterior():
    bridge = MCPClientBridge()
    bridge.mark_pending("jira", "stdio")
    set_bridge(bridge)
    renderer = RecordingRenderer()
    app = SimpleNamespace(renderer=renderer, mcp_socket_path=None, mcp_http_url=None)

    first = install_mcp_boot_status(app)
    second = install_mcp_boot_status(app)

    assert app.mcp_boot_status is second
    assert first is not second
    bridge.mark_connecting("jira", "stdio")
    # Duas renderizações iniciais + uma transição vista só pelo presenter ativo.
    assert len(renderer.blocks) == 3
    second.stop()

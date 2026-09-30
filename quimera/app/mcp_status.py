"""Bloco dinâmico de status MCP exibido no cabeçalho de boot do chat.

Substitui as linhas estáticas "MCP interno iniciado em…", "MCP HTTP externo
iniciado em…" e "MCP client ativo: N tools…" por um bloco único que acompanha
o bridge de MCP clients: cada conexão externa aparece com sua fase
(aguardando, conectando, conectada, falha). Como os handshakes rodam em
background, o bloco é a forma de o usuário saber que a inicialização não
ficou presa e onde agir (MCP Hub, F9) quando algo falha.

Uma autorização OAuth pendente ganha uma linha própria com a URL completa
(clicável na TUI) e o navegador é aberto automaticamente uma vez por URL —
o ``mcp-remote`` roda no sandbox do workspace e não consegue abri-lo.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from ..runtime.mcp.client import (
    MCPConnectionPhase,
    MCPConnectionState,
    shorten_text,
)
from ..runtime.tools.mcp_clients import get_bridge
from ..ui.boot_status import (
    BOOT_STATUS_BUSY,
    BOOT_STATUS_ERROR,
    BOOT_STATUS_INFO,
    BOOT_STATUS_OFF,
    BOOT_STATUS_OK,
    BOOT_STATUS_PENDING,
    BootStatusLine,
    format_boot_status_line,
)
from ..ui.browser import open_in_browser

logger = logging.getLogger(__name__)

MCP_BOOT_STATUS_KEY = "mcp"
MCP_HUB_HINT = "F9 abre o MCP Hub para reconectar"
MCP_LOG_HINT = "detalhes das falhas no log do app"
MCP_AUTH_PENDING_LABEL = "autorização pendente no navegador"
#: Tamanho máximo do detalhe de uma conexão no bloco. O motivo já chega
#: resumido (``MCPConnectError``); o corte é a garantia de que nenhum erro
#: inesperado despeje um stack trace no chat.
MCP_DETAIL_LIMIT = 200

BrowserOpener = Callable[[str], bool]


def _client_label(state: MCPConnectionState) -> str:
    transport = f" ({state.transport})" if state.transport else ""
    return f"MCP client '{state.name}'{transport}"


def _auth_line(state: MCPConnectionState) -> BootStatusLine:
    """Linha com a URL de autorização completa: o link nunca é cortado."""
    return BootStatusLine(
        BOOT_STATUS_INFO,
        f"autorize '{state.name}' no navegador:",
        url=state.auth_url,
    )


def _client_lines(state: MCPConnectionState) -> list[BootStatusLine]:
    label = _client_label(state)
    detail = shorten_text(state.detail, MCP_DETAIL_LIMIT)
    if state.phase == MCPConnectionPhase.CONNECTING:
        text = f"{label}: conectando…"
        if state.auth_pending:
            text = f"{text} · {MCP_AUTH_PENDING_LABEL}"
        elif detail:
            text = f"{text} · {detail}"
        lines = [BootStatusLine(BOOT_STATUS_BUSY, text)]
        if state.auth_pending:
            lines.append(_auth_line(state))
        return lines
    if state.phase == MCPConnectionPhase.CONNECTED:
        text = f"{label}: conectado · {state.tools} tool(s)"
        if detail:
            text = f"{text} · {detail}"
        if state.auth_pending:
            # Renovação de token pedida pelo servidor com a sessão viva.
            text = f"{text} · {MCP_AUTH_PENDING_LABEL}"
        lines = [BootStatusLine(BOOT_STATUS_OK, text)]
        if state.auth_pending:
            lines.append(_auth_line(state))
        return lines
    if state.phase == MCPConnectionPhase.FAILED:
        reason = detail or "erro desconhecido"
        return [
            BootStatusLine(
                BOOT_STATUS_ERROR, f"{label}: falha — {reason} · {MCP_HUB_HINT}"
            )
        ]
    if state.phase == MCPConnectionPhase.DISCONNECTED:
        return [BootStatusLine(BOOT_STATUS_OFF, f"{label}: desconectado")]
    return [BootStatusLine(BOOT_STATUS_PENDING, f"{label}: aguardando conexão…")]


def _app_log_path(app) -> str:
    """Caminho do log do app desta sessão, se o app o expõe; vazio caso contrário."""
    session_paths = getattr(app, "session_paths", None)
    resolver = getattr(session_paths, "app_log_path_for", None)
    session_id = getattr(getattr(app, "storage", None), "session_id", None)
    if not callable(resolver) or not session_id:
        return ""
    try:
        return str(resolver(session_id))
    except Exception:
        return ""


def _log_hint_line(app) -> BootStatusLine:
    path = _app_log_path(app)
    text = f"Detalhes das falhas em {path}" if path else MCP_LOG_HINT.capitalize()
    return BootStatusLine(BOOT_STATUS_INFO, text)


def _bridge_client_states(bridge) -> list[MCPConnectionState]:
    """Estados publicados pelo bridge, completados por sessões sem estado.

    Sessões inseridas diretamente (ex.: testes ou integrações antigas) não
    passam pelas transições do bridge; aparecem como conectadas.
    """
    if bridge is None:
        return []
    states_getter = getattr(bridge, "states", None)
    states = dict(states_getter()) if callable(states_getter) else {}
    for name in getattr(bridge, "sessions", {}) or {}:
        if name not in states:
            states[name] = MCPConnectionState(
                name=name, phase=MCPConnectionPhase.CONNECTED
            )
    return list(states.values())


def build_mcp_boot_status_lines(app, bridge=None) -> list[BootStatusLine]:
    """Monta as linhas do bloco MCP a partir do app e do bridge de clients.

    Uma linha por conexão (mais a URL quando há autorização pendente) e, se
    alguma falhou, o caminho do log. Não há linha de resumo: a contagem de
    "conexões ativas" confundia com o estado real de cada servidor.
    """
    lines: list[BootStatusLine] = []
    socket_path = getattr(app, "mcp_socket_path", None)
    http_url = getattr(app, "mcp_http_url", None)
    if socket_path:
        lines.append(BootStatusLine(BOOT_STATUS_OK, f"MCP interno iniciado em {socket_path}"))
    if http_url:
        lines.append(BootStatusLine(BOOT_STATUS_OK, f"MCP HTTP externo iniciado em {http_url}"))
    if bridge is None:
        bridge = get_bridge()
    states = _bridge_client_states(bridge)
    for state in states:
        lines.extend(_client_lines(state))
    if any(state.failed for state in states):
        # Comando e stderr completos de cada falha estão no log do app,
        # nunca no chat; a linha diz onde procurar.
        lines.append(_log_hint_line(app))
    return lines


class MCPBootStatusPresenter:
    """Mantém o bloco de status MCP do boot sincronizado com o bridge.

    ``start()`` renderiza o bloco e assina as mudanças de estado do bridge;
    cada transição re-renderiza o bloco no lugar (Textual) ou imprime só as
    linhas alteradas (renderers sequenciais). Falhas e pedidos de autorização
    OAuth também geram uma notificação fora do feed, já que o bloco pode
    estar rolado para fora da tela quando a conexão termina. O chat só recebe
    o resumo de uma linha de cada falha; stderr e comando ficam no log do app.

    Cada URL de autorização nova é aberta no navegador uma única vez via
    ``browser_opener`` (padrão: :func:`open_in_browser`).
    """

    def __init__(
        self,
        app,
        renderer,
        *,
        bridge=None,
        key: str = MCP_BOOT_STATUS_KEY,
        browser_opener: BrowserOpener | None = None,
    ) -> None:
        self._app = app
        self._renderer = renderer
        self._bridge = bridge if bridge is not None else get_bridge()
        self._key = key
        self._browser_opener: BrowserOpener = (
            browser_opener if browser_opener is not None else open_in_browser
        )
        self._unsubscribe = None
        self._lock = threading.RLock()
        self._last_seen: dict[str, tuple[str, str, str]] = {}
        self._opened_urls: set[str] = set()
        self._printed: set[BootStatusLine] = set()

    @property
    def bridge(self):
        return self._bridge

    def start(self) -> None:
        """Renderiza o bloco e passa a acompanhar o bridge."""
        subscribe = getattr(self._bridge, "subscribe", None)
        if callable(subscribe) and self._unsubscribe is None:
            self._unsubscribe = subscribe(self.refresh)
        self.refresh()

    def stop(self) -> None:
        """Cancela a assinatura no bridge (idempotente)."""
        unsubscribe = self._unsubscribe
        self._unsubscribe = None
        if callable(unsubscribe):
            try:
                unsubscribe()
            except Exception:
                logger.debug("MCP boot status: falha ao cancelar assinatura", exc_info=True)

    def refresh(self) -> None:
        """Re-renderiza o bloco com o estado atual; seguro em qualquer thread."""
        with self._lock:
            try:
                lines = build_mcp_boot_status_lines(self._app, self._bridge)
                if lines:
                    self._emit_lines(lines)
                self._notify_transitions()
            except Exception:
                logger.exception("MCP boot status: falha ao atualizar o bloco")

    def _emit_lines(self, lines: list[BootStatusLine]) -> None:
        show_status = getattr(self._renderer, "show_boot_status", None)
        if callable(show_status):
            show_status(self._key, lines)
            return
        # Renderer fora do contrato base: imprime só o que ainda não apareceu.
        show_line = getattr(self._renderer, "show_boot_message", None)
        if not callable(show_line):
            show_line = getattr(self._renderer, "show_system_neutral", None)
        if not callable(show_line):
            return
        for line in lines:
            if line in self._printed:
                continue
            self._printed.add(line)
            show_line(format_boot_status_line(line))

    def _open_authorization(self, name: str, url: str) -> bool:
        """Abre a URL de autorização uma vez; False se o navegador não pôde abrir."""
        if url in self._opened_urls:
            return False
        self._opened_urls.add(url)
        try:
            opened = bool(self._browser_opener(url))
        except Exception:
            logger.debug("MCP boot status: abrir navegador falhou", exc_info=True)
            opened = False
        logger.info(
            "MCP client '%s': autorização OAuth %s",
            name,
            "aberta no navegador" if opened else "pendente (navegador indisponível)",
        )
        return opened

    def _notify_transitions(self) -> None:
        notify = getattr(self._renderer, "show_notification", None)
        if not callable(notify):
            return
        for state in _bridge_client_states(self._bridge):
            current = (state.phase, str(state.detail or ""), str(state.auth_url or ""))
            previous = self._last_seen.get(state.name)
            self._last_seen[state.name] = current
            if previous == current:
                continue
            message = None
            severity = "information"
            timeout: float | None = None
            if state.failed and (previous is None or previous[0] != MCPConnectionPhase.FAILED):
                reason = (
                    shorten_text(state.detail, MCP_DETAIL_LIMIT) or "erro desconhecido"
                ).rstrip(".")
                message = (
                    f"MCP client '{state.name}' falhou ao conectar: "
                    f"{reason}. {MCP_HUB_HINT}."
                )
                severity = "warning"
                timeout = 12
            elif state.auth_url and state.auth_url != (previous[2] if previous else ""):
                # Só a autorização OAuth pede ação do usuário; outros detalhes
                # de progresso ficam no bloco e no log.
                if self._open_authorization(state.name, state.auth_url):
                    message = (
                        f"MCP client '{state.name}': autorização aberta no navegador. "
                        "Se nada abriu, clique no link do bloco de status ou use F9."
                    )
                else:
                    message = (
                        f"MCP client '{state.name}': {MCP_AUTH_PENDING_LABEL} — "
                        "clique no link do bloco de status ou use F9 → Autorizar."
                    )
                    severity = "warning"
                timeout = 20
            if message is None:
                continue
            try:
                notify(message, severity=severity, timeout=timeout)
            except Exception:
                logger.debug("MCP boot status: notificação falhou", exc_info=True)


def install_mcp_boot_status(app, renderer=None) -> MCPBootStatusPresenter:
    """Cria, registra em ``app.mcp_boot_status`` e inicia o presenter."""
    renderer = renderer if renderer is not None else getattr(app, "renderer", None)
    presenter = MCPBootStatusPresenter(app, renderer)
    previous = getattr(app, "mcp_boot_status", None)
    if isinstance(previous, MCPBootStatusPresenter):
        previous.stop()
    try:
        setattr(app, "mcp_boot_status", presenter)
    except Exception:
        logger.debug("MCP boot status: app não aceita atributo", exc_info=True)
    presenter.start()
    return presenter

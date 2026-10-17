"""Hub visual para inspecionar e gerenciar conexões MCP da sessão."""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select

from quimera.runtime.drivers.tool_schemas import get_bridge_schemas
from quimera.runtime.mcp.manager import MCPConnectionInfo, MCPConnectionManager
from quimera.runtime.mcp.session import (
    describe_http_error,
    load_persisted_authorized_clients,
    start_external_mcp_http,
    stop_external_mcp_http,
)
from quimera.runtime.mcp.settings import (
    SCOPE_GLOBAL,
    SCOPE_LOCAL,
    MCPHttpSettings,
    MCPHttpSettingsStore,
)
from quimera.ui.browser import open_in_browser

SCOPE_OPTIONS = [
    ("Global · todos os workspaces", SCOPE_GLOBAL),
    ("Local · só este workspace", SCOPE_LOCAL),
]


@dataclass(frozen=True)
class MCPClientGroup:
    """Um client OAuth na aba Clientes: todos os cadastros com o mesmo nome.

    ChatGPT e similares repetem o registro dinâmico e acumulam ``client_id``;
    para quem usa o Hub é um único client, e é pelo nome que ele é liberado
    por workspace. ``client_ids`` guarda os cadastros, para revogar e fechar
    sessões de todos de uma vez. ``activation_scope`` diz quem decidiu
    ``active``: o padrão global ou uma exceção deste workspace.
    """

    name: str
    client_ids: tuple[str, ...]
    scope: str
    authorized: bool
    connected: bool
    active: bool
    activation_scope: str = SCOPE_LOCAL

    @property
    def label(self) -> str:
        count = len(self.client_ids)
        return f"{self.name} ×{count}" if count > 1 else self.name

    @property
    def state_label(self) -> str:
        if not self.authorized:
            return "não autorizado"
        if not self.active:
            return "inativo"
        return "conectado" if self.connected else "ativo"


def group_clients_by_name(clients, decision) -> list[MCPClientGroup]:
    """Agrupa os clients conhecidos pelo servidor HTTP por nome, na ordem em que aparecem.

    ``decision(nome)`` devolve ``(liberado, quem decide)`` para o workspace.
    """
    groups: dict[str, dict] = {}
    for client in clients:
        name = str(client.client_name or client.client_id or client.session_id[:8] or "").strip()
        if not name:
            continue
        key = client.client_id or client.session_id
        entry = groups.setdefault(
            name, {"ids": [], "scope": "", "authorized": False, "connected": False}
        )
        if key and key not in entry["ids"]:
            entry["ids"].append(key)
        entry["scope"] = entry["scope"] or str(client.scope or client.profile or "mcp")
        entry["authorized"] = entry["authorized"] or bool(getattr(client, "authorized", False))
        entry["connected"] = entry["connected"] or bool(getattr(client, "connected", False))
    return [
        MCPClientGroup(
            name,
            tuple(entry["ids"]),
            entry["scope"],
            entry["authorized"],
            entry["connected"],
            *decision(name),
        )
        for name, entry in groups.items()
    ]


class MCPScopePromptScreen(ModalScreen[str | None]):
    """Pergunta onde vale um Ativar/Desativar: só neste workspace ou em todos.

    Devolve ``local``, ``global`` ou ``None`` (cancelado). O foco inicial fica
    em "Neste workspace": o caminho curto nunca amplia o alcance sozinho.
    """

    CSS = """
    MCPScopePromptScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }
    #mcp_scope_dialog {
        width: 78;
        height: auto;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    #mcp_scope_title {
        width: 100%;
        text-style: bold;
        color: $accent;
    }
    #mcp_scope_detail {
        width: 100%;
        margin-top: 1;
        color: $text-muted;
    }
    #mcp_scope_buttons {
        height: 1;
        margin-top: 1;
    }
    #mcp_scope_buttons Button {
        height: 1;
        min-width: 14;
        border: none;
        margin-right: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancelar"),
        ("l", "choose('local')", "Neste workspace"),
        ("g", "choose('global')", "Em todos"),
    ]

    AUTO_FOCUS = "#mcp_scope_local"

    def __init__(self, title: str, detail: str) -> None:
        super().__init__()
        self._title = title
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Container(id="mcp_scope_dialog"):
            yield Label(self._title, id="mcp_scope_title")
            yield Label(self._detail, id="mcp_scope_detail")
            with Horizontal(id="mcp_scope_buttons"):
                yield Button("Neste workspace (L)", id="mcp_scope_local", variant="primary")
                yield Button("Em todos (G)", id="mcp_scope_global")
                yield Button("Cancelar (Esc)", id="mcp_scope_cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        scopes = {"mcp_scope_local": SCOPE_LOCAL, "mcp_scope_global": SCOPE_GLOBAL}
        self.dismiss(scopes.get(event.button.id or ""))

    def action_choose(self, scope: str) -> None:
        self.dismiss(scope)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MCPServerEditorScreen(ModalScreen[str | None]):
    """Editor compacto para criar ou reconfigurar um servidor MCP de saída."""

    CSS = """
    MCPServerEditorScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }
    #mcp_editor_dialog {
        width: 82;
        height: 90%;
        /* Altura natural do formulário: 20 linhas de campos + 10 de moldura
           (borda, padding, header, botões e rodapé). Acima disso o diálogo não
           cresce; abaixo, só a área de campos rola. */
        max-height: 40;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    #mcp_editor_header {
        height: 1;
        margin-bottom: 1;
    }
    #mcp_editor_title {
        width: 1fr;
        content-align: center middle;
        text-style: bold;
        color: $accent;
    }
    #mcp_editor_close {
        width: 3;
        min-width: 3;
        height: 1;
        border: none;
        padding: 0;
    }
    #mcp_editor_fields {
        height: 1fr;
        min-height: 3;
        /* Calha fixa: os campos nascem com a largura final, e o foco inicial
           não rola o formulário ao surgir a barra de rolagem. */
        scrollbar-gutter: stable;
    }
    #mcp_editor_fields Label {
        margin-top: 1;
        color: $text-muted;
    }
    #mcp_editor_fields Input, #mcp_editor_fields Select {
        width: 100%;
    }
    #mcp_editor_buttons {
        height: 1;
        margin-top: 1;
        align-horizontal: right;
    }
    #mcp_editor_buttons Button {
        height: 1;
        min-width: 14;
        border: none;
        margin-left: 1;
    }
    #mcp_editor_activity {
        height: 1;
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancelar"),
        ("ctrl+s", "save", "Salvar"),
    ]

    AUTO_FOCUS = "#mcp_editor_name"

    _HINT = "Ctrl+S salva · Esc cancela"

    def __init__(
        self,
        manager: MCPConnectionManager,
        parent_app,
        info: MCPConnectionInfo | None = None,
    ) -> None:
        super().__init__()
        self.manager = manager
        self.parent_app = parent_app
        self.info = info
        self._busy = False

    def compose(self) -> ComposeResult:
        info = self.info
        transport = info.transport if info is not None else "remote"
        if transport not in {"remote", "http", "stdio", "socket"}:
            transport = "http"
        # O escopo do editor é onde a configuração fica gravada: catálogo
        # global ou só este workspace. Não é a ativação, que tem botão próprio
        # e nunca muda ao salvar. Arquivos anteriores à divisão global/local
        # não tinham escopo; eles são conexões locais do workspace por
        # compatibilidade.
        scope = getattr(info, "scope", SCOPE_LOCAL) if info is not None else SCOPE_LOCAL
        if scope not in {SCOPE_GLOBAL, SCOPE_LOCAL}:
            scope = SCOPE_LOCAL
        with Container(id="mcp_editor_dialog"):
            with Horizontal(id="mcp_editor_header"):
                yield Label(
                    "Editar servidor MCP" if info is not None else "Adicionar servidor MCP",
                    id="mcp_editor_title",
                )
                yield Button("×", id="mcp_editor_close")

            with VerticalScroll(id="mcp_editor_fields"):
                yield Label("Nome")
                yield Input(
                    value=info.name if info is not None else "",
                    placeholder="github",
                    id="mcp_editor_name",
                )

                yield Label("Escopo · onde a configuração fica: catálogo global ou só este workspace")
                yield Select(
                    SCOPE_OPTIONS,
                    value=scope,
                    allow_blank=False,
                    id="mcp_editor_scope",
                )

                yield Label("Transporte")
                yield Select(
                    [
                        ("Remote / OAuth", "remote"),
                        ("HTTP", "http"),
                        ("STDIO", "stdio"),
                        ("Socket Unix", "socket"),
                    ],
                    value=transport,
                    id="mcp_editor_transport",
                )

                yield Label("Endpoint / comando / path")
                yield Input(
                    value=info.endpoint if info is not None else "",
                    placeholder="https://mcp.exemplo.com/mcp",
                    id="mcp_editor_endpoint",
                )

                yield Label("Ambiente opcional · KEY=valor,KEY2=valor2")
                yield Input(
                    value=self.manager.env_text_for(info.name) if info is not None else "",
                    placeholder="TOKEN=...",
                    id="mcp_editor_env",
                )

            with Horizontal(id="mcp_editor_buttons"):
                yield Button("Cancelar", id="mcp_editor_cancel")
                yield Button("Salvar", id="mcp_editor_save", variant="primary")
            yield Label(self._HINT, id="mcp_editor_activity")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in {"mcp_editor_close", "mcp_editor_cancel"}:
            self.action_cancel()
        elif event.button.id == "mcp_editor_save":
            self.action_save()

    def action_cancel(self) -> None:
        if not self._busy:
            self.dismiss(None)

    def action_save(self) -> None:
        """Valida, persiste e aplica a configuração em background.

        Salvar não liga nem desliga a conexão: o manager preserva a decisão
        de ativação e só refaz o handshake se ela está ativa aqui e a
        configuração mudou. O editor não espera o servidor responder: uma
        conexão ``remote:`` pode ficar minutos aguardando a autorização OAuth
        no navegador, e a tabela do hub mostra o progresso depois que o
        editor fecha.
        """
        if self._busy:
            return
        try:
            spec, env_spec, name, scope = self._build_specs()
        except ValueError as exc:
            self.parent_app.notify(str(exc), severity="error")
            return
        previous_name = self.info.name if self.info is not None else None
        self._busy = True
        self.query_one("#mcp_editor_activity", Label).update(f"Salvando {name}…")
        try:
            info = self.manager.upsert_in_background(
                spec, env_spec=env_spec, scope=scope, previous_name=previous_name
            )
        except Exception as exc:
            self._save_failed(name, str(exc))
            return
        self._save_succeeded(name, scope, info)

    def _build_specs(self) -> tuple[str, str, str, str]:
        name = self.query_one("#mcp_editor_name", Input).value.strip()
        endpoint = self.query_one("#mcp_editor_endpoint", Input).value.strip()
        transport_value = self.query_one("#mcp_editor_transport", Select).value
        transport = str(transport_value or "").strip()
        scope = str(self.query_one("#mcp_editor_scope", Select).value or "").strip()
        if scope not in {SCOPE_GLOBAL, SCOPE_LOCAL}:
            raise ValueError("Selecione o escopo da conexão: global ou local.")
        if not name:
            raise ValueError("Informe um nome para a conexão MCP.")
        if any(ch in name for ch in " =:"):
            raise ValueError("O nome da conexão não pode conter espaço, '=' ou ':'.")
        if not endpoint:
            raise ValueError("Informe o endpoint, comando ou path da conexão MCP.")

        if transport == "http":
            if not endpoint.startswith(("http://", "https://")):
                raise ValueError("Transporte HTTP exige endpoint http:// ou https://.")
            spec = f"{name}={endpoint}"
        elif transport in {"remote", "stdio", "socket"}:
            spec = f"{name}={transport}:{endpoint}"
        else:
            raise ValueError("Selecione um transporte MCP válido.")

        # O campo nasce com o ambiente persistido; o que está nele ao salvar é o
        # que fica gravado, inclusive sob o novo nome quando a conexão é
        # renomeada. Vazio remove as variáveis da conexão.
        env_text = self.query_one("#mcp_editor_env", Input).value.strip()
        malformed = [
            part
            for part in (item.strip() for item in env_text.split(","))
            if part and ("=" not in part or not part.split("=", 1)[0].strip())
        ]
        if malformed:
            raise ValueError(
                "Ambiente deve ser KEY=valor,KEY2=valor2; inválido: " + ", ".join(malformed)
            )
        env_spec = f"{name}={env_text}" if env_text else ""
        return spec, env_spec, name, scope

    def _save_failed(self, name: str, error: str) -> None:
        self._busy = False
        message = f"Falha ao salvar {name}: {error}"
        self.query_one("#mcp_editor_activity", Label).update(message)
        self.parent_app.notify(message, severity="error")

    def _save_succeeded(self, name: str, scope: str, info) -> None:
        self._busy = False
        where = "no catálogo global" if scope == SCOPE_GLOBAL else "neste workspace"
        if not getattr(info, "active", True):
            state = (
                "continua inativo neste workspace"
                if self.info is not None
                else "inativo até Ativar, aqui ou em todos os workspaces"
            )
        elif getattr(info, "in_progress", False):
            state = (
                "reconectando em background"
                if self.info is not None
                else "conectando em background"
            )
        else:
            state = "conexão ativa mantida"
        self.parent_app.notify(f"MCP '{name}' salvo {where} · {state}.", severity="information")
        self.dismiss(name)


class MCPHttpSettingsEditorScreen(ModalScreen[MCPHttpSettings | None]):
    """Editor de host, porta e allowlist do servidor MCP HTTP, por escopo."""

    CSS = """
    MCPHttpSettingsEditorScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }
    #mcp_http_editor_dialog {
        width: 72;
        height: 90%;
        max-height: 24;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    #mcp_http_editor_header {
        height: 1;
        margin-bottom: 1;
    }
    #mcp_http_editor_title {
        width: 1fr;
        content-align: center middle;
        text-style: bold;
        color: $accent;
    }
    #mcp_http_editor_close {
        width: 3;
        min-width: 3;
        height: 1;
        border: none;
        padding: 0;
    }
    #mcp_http_editor_fields {
        height: 1fr;
        min-height: 3;
        scrollbar-gutter: stable;
    }
    #mcp_http_editor_fields Label {
        margin-top: 1;
        color: $text-muted;
    }
    #mcp_http_editor_fields Input, #mcp_http_editor_fields Select {
        width: 100%;
    }
    #mcp_http_editor_buttons {
        height: 1;
        margin-top: 1;
        align-horizontal: right;
    }
    #mcp_http_editor_buttons Button {
        height: 1;
        min-width: 14;
        border: none;
        margin-left: 1;
    }
    #mcp_http_editor_activity {
        height: 1;
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancelar"),
        ("ctrl+s", "save", "Salvar"),
    ]

    AUTO_FOCUS = "#mcp_http_editor_port"

    _HINT = "Ctrl+S salva · Esc cancela"

    def __init__(self, store: MCPHttpSettingsStore, parent_app) -> None:
        super().__init__()
        self.store = store
        self.parent_app = parent_app

    def compose(self) -> ComposeResult:
        settings = self.store.load()
        sources = self.store.sources()
        with Container(id="mcp_http_editor_dialog"):
            with Horizontal(id="mcp_http_editor_header"):
                yield Label("Servidor MCP HTTP", id="mcp_http_editor_title")
                yield Button("×", id="mcp_http_editor_close")
            with VerticalScroll(id="mcp_http_editor_fields"):
                yield Label(f"Host · origem: {sources['host']}", id="mcp_http_editor_host_label")
                yield Input(value=settings.host, placeholder="127.0.0.1", id="mcp_http_editor_host")
                yield Label(f"Porta · origem: {sources['port']}", id="mcp_http_editor_port_label")
                yield Input(value=str(settings.port), placeholder="9090", id="mcp_http_editor_port")
                yield Label(
                    f"Tools · read-local, read, agent, all ou CSV · origem: {sources['allow_tools']}",
                    id="mcp_http_editor_tools_label",
                )
                yield Input(
                    value=settings.allow_tools,
                    placeholder="read",
                    id="mcp_http_editor_allow_tools",
                )
                yield Label("Escopo · onde gravar estes valores")
                yield Select(
                    SCOPE_OPTIONS,
                    value=SCOPE_LOCAL,
                    allow_blank=False,
                    id="mcp_http_editor_scope",
                )
            with Horizontal(id="mcp_http_editor_buttons"):
                yield Button("Usar global", id="mcp_http_editor_inherit")
                yield Button("Cancelar", id="mcp_http_editor_cancel")
                yield Button("Salvar", id="mcp_http_editor_save", variant="primary")
            yield Label(self._HINT, id="mcp_http_editor_activity")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in {"mcp_http_editor_close", "mcp_http_editor_cancel"}:
            self.action_cancel()
        elif event.button.id == "mcp_http_editor_save":
            self.action_save()
        elif event.button.id == "mcp_http_editor_inherit":
            try:
                settings = self.store.save(SCOPE_LOCAL, host=None, port=None, allow_tools=None)
            except (ValueError, OSError) as exc:
                self.parent_app.notify(str(exc), severity="error")
                return
            self.parent_app.notify("Servidor MCP HTTP: usando os padrões globais neste workspace.")
            self.dismiss(settings)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "mcp_http_editor_scope" or not self.is_mounted:
            return
        global_scope = event.value == SCOPE_GLOBAL
        settings = (
            MCPHttpSettings(**self.store.layer(SCOPE_GLOBAL))
            if global_scope
            else self.store.load()
        )
        self.query_one("#mcp_http_editor_host", Input).value = settings.host
        self.query_one("#mcp_http_editor_port", Input).value = str(settings.port)
        self.query_one("#mcp_http_editor_allow_tools", Input).value = settings.allow_tools
        self.query_one("#mcp_http_editor_inherit", Button).disabled = global_scope
        sources = (
            {key: "global" if key in self.store.layer(SCOPE_GLOBAL) else "default" for key in ("host", "port", "allow_tools")}
            if global_scope
            else self.store.sources()
        )
        self.query_one("#mcp_http_editor_host_label", Label).update(f"Host · origem: {sources['host']}")
        self.query_one("#mcp_http_editor_port_label", Label).update(f"Porta · origem: {sources['port']}")
        self.query_one("#mcp_http_editor_tools_label", Label).update(
            f"Tools · read-local, read, agent, all ou CSV · origem: {sources['allow_tools']}"
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        host = self.query_one("#mcp_http_editor_host", Input).value.strip()
        port_text = self.query_one("#mcp_http_editor_port", Input).value.strip()
        allow_tools = self.query_one("#mcp_http_editor_allow_tools", Input).value.strip()
        scope = str(self.query_one("#mcp_http_editor_scope", Select).value or "").strip()
        try:
            port = int(port_text)
        except ValueError:
            self.parent_app.notify("Porta deve ser um número inteiro.", severity="error")
            return
        try:
            settings = self.store.save(scope, host=host, port=port, allow_tools=allow_tools)
        except (ValueError, OSError) as exc:
            self.parent_app.notify(str(exc), severity="error")
            return
        where = "no global" if scope == SCOPE_GLOBAL else "neste workspace"
        local_overrides = scope == SCOPE_GLOBAL and any(
            self.store.sources()[key] == SCOPE_LOCAL for key in ("host", "port", "allow_tools")
        )
        suffix = " · valores locais continuam valendo aqui" if local_overrides else ""
        self.parent_app.notify(
            f"Servidor MCP HTTP: {settings.url} · tools {settings.allow_tools} · gravado {where}{suffix}.",
            severity="information",
        )
        self.dismiss(settings)


class MCPConnectionsScreen(ModalScreen[None]):
    """Exibe os papéis MCP da sessão e gerencia servidores MCP de saída."""

    CSS = """
    MCPConnectionsScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }
    #mcp_dialog {
        width: 96%;
        max-width: 104;
        height: 100%;
        max-height: 28;
        background: $surface;
        border: round $primary;
        padding: 0 1;
    }
    #mcp_header {
        height: 1;
        margin-bottom: 1;
    }
    #mcp_title {
        width: 1fr;
        content-align: center middle;
        text-style: bold;
        color: $accent;
    }
    #mcp_close_top {
        width: 3;
        min-width: 3;
        height: 1;
        border: none;
        padding: 0;
    }
    #mcp_roles {
        height: 2;
        margin-bottom: 1;
    }
    .mcp_role_state {
        color: $text-muted;
    }
    #mcp_http_row {
        height: 1;
    }
    #mcp_http_state {
        width: 1fr;
    }
    #mcp_http_row Button {
        height: 1;
        min-width: 11;
        border: none;
        padding: 0 1;
        margin-left: 1;
    }
    #mcp_tabs {
        height: 1;
        margin-bottom: 1;
    }
    #mcp_tabs Button {
        height: 1;
        min-width: 16;
        border: none;
        margin-right: 1;
    }
    #mcp_clients_panel, #mcp_servers_panel {
        height: 1fr;
    }
    #mcp_incoming_header, #mcp_clients_header {
        height: 1;
    }
    #mcp_incoming_title, #mcp_clients_title {
        width: 1fr;
        text-style: bold;
        color: $text;
    }
    #mcp_incoming_summary, #mcp_clients_summary {
        width: auto;
        color: $text-muted;
        text-align: right;
    }
    #mcp_incoming_table {
        height: 1fr;
        margin-top: 1;
    }
    #mcp_incoming_actions {
        height: 2;
        margin-top: 1;
    }
    #mcp_incoming_actions Button {
        height: 1;
        min-width: 12;
        border: none;
        margin-right: 1;
    }
    #mcp_table {
        height: 1fr;
        margin-top: 1;
    }
    #mcp_actions {
        layout: grid;
        grid-size: 3 2;
        grid-rows: 1;
        grid-gutter: 0 1;
        height: 2;
        margin-top: 1;
    }
    #mcp_actions Button {
        width: 100%;
        height: 1;
        min-width: 12;
        border: none;
    }
    #mcp_auth_row {
        height: 1;
        margin-top: 1;
    }
    #mcp_auth_row Button {
        height: 1;
        min-width: 12;
        border: none;
        margin-right: 1;
    }
    #mcp_auth_label {
        width: 1fr;
        color: $warning;
    }
    #mcp_activity {
        height: 1;
        color: $text-muted;
        margin-top: 1;
    }
    .hidden { display: none; }
    """

    BINDINGS = [
        ("escape", "close", "Fechar"),
        ("ctrl+n", "new_connection", "Adicionar servidor"),
        ("ctrl+e", "edit_connection", "Editar servidor"),
        ("ctrl+t", "toggle_active", "Ativar/desativar"),
        ("ctrl+r", "reconnect", "Reconectar"),
        ("ctrl+o", "authorize", "Autorizar no navegador"),
    ]

    AUTO_FOCUS = "#mcp_incoming_table"

    def __init__(self, quimera_app, parent_app) -> None:
        super().__init__()
        self.quimera_app = quimera_app
        self.parent_app = parent_app
        self.manager = MCPConnectionManager.from_app(quimera_app)
        self._selected_name: str | None = None
        self._selected_client: str | None = None
        self._client_groups: dict[str, MCPClientGroup] = {}
        self._active_tab = "clients"
        self._busy = False
        self._unsubscribe_bridge = None
        self._ui_thread_id: int | None = None
        # Conexões iniciadas por este hub; avisadas ao terminar o handshake.
        self._pending_connections: set[str] = set()
        # Remoção de uma conexão global exige dois toques em Remover.
        self._confirm_remove: str | None = None

    def compose(self) -> ComposeResult:
        with Container(id="mcp_dialog"):
            with Horizontal(id="mcp_header"):
                yield Label("MCP Hub", id="mcp_title")
                yield Button("×", id="mcp_close_top")

            with Vertical(id="mcp_roles"):
                yield Label("", id="mcp_socket_state", classes="mcp_role_state")
                with Horizontal(id="mcp_http_row"):
                    yield Label("", id="mcp_http_state", classes="mcp_role_state")
                    yield Button("Ativar", id="mcp_server_toggle")
                    yield Button("Configurar", id="mcp_server_configure")

            with Horizontal(id="mcp_tabs"):
                yield Button("Clientes", id="mcp_tab_clients", variant="primary")
                yield Button("Servidores", id="mcp_tab_servers")

            with Vertical(id="mcp_clients_panel"):
                with Horizontal(id="mcp_incoming_header"):
                    yield Label("Clientes autorizados", id="mcp_incoming_title")
                    yield Label("", id="mcp_incoming_summary")
                yield DataTable(
                    id="mcp_incoming_table",
                    cursor_type="row",
                    zebra_stripes=True,
                )
                with Horizontal(id="mcp_incoming_actions"):
                    yield Button("Ativar", id="mcp_client_toggle", variant="primary")
                    yield Button(
                        "Revogar autorização",
                        id="mcp_revoke_client",
                        variant="error",
                    )

            with Vertical(id="mcp_servers_panel", classes="hidden"):
                with Horizontal(id="mcp_clients_header"):
                    yield Label("Servidores conectados", id="mcp_clients_title")
                    yield Label("", id="mcp_clients_summary")
                yield DataTable(id="mcp_table", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="mcp_actions"):
                    yield Button("Adicionar", id="mcp_new", variant="primary")
                    yield Button("Editar", id="mcp_edit")
                    yield Button("Ativar", id="mcp_toggle")
                    yield Button("Reconectar", id="mcp_reconnect")
                    yield Button("Desconectar", id="mcp_disconnect")
                    yield Button("Remover", id="mcp_remove", variant="error")
                with Horizontal(id="mcp_auth_row", classes="hidden"):
                    yield Button(
                        "Autorizar no navegador", id="mcp_authorize", variant="warning"
                    )
                    yield Button("Copiar link", id="mcp_copy_auth")
                    yield Label("", id="mcp_auth_label")
            yield Label("", id="mcp_activity")

    def on_mount(self) -> None:
        incoming = self.query_one("#mcp_incoming_table", DataTable)
        # "Escopo OAuth" são os scopes do token; "Escopo" é quem decide a
        # liberação (padrão global ou exceção deste workspace). O cadastro em
        # si é sempre global.
        incoming.add_columns("Cliente", "Transporte", "Escopo OAuth", "Estado", "Escopo")
        table = self.query_one("#mcp_table", DataTable)
        # Mantém a ordem histórica das quatro primeiras colunas para que
        # extensões/fixtures legadas continuem lendo o estado em ``[3]``;
        # o escopo fica visível como a última coluna.
        table.add_columns("Nome", "Transporte", "Endpoint", "Estado", "Escopo")
        self._refresh_view()
        self._update_action_state()
        self._update_client_action_state()
        # Conexões iniciadas em background podem terminar com o hub aberto:
        # acompanha o bridge para a tabela refletir cada transição.
        self._ui_thread_id = threading.get_ident()
        subscribe = getattr(self.manager, "subscribe", None)
        if callable(subscribe):
            self._unsubscribe_bridge = subscribe(self._on_bridge_state_changed)

    def on_unmount(self) -> None:
        unsubscribe = self._unsubscribe_bridge
        self._unsubscribe_bridge = None
        if callable(unsubscribe):
            unsubscribe()

    def _on_bridge_state_changed(self) -> None:
        if threading.get_ident() == self._ui_thread_id:
            self._refresh_connections_safely()
            return
        try:
            self.app.call_from_thread(self._refresh_connections_safely)
        except RuntimeError:
            # App já encerrado ou callback fora do loop Textual.
            return

    def _refresh_connections_safely(self) -> None:
        if not self.is_mounted:
            return
        try:
            self._refresh_connections()
        except Exception:
            return

    def _refresh_view(self) -> None:
        self._refresh_roles()
        self._refresh_incoming_clients()
        self._refresh_connections()

    def _refresh_roles(self) -> None:
        socket_path = str(getattr(self.quimera_app, "mcp_socket_path", "") or "")
        self.query_one("#mcp_socket_state", Label).update(
            self._status_text("Socket local", socket_path)
        )
        self.query_one("#mcp_http_state", Label).update(self._http_status_text())
        toggle = self.query_one("#mcp_server_toggle", Button)
        # O botão reflete a escolha persistida ("sobe com este workspace"),
        # não só o processo vivo: um servidor que falhou na partida continua
        # configurado para subir, e Desativar é o que impede a próxima tentativa.
        toggle.label = "Desativar" if self._http_server_enabled() else "Ativar"
        toggle.disabled = self._busy
        self.query_one("#mcp_server_configure", Button).disabled = self._busy

    def _http_store(self) -> MCPHttpSettingsStore:
        return MCPHttpSettingsStore.from_workspace(getattr(self.quimera_app, "workspace"))

    def _http_server_running(self) -> bool:
        return getattr(self.quimera_app, "external_mcp_http_server", None) is not None

    def _http_server_enabled(self) -> bool:
        """Servidor de pé nesta sessão ou configurado para subir com o workspace."""
        return self._http_server_running() or self._http_store().load().enabled

    def _http_status_text(self) -> Text:
        """Linha do servidor HTTP: URL viva, falha de partida ou configuração em espera."""
        text = Text("MCP HTTP OAuth · ")
        http_url = str(getattr(self.quimera_app, "mcp_http_url", "") or "")
        error = str(getattr(self.quimera_app, "external_mcp_http_error", "") or "")
        store = self._http_store()
        settings = store.load()
        sources = store.sources()
        if http_url:
            settings = getattr(self.quimera_app, "mcp_http_settings", None) or settings
            text.append("●", style="green")
            text.append(f" ativo · {http_url} · tools {settings.allow_tools}")
            return text
        if error:
            text.append("●", style="red")
            text.append(f" falha · {error}")
            if settings.enabled:
                text.append(" · Configurar ajusta a porta; Desativar não sobe mais")
            return text
        text.append("○ inativo")
        text.append(
            f" · porta {settings.port} ({sources['port']}) · tools {settings.allow_tools}"
        )
        return text

    @staticmethod
    def _status_text(label: str, endpoint: str) -> Text:
        text = Text(f"{label} · ")
        if endpoint:
            text.append("●", style="green")
            text.append(f" ativo · {endpoint}")
        else:
            text.append("○ inativo")
        return text

    def _refresh_incoming_clients(self) -> None:
        table = self.query_one("#mcp_incoming_table", DataTable)
        table.clear()
        http_server = getattr(self.quimera_app, "external_mcp_http_server", None)
        if http_server is None:
            workspace = getattr(self.quimera_app, "workspace", None)
            clients = (
                load_persisted_authorized_clients(workspace)
                if workspace is not None
                else []
            )
        else:
            provider = getattr(http_server, "known_clients", None)
            clients = provider() if callable(provider) else http_server.connected_clients()
        groups = group_clients_by_name(clients, self._http_store().client_decision)
        self._client_groups = {group.name: group for group in groups}
        authorized_count = sum(1 for group in groups if group.authorized)
        active_count = sum(1 for group in groups if group.authorized and group.active)
        self.query_one("#mcp_incoming_summary", Label).update(
            f"{authorized_count} autorizados · {active_count} ativos aqui"
        )
        selected_row: int | None = None
        for index, group in enumerate(groups):
            table.add_row(
                group.label,
                "http/oauth",
                group.scope,
                self._client_state_cell(group),
                group.activation_scope,
                key=group.name,
            )
            if group.name == self._selected_client:
                selected_row = index
        if self._selected_client and selected_row is None:
            self._selected_client = None
        elif selected_row is not None and table.cursor_row != selected_row:
            table.move_cursor(row=selected_row, animate=False)

    @staticmethod
    def _client_state_cell(group: MCPClientGroup) -> Text:
        label = group.state_label
        if not group.authorized:
            return Text(label, style="red")
        if not group.active:
            return Text(label, style="dim")
        if group.connected:
            return Text(label, style="green")
        return Text(label)

    def _selected_client_group(self) -> MCPClientGroup | None:
        return self._client_groups.get(self._selected_client) if self._selected_client else None

    def _refresh_connections(self) -> None:
        table = self.query_one("#mcp_table", DataTable)
        table.clear()
        connections = self.manager.list_connections()
        connected = sum(1 for item in connections if item.connected)
        in_progress = sum(1 for item in connections if item.in_progress)
        schemas = get_bridge_schemas()
        summary = f"{connected}/{len(connections)} conectados · {len(schemas)} tools"
        if in_progress:
            summary = f"{summary} · {in_progress} conectando"
        self.query_one("#mcp_clients_summary", Label).update(summary)
        selected_row: int | None = None
        for index, info in enumerate(connections):
            table.add_row(
                info.name,
                info.transport,
                self._short_endpoint(info.endpoint),
                self._state_cell(info),
                self._scope_cell(info),
                key=info.name,
            )
            if info.name == self._selected_name:
                selected_row = index
        if self._selected_name and selected_row is None:
            self._selected_name = None
        elif selected_row is not None and table.cursor_row != selected_row:
            # A tabela é reconstruída a cada transição do bridge; sem isso o
            # cursor voltaria à primeira linha e trocaria a seleção.
            table.move_cursor(row=selected_row, animate=False)
        self._update_action_state()
        self._check_pending_connections(connections)

    def _check_pending_connections(self, connections: list[MCPConnectionInfo]) -> None:
        """Avisa o resultado das conexões iniciadas por este hub."""
        if not self._pending_connections:
            return
        by_name = {item.name: item for item in connections}
        for name in list(self._pending_connections):
            info = by_name.get(name)
            if info is not None and info.in_progress:
                continue
            self._pending_connections.discard(name)
            if info is None:
                continue
            if info.established:
                message = f"MCP '{name}' conectado · {info.tools} tool(s)."
                self._set_activity(message)
                self.parent_app.notify(message, severity="information")
            elif info.failed:
                # O bloco de status do chat já emitiu o toast da falha.
                self._set_activity(f"Falha ao conectar {name}: {info.detail}")

    @staticmethod
    def _short_endpoint(endpoint: str, limit: int = 48) -> str:
        if len(endpoint) <= limit:
            return endpoint
        return endpoint[: limit - 1] + "…"

    @staticmethod
    def _scope_cell(info: MCPConnectionInfo) -> str:
        """Onde a configuração vive; uma global com decisão própria deste workspace é marcada."""
        if info.is_global and info.activation_scope == SCOPE_LOCAL:
            return "global (exceção local)"
        return info.scope

    @staticmethod
    def _state_cell(info: MCPConnectionInfo) -> Text:
        label = info.state_label
        if not info.active:
            return Text(label, style="dim")
        if info.failed:
            return Text(label, style="red")
        if info.in_progress or info.auth_pending:
            return Text(label, style="yellow")
        if info.established:
            return Text(label, style="green")
        return Text(label, style="dim")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Mantém o estado das ações alinhado à linha visualmente destacada."""
        if event.data_table.id == "mcp_incoming_table":
            name = str(event.row_key.value or "")
            self._selected_client = name or None
            self._update_client_action_state()
            group = self._selected_client_group()
            if group is None:
                return
            registrations = (
                f" · {len(group.client_ids)} cadastros OAuth" if len(group.client_ids) > 1 else ""
            )
            if not group.authorized:
                self._set_activity(f"{group.name}: sem autorização válida{registrations}")
                return
            local = group.activation_scope == SCOPE_LOCAL
            if group.active:
                origin = (
                    "liberado por decisão deste workspace"
                    if local
                    else "liberado em todos os workspaces (padrão global)"
                )
                self._set_activity(f"{group.name}: {origin}{registrations}")
            else:
                origin = (
                    "bloqueado por decisão deste workspace"
                    if local
                    else "inativo · o padrão global não o libera"
                )
                self._set_activity(
                    f"{group.name}: {origin} · Ativar pergunta o escopo{registrations}"
                )
            return
        if event.data_table.id != "mcp_table":
            return
        name = str(event.row_key.value or "")
        if not name:
            return
        if name != self._selected_name:
            self._confirm_remove = None
        self._selected_name = name
        self._update_action_state()
        info = self._find_info(name)
        if info is not None and not info.active:
            hint = "Ativar pergunta o escopo" if info.is_global else "Ativar religa aqui"
            self._set_activity(f"{name}: {self._activation_note(info)} · {hint}.")
            return
        if info is not None and info.auth_pending:
            self._set_activity(f"{name}: autorização pendente · {info.auth_url}")
            return
        if info is not None and info.detail and (info.failed or info.in_progress):
            self._set_activity(f"{name}: {info.detail}")
            return
        if info is not None:
            self._set_activity(f"{name}: {self._activation_note(info)}")
            return
        self._set_activity(f"Servidor selecionado: {name}")

    @staticmethod
    def _activation_note(info: MCPConnectionInfo) -> str:
        """Quem decidiu o estado de uma conexão, em linguagem de usuário."""
        if not info.is_global:
            return (
                "conexão local · ativa só neste workspace"
                if info.active
                else "conexão local · desligada neste workspace"
            )
        if info.activation_scope == SCOPE_GLOBAL:
            return (
                "ativo em todos os workspaces (padrão global)"
                if info.active
                else "inativo · o padrão global não o liga"
            )
        if info.active:
            return (
                "ativo por decisão deste workspace · o padrão global também o liga"
                if info.default_active
                else "ativo só neste workspace · o padrão global não o liga"
            )
        return (
            "desligado só neste workspace · o padrão global o liga"
            if info.default_active
            else "desligado por decisão deste workspace · o padrão global também não o liga"
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Ativação explícita reaproveita a mesma semântica do destaque visual."""
        highlighted = DataTable.RowHighlighted(
            event.data_table,
            event.cursor_row,
            event.row_key,
        )
        self.on_data_table_row_highlighted(highlighted)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "mcp_close_top":
            self.action_close()
        elif button_id == "mcp_tab_clients":
            self._show_tab("clients")
        elif button_id == "mcp_tab_servers":
            self._show_tab("servers")
        elif button_id == "mcp_new":
            self.action_new_connection()
        elif button_id == "mcp_edit":
            self.action_edit_connection()
        elif button_id == "mcp_toggle":
            self.action_toggle_active()
        elif button_id == "mcp_server_toggle":
            self.action_toggle_server()
        elif button_id == "mcp_server_configure":
            self.action_configure_server()
        elif button_id == "mcp_reconnect":
            self.action_reconnect()
        elif button_id == "mcp_authorize":
            self.action_authorize()
        elif button_id == "mcp_copy_auth":
            self._copy_authorization_link()
        elif button_id == "mcp_disconnect":
            self._disconnect_selected()
        elif button_id == "mcp_remove":
            self._remove_selected()
        elif button_id == "mcp_client_toggle":
            self.action_toggle_client()
        elif button_id == "mcp_revoke_client":
            self._revoke_selected_client()

    def _show_tab(self, tab: str) -> None:
        if tab not in {"clients", "servers"}:
            return
        self._active_tab = tab
        clients_active = tab == "clients"
        self.query_one("#mcp_clients_panel").set_class(not clients_active, "hidden")
        self.query_one("#mcp_servers_panel").set_class(clients_active, "hidden")
        self.query_one("#mcp_tab_clients", Button).variant = (
            "primary" if clients_active else "default"
        )
        self.query_one("#mcp_tab_servers", Button).variant = (
            "default" if clients_active else "primary"
        )
        target = "#mcp_incoming_table" if clients_active else "#mcp_table"
        self.query_one(target, DataTable).focus()
        self._set_activity(
            "Ativar libera o client neste workspace · Revogar apaga a autorização global."
            if clients_active
            else "Ativar liga uma conexão do catálogo neste workspace · Remover apaga a definição."
        )

    def action_close(self) -> None:
        if not self._busy:
            self.dismiss()

    def action_new_connection(self) -> None:
        if self._busy:
            return
        self.app.push_screen(
            MCPServerEditorScreen(self.manager, self.parent_app),
            self._on_editor_closed,
        )

    def action_edit_connection(self) -> None:
        if self._busy:
            return
        info = self._selected_info()
        if info is None:
            return
        self.app.push_screen(
            MCPServerEditorScreen(self.manager, self.parent_app, info),
            self._on_editor_closed,
        )

    def _on_editor_closed(self, name: str | None) -> None:
        if not name:
            self._refresh_view()
            return
        self._selected_name = name
        info = self._find_info(name)
        if info is not None and info.active and info.in_progress:
            # Ativo e com configuração nova: o manager refez o handshake.
            self._pending_connections.add(name)
        self._refresh_view()
        if info is None:
            self._set_activity(f"Servidor '{name}' salvo.")
        elif info.active and info.in_progress:
            self._set_activity(f"Servidor '{name}' salvo · conectando em background.")
        elif info.active:
            self._set_activity(f"Servidor '{name}' salvo · {self._activation_note(info)}.")
        else:
            hint = "Ativar pergunta o escopo" if info.is_global else "Ativar religa aqui"
            self._set_activity(f"Servidor '{name}' salvo · {self._activation_note(info)} · {hint}.")

    def _ask_scope(self, title: str, detail: str, apply) -> None:
        """Pergunta o escopo de uma ativação e chama ``apply(scope)`` se não for cancelada."""

        def chosen(scope: str | None) -> None:
            if scope in (SCOPE_LOCAL, SCOPE_GLOBAL) and not self._busy:
                apply(scope)

        self.app.push_screen(MCPScopePromptScreen(title, detail), chosen)

    def action_toggle_active(self) -> None:
        """Liga ou desliga o item selecionado na aba ativa, no escopo escolhido."""
        if self._busy:
            return
        if self._active_tab == "clients":
            self.action_toggle_client()
            return
        name = self._require_selected()
        if name is None:
            return
        info = self._find_info(name)
        if info is None:
            return
        enable = not info.active
        verb = "Ativar" if enable else "Desativar"

        def apply(scope: str) -> None:
            where = "em todos os workspaces" if scope == SCOPE_GLOBAL else "neste workspace"
            self._start_busy(f"{'Ativando' if enable else 'Desativando'} {name} {where}…")
            self._toggle_worker(name, enable, scope)

        if not info.is_global:
            # Conexão local não tem padrão global: a decisão é sempre deste
            # workspace, sem perguntar o escopo.
            apply(SCOPE_LOCAL)
            return
        self._ask_scope(
            f"{verb} '{name}'",
            "Neste workspace: exceção só daqui; o padrão global não a desfaz.\n"
            "Em todos: muda o padrão global e este workspace volta a segui-lo; "
            "os já abertos aplicam ao reiniciar.",
            apply,
        )

    @work(thread=True, exclusive=True, group="mcp-management")
    def _toggle_worker(self, name: str, enable: bool, scope: str) -> None:
        try:
            self.manager.set_active(name, enable, scope)
        except Exception as exc:
            verb = "ativar" if enable else "desativar"
            self.app.call_from_thread(
                self._finish_busy, f"Falha ao {verb} {name}: {exc}", "error", name
            )
            return
        self.app.call_from_thread(self._finish_toggle, name, enable, scope)

    def _finish_toggle(self, name: str, enable: bool, scope: str) -> None:
        where = "em todos os workspaces" if scope == SCOPE_GLOBAL else "neste workspace"
        if enable:
            self._pending_connections.add(name)
            message = f"MCP '{name}' ativado {where} · conectando em background."
        else:
            message = f"MCP '{name}' desativado {where}."
        if scope == SCOPE_GLOBAL:
            message = f"{message} Os outros workspaces abertos aplicam ao reiniciar."
        self._finish_busy(message, "information", name)

    def action_toggle_server(self) -> None:
        """Sobe ou derruba o servidor MCP HTTP e persiste a escolha no workspace."""
        if self._busy:
            return
        enable = not self._http_server_enabled()
        self._start_busy(
            "Ativando servidor MCP HTTP…" if enable else "Desativando servidor MCP HTTP…"
        )
        self._server_worker(enable)

    @work(thread=True, exclusive=True, group="mcp-management")
    def _server_worker(self, enable: bool) -> None:
        store = self._http_store()
        try:
            if enable:
                settings = replace(store.load(), enabled=True)
                start_external_mcp_http(
                    self.quimera_app, getattr(self.quimera_app, "workspace"), settings
                )
                try:
                    store.save(SCOPE_LOCAL, enabled=True)
                except Exception:
                    stop_external_mcp_http(self.quimera_app)
                    raise
                message = (
                    f"Servidor MCP HTTP ativo em {settings.url} · "
                    "sobe junto com este workspace a partir de agora."
                )
            else:
                was_running = stop_external_mcp_http(self.quimera_app)
                store.save(SCOPE_LOCAL, enabled=False)
                message = (
                    "Servidor MCP HTTP desativado · não sobe mais com este workspace."
                    if was_running
                    else "Servidor MCP HTTP não sobe mais com este workspace."
                )
        except Exception as exc:
            verb = "ativar" if enable else "desativar"
            self.app.call_from_thread(
                self._finish_busy,
                f"Falha ao {verb} o servidor MCP HTTP: {describe_http_error(exc)}",
                "error",
                self._selected_name,
            )
            return
        self.app.call_from_thread(self._finish_busy, message, "information", self._selected_name)

    def action_configure_server(self) -> None:
        if self._busy:
            return
        self.app.push_screen(
            MCPHttpSettingsEditorScreen(self._http_store(), self.parent_app),
            self._on_http_editor_closed,
        )

    def _on_http_editor_closed(self, settings: MCPHttpSettings | None) -> None:
        """Aplica a configuração salva ao estado já decidido; nunca decide ativação.

        Com o servidor de pé, reinicia só se host, porta ou tools efetivos
        mudaram. Ativado neste workspace mas parado (porta ocupada na partida,
        por exemplo), tenta subir com os valores novos: a ativação já era a
        escolha do workspace. Desativado, apenas grava.
        """
        if settings is None:
            return
        if self._http_server_running():
            live = getattr(self.quimera_app, "mcp_http_settings", None)
            effective = (settings.host, settings.port, settings.allow_tools)
            if live is not None and (live.host, live.port, live.allow_tools) == effective:
                self._refresh_roles()
                self._set_activity(
                    f"Servidor MCP HTTP: configuração salva · segue em {settings.url} sem mudança efetiva."
                )
                return
            self._start_busy(f"Reiniciando servidor MCP HTTP em {settings.url}…")
            self._restart_server_worker(settings)
            return
        if settings.enabled:
            self._start_busy(f"Subindo servidor MCP HTTP em {settings.url}…")
            self._restart_server_worker(settings)
            return
        self.quimera_app.external_mcp_http_error = ""
        self._refresh_roles()
        self._set_activity(
            f"Servidor MCP HTTP configurado para {settings.url} · Ativar sobe com estes valores."
        )

    @work(thread=True, exclusive=True, group="mcp-management")
    def _restart_server_worker(self, settings: MCPHttpSettings) -> None:
        was_running = False
        try:
            was_running = stop_external_mcp_http(self.quimera_app)
            start_external_mcp_http(
                self.quimera_app, getattr(self.quimera_app, "workspace"), settings
            )
        except Exception as exc:
            prefix = "Servidor MCP HTTP parado; falha" if was_running else "Falha"
            self.app.call_from_thread(
                self._finish_busy,
                f"{prefix} ao subir em {settings.url}: {describe_http_error(exc)}",
                "error",
                self._selected_name,
            )
            return
        verb = "reiniciado" if was_running else "ativo"
        self.app.call_from_thread(
            self._finish_busy,
            f"Servidor MCP HTTP {verb} em {settings.url}.",
            "information",
            self._selected_name,
        )

    def action_reconnect(self) -> None:
        """Refaz o handshake em background; o hub continua utilizável.

        Um handshake anterior ainda preso (ex.: OAuth não concluído) é
        abandonado pelo bridge. A tabela acompanha as transições e o resultado
        vira toast quando chega.
        """
        if self._busy:
            return
        name = self._require_selected()
        if name is None:
            return
        try:
            self.manager.reconnect_in_background(name)
        except Exception as exc:
            message = f"Falha ao reconectar {name}: {exc}"
            self._set_activity(message)
            self.parent_app.notify(message, severity="error")
            return
        self._pending_connections.add(name)
        self._refresh_connections()
        self._set_activity(f"Reconectando {name}… o hub pode ser fechado enquanto isso.")

    def _selected_auth_url(self) -> tuple[str, str] | None:
        """``(nome, url)`` da autorização pendente da linha selecionada."""
        info = self._selected_info()
        if info is None or not info.auth_pending:
            return None
        return info.name, info.auth_url

    def action_authorize(self) -> None:
        """Abre a URL de autorização OAuth da conexão selecionada no navegador."""
        pending = self._selected_auth_url()
        if pending is None:
            self.parent_app.notify(
                "A conexão selecionada não tem autorização pendente.", severity="warning"
            )
            return
        name, url = pending
        if open_in_browser(url):
            message = f"Autorização de '{name}' aberta no navegador."
            self._set_activity(message)
            self.parent_app.notify(message, severity="information")
            return
        self._copy_authorization_link(
            prefix="Navegador indisponível; ", severity="warning"
        )

    def _copy_authorization_link(
        self, *, prefix: str = "", severity: str = "information"
    ) -> None:
        """Copia a URL de autorização via OSC 52 (depende do terminal)."""
        pending = self._selected_auth_url()
        if pending is None:
            self.parent_app.notify(
                "A conexão selecionada não tem autorização pendente.", severity="warning"
            )
            return
        name, url = pending
        try:
            self.app.copy_to_clipboard(url)
        except Exception:
            pass
        message = (
            f"{prefix}link de autorização de '{name}' copiado para a área de "
            "transferência (se o terminal suportar OSC 52)."
        )
        self._set_activity(f"{name}: autorização pendente · {url}")
        self.parent_app.notify(message, severity=severity)

    def _disconnect_selected(self) -> None:
        if self._busy:
            return
        name = self._require_selected()
        if name is None:
            return
        self._start_busy(f"Desconectando {name}…")
        self._disconnect_worker(name)

    def _remove_selected(self) -> None:
        if self._busy:
            return
        name = self._require_selected()
        if name is None:
            return
        info = self._find_info(name)
        if info is not None and info.is_global and self._confirm_remove != name:
            self._confirm_remove = name
            message = (
                f"'{name}' é global: Remover de novo apaga do catálogo e de todos os workspaces "
                "(Desativar só desliga aqui)."
            )
            self._set_activity(message)
            self.parent_app.notify(message, severity="warning")
            return
        self._confirm_remove = None
        self._start_busy(f"Removendo {name}…")
        self._remove_worker(name)

    def _selected_info(self) -> MCPConnectionInfo | None:
        name = self._require_selected()
        if name is None:
            return None
        return self._find_info(name)

    def _find_info(self, name: str) -> MCPConnectionInfo | None:
        return next(
            (item for item in self.manager.list_connections() if item.name == name),
            None,
        )

    def _require_selected(self) -> str | None:
        if self._selected_name:
            return self._selected_name
        self.parent_app.notify("Selecione um servidor MCP primeiro.", severity="warning")
        return None

    def _update_action_state(self) -> None:
        info = self._find_info(self._selected_name) if self._selected_name else None
        busy_or_empty = self._busy or info is None
        for widget_id in ("#mcp_edit", "#mcp_remove"):
            self.query_one(widget_id, Button).disabled = busy_or_empty
        # Reconectar/Desconectar só fazem sentido com a conexão ativa aqui.
        inactive = info is None or not info.active
        for widget_id in ("#mcp_reconnect", "#mcp_disconnect"):
            self.query_one(widget_id, Button).disabled = busy_or_empty or inactive
        toggle = self.query_one("#mcp_toggle", Button)
        toggle.label = "Desativar" if info is not None and info.active else "Ativar"
        toggle.disabled = busy_or_empty
        self.query_one("#mcp_new", Button).disabled = self._busy
        self._update_auth_row()

    def _update_auth_row(self) -> None:
        """Mostra a linha de autorização só quando a seleção tem OAuth pendente."""
        pending = self._selected_auth_url() if self._selected_name else None
        row = self.query_one("#mcp_auth_row")
        row.set_class(pending is None, "hidden")
        if pending is None:
            return
        name, _url = pending
        self.query_one("#mcp_auth_label", Label).update(
            f"'{name}' aguarda autorização no navegador"
        )

    def _update_client_action_state(self) -> None:
        http_server = getattr(self.quimera_app, "external_mcp_http_server", None)
        group = self._selected_client_group()
        toggle = self.query_one("#mcp_client_toggle", Button)
        toggle.label = "Desativar" if group is not None and group.active else "Ativar"
        # A liberação é configuração do workspace: vale mesmo com o HTTP parado.
        toggle.disabled = self._busy or group is None or not group.authorized
        # Revogar mexe no store OAuth vivo; sem servidor não há provider.
        self.query_one("#mcp_revoke_client", Button).disabled = (
            self._busy or group is None or http_server is None
        )

    def action_toggle_client(self) -> None:
        """Libera ou bloqueia o client OAuth selecionado neste workspace."""
        if self._busy:
            return
        group = self._selected_client_group()
        if group is None:
            return
        if not group.authorized:
            self.parent_app.notify(
                f"'{group.name}' não tem autorização válida; autorize-o antes de ativar.",
                severity="warning",
            )
            return
        enable = not group.active
        verb = "Ativar" if enable else "Desativar"

        def apply(scope: str) -> None:
            where = "em todos os workspaces" if scope == SCOPE_GLOBAL else "neste workspace"
            self._start_busy(f"{'Ativando' if enable else 'Desativando'} {group.name} {where}…")
            self._client_toggle_worker(group, enable, scope)

        self._ask_scope(
            f"{verb} o client '{group.name}'",
            "Neste workspace: só aqui; o padrão global não desfaz a escolha.\n"
            "Em todos: muda o padrão global, e vale na próxima requisição de cada workspace "
            "aberto. Quem tem acesso usa as tools do workspace, inclusive shell e escrita.",
            apply,
        )

    @work(thread=True, exclusive=True, group="mcp-management")
    def _client_toggle_worker(self, group: MCPClientGroup, enable: bool, scope: str) -> None:
        closed = 0
        try:
            self._http_store().set_client_active(group.name, enable, scope)
            if not enable:
                http_server = getattr(self.quimera_app, "external_mcp_http_server", None)
                close = getattr(http_server, "close_client_sessions", None)
                if callable(close):
                    closed = int(close(group.client_ids) or 0)
        except Exception as exc:
            verb = "ativar" if enable else "desativar"
            self.app.call_from_thread(
                self._finish_client_action,
                f"Falha ao {verb} {group.name}: {exc}",
                "error",
                group.name,
            )
            return
        where = "em todos os workspaces" if scope == SCOPE_GLOBAL else "neste workspace"
        if enable:
            message = f"Client '{group.name}' ativado {where} · pode usar o servidor MCP HTTP."
        else:
            message = f"Client '{group.name}' desativado {where}."
            if closed:
                message = f"{message} · {closed} sessão(ões) encerrada(s)."
        self.app.call_from_thread(self._finish_client_action, message, "information", group.name)

    def _revoke_selected_client(self) -> None:
        if self._busy:
            return
        group = self._selected_client_group()
        if group is None:
            return
        http_server = getattr(self.quimera_app, "external_mcp_http_server", None)
        if http_server is None:
            self.parent_app.notify("Servidor MCP HTTP não está ativo.", severity="error")
            return
        self._start_busy(f"Revogando autorização de {group.name}…")
        self._revoke_client_worker(http_server, group)

    @work(thread=True, exclusive=True, group="mcp-management")
    def _revoke_client_worker(self, http_server, group: MCPClientGroup) -> None:
        try:
            for client_id in group.client_ids:
                http_server.revoke_client_authorization(client_id)
        except Exception as exc:
            self.app.call_from_thread(
                self._finish_client_action,
                f"Falha ao revogar autorização de {group.name}: {exc}",
                "error",
                group.name,
            )
            return
        self.app.call_from_thread(
            self._finish_client_action,
            f"Autorização de '{group.name}' revogada em todos os workspaces.",
            "information",
            None,
        )

    def _finish_client_action(
        self,
        message: str,
        severity: str,
        selected_client: str | None,
    ) -> None:
        self._busy = False
        self._selected_client = selected_client
        self._refresh_view()
        self._update_action_state()
        self._update_client_action_state()
        self._set_activity(message)
        self.parent_app.notify(message, severity=severity)

    @work(thread=True, exclusive=True, group="mcp-management")
    def _disconnect_worker(self, name: str) -> None:
        try:
            self.manager.disconnect(name)
        except Exception as exc:
            self.app.call_from_thread(
                self._finish_busy,
                f"Falha ao desconectar {name}: {exc}",
                "error",
                name,
            )
            return
        self.app.call_from_thread(
            self._finish_busy,
            f"MCP '{name}' desconectado nesta sessão.",
            "information",
            name,
        )

    @work(thread=True, exclusive=True, group="mcp-management")
    def _remove_worker(self, name: str) -> None:
        info = self._find_info(name)
        try:
            fallback = self.manager.remove(name)
        except Exception as exc:
            self.app.call_from_thread(
                self._finish_busy,
                f"Falha ao remover {name}: {exc}",
                "error",
                name,
            )
            return
        if info is not None and info.is_global:
            message = f"MCP '{name}' removido do catálogo global."
        elif fallback is not None:
            message = (
                f"MCP '{name}' local removido · a global volta a valer"
                f"{' e reconecta' if fallback.active else ' (inativa aqui)'}."
            )
        else:
            message = f"MCP '{name}' removido do workspace."
        self.app.call_from_thread(
            self._finish_busy,
            message,
            "information",
            name if fallback is not None else None,
        )

    def _start_busy(self, message: str) -> None:
        self._busy = True
        self._update_action_state()
        self._update_client_action_state()
        for widget_id in ("#mcp_server_toggle", "#mcp_server_configure"):
            self.query_one(widget_id, Button).disabled = True
        self._set_activity(message)

    def _finish_busy(
        self,
        message: str,
        severity: str,
        selected_name: str | None = None,
    ) -> None:
        self._busy = False
        self._selected_name = selected_name
        self._refresh_view()
        self._update_client_action_state()
        self._set_activity(message)
        self.parent_app.notify(message, severity=severity)

    def _set_activity(self, message: str) -> None:
        self.query_one("#mcp_activity", Label).update(message)

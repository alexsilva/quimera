"""Hub visual para inspecionar e gerenciar conexões MCP da sessão."""
from __future__ import annotations

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select

from quimera.runtime.drivers.tool_schemas import get_bridge_schemas
from quimera.runtime.mcp.manager import MCPConnectionInfo, MCPConnectionManager


class MCPServerEditorScreen(ModalScreen[str | None]):
    """Editor compacto para criar ou reconfigurar um servidor MCP de saída."""

    CSS = """
    MCPServerEditorScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }
    #mcp_editor_dialog {
        width: 82;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    #mcp_editor_header {
        height: 2;
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
        height: auto;
    }
    #mcp_editor_fields Label {
        margin-top: 1;
        color: $text-muted;
    }
    #mcp_editor_fields Input, #mcp_editor_fields Select {
        width: 100%;
    }
    #mcp_editor_buttons {
        height: 2;
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
        with Container(id="mcp_editor_dialog"):
            with Horizontal(id="mcp_editor_header"):
                yield Label(
                    "Editar servidor MCP" if info is not None else "Adicionar servidor MCP",
                    id="mcp_editor_title",
                )
                yield Button("×", id="mcp_editor_close")

            with Vertical(id="mcp_editor_fields"):
                yield Label("Nome")
                yield Input(
                    value=info.name if info is not None else "",
                    placeholder="github",
                    id="mcp_editor_name",
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

                yield Label(
                    "Ambiente opcional · KEY=valor,KEY2=valor2 · vazio mantém o existente"
                )
                yield Input(
                    placeholder="TOKEN=...",
                    password=True,
                    id="mcp_editor_env",
                )

            with Horizontal(id="mcp_editor_buttons"):
                yield Button("Cancelar", id="mcp_editor_cancel")
                yield Button("Conectar e salvar", id="mcp_editor_save", variant="primary")
            yield Label("", id="mcp_editor_activity")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in {"mcp_editor_close", "mcp_editor_cancel"}:
            self.action_cancel()
        elif event.button.id == "mcp_editor_save":
            self.action_save()

    def action_cancel(self) -> None:
        if not self._busy:
            self.dismiss(None)

    def action_save(self) -> None:
        if self._busy:
            return
        try:
            spec, env_spec, name = self._build_specs()
        except ValueError as exc:
            self.parent_app.notify(str(exc), severity="error")
            return
        previous_name = self.info.name if self.info is not None else None
        self._busy = True
        self.query_one("#mcp_editor_activity", Label).update(f"Conectando {name}…")
        self._save_worker(spec, env_spec, name, previous_name)

    def _build_specs(self) -> tuple[str, str | None, str]:
        name = self.query_one("#mcp_editor_name", Input).value.strip()
        endpoint = self.query_one("#mcp_editor_endpoint", Input).value.strip()
        transport_value = self.query_one("#mcp_editor_transport", Select).value
        transport = str(transport_value or "").strip()
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

        env_text = self.query_one("#mcp_editor_env", Input).value.strip()
        env_spec = f"{name}={env_text}" if env_text else None
        return spec, env_spec, name

    @work(thread=True, exclusive=True, group="mcp-editor")
    def _save_worker(
        self,
        spec: str,
        env_spec: str | None,
        name: str,
        previous_name: str | None,
    ) -> None:
        try:
            self.manager.upsert(spec, env_spec=env_spec)
            if previous_name and previous_name != name:
                self.manager.remove(previous_name)
        except Exception as exc:
            self.app.call_from_thread(self._save_failed, name, str(exc))
            return
        self.app.call_from_thread(self._save_succeeded, name)

    def _save_failed(self, name: str, error: str) -> None:
        self._busy = False
        message = f"Falha ao conectar {name}: {error}"
        self.query_one("#mcp_editor_activity", Label).update(message)
        self.parent_app.notify(message, severity="error")

    def _save_succeeded(self, name: str) -> None:
        self._busy = False
        self.parent_app.notify(
            f"MCP '{name}' conectado e salvo.",
            severity="information",
        )
        self.dismiss(name)


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
        min-width: 20;
        border: none;
    }
    #mcp_table {
        height: 1fr;
        margin-top: 1;
    }
    #mcp_actions {
        height: 2;
        margin-top: 1;
    }
    #mcp_actions Button {
        height: 1;
        min-width: 12;
        border: none;
        margin-right: 1;
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
        ("ctrl+r", "reconnect", "Reconectar"),
    ]

    AUTO_FOCUS = "#mcp_incoming_table"

    def __init__(self, quimera_app, parent_app) -> None:
        super().__init__()
        self.quimera_app = quimera_app
        self.parent_app = parent_app
        self.manager = MCPConnectionManager.from_app(quimera_app)
        self._selected_name: str | None = None
        self._selected_client_id: str | None = None
        self._active_tab = "clients"
        self._busy = False

    def compose(self) -> ComposeResult:
        with Container(id="mcp_dialog"):
            with Horizontal(id="mcp_header"):
                yield Label("MCP Hub", id="mcp_title")
                yield Button("×", id="mcp_close_top")

            with Vertical(id="mcp_roles"):
                yield Label("", id="mcp_socket_state", classes="mcp_role_state")
                yield Label("", id="mcp_http_state", classes="mcp_role_state")

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
                    yield Button("Adicionar servidor", id="mcp_new", variant="primary")
                    yield Button("Editar", id="mcp_edit")
                    yield Button("Reconectar", id="mcp_reconnect")
                    yield Button("Desconectar", id="mcp_disconnect")
                    yield Button("Remover", id="mcp_remove", variant="error")
            yield Label("", id="mcp_activity")

    def on_mount(self) -> None:
        incoming = self.query_one("#mcp_incoming_table", DataTable)
        incoming.add_columns("Cliente", "Transporte", "Escopo", "Estado")
        table = self.query_one("#mcp_table", DataTable)
        table.add_columns("Nome", "Transporte", "Endpoint", "Estado")
        self._refresh_view()
        self._update_action_state()
        self._update_client_action_state()

    def _refresh_view(self) -> None:
        self._refresh_roles()
        self._refresh_incoming_clients()
        self._refresh_connections()

    def _refresh_roles(self) -> None:
        socket_path = str(getattr(self.quimera_app, "mcp_socket_path", "") or "")
        http_url = str(getattr(self.quimera_app, "mcp_http_url", "") or "")
        self.query_one("#mcp_socket_state", Label).update(
            self._status_text("Socket local", socket_path)
        )
        self.query_one("#mcp_http_state", Label).update(
            self._status_text("MCP HTTP OAuth", http_url)
        )

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
            clients = []
        else:
            provider = getattr(http_server, "known_clients", None)
            clients = provider() if callable(provider) else http_server.connected_clients()
        authorized_count = sum(
            1 for client in clients if getattr(client, "authorized", False)
        )
        self.query_one("#mcp_incoming_summary", Label).update(
            f"{authorized_count} autorizados"
        )
        seen_row_keys: set[str] = set()
        for client in clients:
            label = client.client_name or client.client_id or client.session_id[:8]
            scope = client.scope or client.profile or "mcp"
            state = (
                "autorizado"
                if getattr(client, "authorized", False)
                else "não autorizado"
            )
            row_key = client.client_id or client.session_id
            if not row_key or row_key in seen_row_keys:
                continue
            seen_row_keys.add(row_key)
            table.add_row(label, "http/oauth", scope, state, key=row_key)

    def _refresh_connections(self) -> None:
        table = self.query_one("#mcp_table", DataTable)
        table.clear()
        connections = self.manager.list_connections()
        connected = sum(1 for item in connections if item.connected)
        schemas = get_bridge_schemas()
        self.query_one("#mcp_clients_summary", Label).update(
            f"{connected}/{len(connections)} conectados · {len(schemas)} tools"
        )
        for info in connections:
            table.add_row(
                info.name,
                info.transport,
                self._short_endpoint(info.endpoint),
                "conectado" if info.connected else "offline",
                key=info.name,
            )
        if self._selected_name and not any(
            item.name == self._selected_name for item in connections
        ):
            self._selected_name = None
        self._update_action_state()

    @staticmethod
    def _short_endpoint(endpoint: str, limit: int = 48) -> str:
        if len(endpoint) <= limit:
            return endpoint
        return endpoint[: limit - 1] + "…"

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Mantém o estado das ações alinhado à linha visualmente destacada."""
        if event.data_table.id == "mcp_incoming_table":
            client_id = str(event.row_key.value or "")
            self._selected_client_id = client_id or None
            self._update_client_action_state()
            if client_id:
                self._set_activity(f"Cliente selecionado: {client_id}")
            return
        if event.data_table.id != "mcp_table":
            return
        name = str(event.row_key.value or "")
        if not name:
            return
        self._selected_name = name
        self._update_action_state()
        self._set_activity(f"Servidor selecionado: {name}")

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
        elif button_id == "mcp_reconnect":
            self.action_reconnect()
        elif button_id == "mcp_disconnect":
            self._disconnect_selected()
        elif button_id == "mcp_remove":
            self._remove_selected()
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
        if name:
            self._selected_name = name
        self._refresh_view()
        if name:
            self._set_activity(f"Servidor '{name}' atualizado.")

    def action_reconnect(self) -> None:
        if self._busy:
            return
        name = self._require_selected()
        if name is None:
            return
        self._start_busy(f"Reconectando {name}…")
        self._reconnect_worker(name)

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
        self._start_busy(f"Removendo {name}…")
        self._remove_worker(name)

    def _selected_info(self) -> MCPConnectionInfo | None:
        name = self._require_selected()
        if name is None:
            return None
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
        selected = bool(self._selected_name)
        busy_or_empty = self._busy or not selected
        for widget_id in (
            "#mcp_edit",
            "#mcp_reconnect",
            "#mcp_disconnect",
            "#mcp_remove",
        ):
            self.query_one(widget_id, Button).disabled = busy_or_empty
        self.query_one("#mcp_new", Button).disabled = self._busy

    def _update_client_action_state(self) -> None:
        self.query_one("#mcp_revoke_client", Button).disabled = (
            self._busy or not self._selected_client_id
        )

    def _revoke_selected_client(self) -> None:
        if self._busy or not self._selected_client_id:
            return
        http_server = getattr(self.quimera_app, "external_mcp_http_server", None)
        if http_server is None:
            self.parent_app.notify("Servidor MCP HTTP não está ativo.", severity="error")
            return
        client_id = self._selected_client_id
        self._start_busy(f"Revogando autorização de {client_id}…")
        self._revoke_client_worker(http_server, client_id)

    @work(thread=True, exclusive=True, group="mcp-management")
    def _revoke_client_worker(self, http_server, client_id: str) -> None:
        try:
            http_server.revoke_client_authorization(client_id)
        except Exception as exc:
            self.app.call_from_thread(
                self._finish_client_revoke,
                f"Falha ao revogar autorização de {client_id}: {exc}",
                "error",
                client_id,
            )
            return
        self.app.call_from_thread(
            self._finish_client_revoke,
            f"Autorização de '{client_id}' revogada.",
            "information",
            None,
        )

    def _finish_client_revoke(
        self,
        message: str,
        severity: str,
        selected_client_id: str | None,
    ) -> None:
        self._busy = False
        self._selected_client_id = selected_client_id
        self._refresh_view()
        self._update_action_state()
        self._update_client_action_state()
        self._set_activity(message)
        self.parent_app.notify(message, severity=severity)

    @work(thread=True, exclusive=True, group="mcp-management")
    def _reconnect_worker(self, name: str) -> None:
        try:
            self.manager.reconnect(name)
        except Exception as exc:
            self.app.call_from_thread(
                self._finish_busy,
                f"Falha ao reconectar {name}: {exc}",
                "error",
                name,
            )
            return
        self.app.call_from_thread(
            self._finish_busy,
            f"MCP '{name}' reconectado.",
            "information",
            name,
        )

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
        try:
            self.manager.remove(name)
        except Exception as exc:
            self.app.call_from_thread(
                self._finish_busy,
                f"Falha ao remover {name}: {exc}",
                "error",
                name,
            )
            return
        self.app.call_from_thread(
            self._finish_busy,
            f"MCP '{name}' removido do workspace.",
            "information",
            None,
        )

    def _start_busy(self, message: str) -> None:
        self._busy = True
        self._update_action_state()
        self._update_client_action_state()
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

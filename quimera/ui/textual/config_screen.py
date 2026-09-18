"""Janela popup de configuração do Quimera."""
from __future__ import annotations

from typing import TYPE_CHECKING
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select
from quimera.app.prompt_formatter import PromptFormatter
from quimera.themes import DENSITY_OPTIONS, names as theme_names

if TYPE_CHECKING:
    from quimera.config import ConfigManager
    from quimera.ui.textual.app import QuimeraTextualApp


class ConfigScreen(ModalScreen[None]):
    """Janela popup de configuração do Quimera."""

    CSS = """
    ConfigScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }
    #config_dialog {
        width: 64;
        height: 90%;
        max-height: 34;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    #config_header {
        height: 1;
        margin-bottom: 1;
    }
    #config_title {
        width: 1fr;
        content-align: center middle;
        text-align: center;
        text-style: bold;
        color: $accent;
    }
    #config_close {
        width: 3;
        min-width: 3;
        height: 1;
        border: none;
        padding: 0;
    }
    #config_fields {
        height: 1fr;
        min-height: 3;
        overflow-y: auto;
    }
    #config_fields Label {
        margin-top: 1;
        color: $text-muted;
    }
    #config_footer {
        height: 3;
        margin-top: 1;
    }
    #config_buttons {
        height: 1;
        align-horizontal: right;
    }
    #config_buttons Button {
        height: 1;
        min-width: 12;
        border: none;
        margin-left: 2;
    }
    #config_hint {
        margin-top: 1;
        width: 100%;
        text-align: center;
        color: $text-muted;
        height: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancelar"),
        ("ctrl+s", "save", "Aplicar"),
    ]

    AUTO_FOCUS = "#cfg_user_name"

    def __init__(self, quimera_app, parent_app: QuimeraTextualApp) -> None:
        """Inicializa a tela de configuração."""
        super().__init__()
        self.quimera_app = quimera_app
        self.parent_app = parent_app
        self.config: ConfigManager = quimera_app.config

    def compose(self) -> ComposeResult:
        """Monta o layout da janela de configuração."""
        with Container(id="config_dialog"):
            with Horizontal(id="config_header"):
                yield Label("Configurações do Quimera", id="config_title")
                yield Button("×", id="config_close")

            with Vertical(id="config_fields"):
                yield Label("Nome do Usuário:")
                yield Input(value=self.config.user_name, id="cfg_user_name")

                yield Label("Janela de Histórico:")
                yield Input(value=str(self.config.history_window), id="cfg_history_window")

                yield Label("Limite de Resumo (auto-summarize):")
                yield Input(value=str(self.config.auto_summarize_threshold), id="cfg_auto_summarize")

                yield Label("Timeout Inativo (segundos):")
                yield Input(value=str(self.config.idle_timeout_seconds), id="cfg_idle_timeout")

                yield Label("Tempo Máximo do Agente (segundos):")
                yield Input(
                    value=str(self.config.max_agent_execution_seconds),
                    id="cfg_max_agent_execution",
                )

                yield Label("Política do Workspace:")
                yield Select(
                    [
                        ("strict", "strict"),
                        ("developer", "developer"),
                        ("autonomous", "autonomous"),
                    ],
                    value=self.config.workspace_policy,
                    id="cfg_workspace_policy",
                )

                yield Label("Visibilidade da Execução:")
                active_visibility = getattr(
                    getattr(self.quimera_app, "visibility", None), "value", None
                ) or self.config.visibility
                yield Select(
                    [(v, v) for v in ("quiet", "summary", "full")],
                    value=active_visibility,
                    id="cfg_visibility",
                )

                yield Label("Agentes em Paralelo (threads):")
                yield Input(value=str(self.config.threads), id="cfg_threads")

                yield Label("Tema:")
                theme_options = [(t, t) for t in theme_names()]
                yield Select(theme_options, value=self.config.theme, id="cfg_theme")

                yield Label("Densidade:")
                density_options = [(d, d) for d in DENSITY_OPTIONS]
                yield Select(density_options, value=self.config.density, id="cfg_density")

            with Vertical(id="config_footer"):
                with Horizontal(id="config_buttons"):
                    yield Button("Cancelar", id="cfg_cancel")
                    yield Button("Aplicar", variant="primary", id="cfg_save")

                yield Label("Enter/Ctrl+S aplica · Esc cancela", id="config_hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Trata o clique dos botões."""
        if event.button.id in {"config_close", "cfg_cancel"}:
            self.action_cancel()
        elif event.button.id == "cfg_save":
            self.action_save()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Aplica as configurações ao pressionar Enter em um campo."""
        event.stop()
        self.action_save()

    def action_cancel(self) -> None:
        """Fecha a janela descartando as alterações."""
        self.dismiss()

    def action_save(self) -> None:
        """Valida e salva as configurações configuradas."""
        user_name = self.query_one("#cfg_user_name", Input).value.strip()

        # Validar inteiros
        try:
            history_window = int(self.query_one("#cfg_history_window", Input).value)
            if history_window <= 0:
                raise ValueError
        except ValueError:
            self.parent_app.notify("Janela de histórico deve ser um número inteiro positivo.", severity="error")
            return

        try:
            auto_summarize = int(self.query_one("#cfg_auto_summarize", Input).value)
            if auto_summarize <= 0:
                raise ValueError
        except ValueError:
            self.parent_app.notify("Limite de resumo deve ser um número inteiro positivo.", severity="error")
            return

        try:
            idle_timeout = int(self.query_one("#cfg_idle_timeout", Input).value)
            if idle_timeout <= 0:
                raise ValueError
        except ValueError:
            self.parent_app.notify("Timeout inativo deve ser um número inteiro positivo.", severity="error")
            return

        try:
            max_agent_execution = int(
                self.query_one("#cfg_max_agent_execution", Input).value
            )
            if max_agent_execution <= 0:
                raise ValueError
        except ValueError:
            self.parent_app.notify(
                "Tempo máximo do agente deve ser um número inteiro positivo.",
                severity="error",
            )
            return

        try:
            threads = int(self.query_one("#cfg_threads", Input).value)
            if threads <= 0:
                raise ValueError
        except ValueError:
            self.parent_app.notify("Threads deve ser um número inteiro positivo.", severity="error")
            return

        workspace_policy = self.query_one("#cfg_workspace_policy", Select).value
        visibility = self.query_one("#cfg_visibility", Select).value
        theme = self.query_one("#cfg_theme", Select).value
        density = self.query_one("#cfg_density", Select).value

        if theme is None or theme is Select.BLANK:
            theme = self.config.theme
        if density is None or density is Select.BLANK:
            density = self.config.density
        if workspace_policy is None or workspace_policy is Select.BLANK:
            workspace_policy = self.config.workspace_policy
        if visibility is None or visibility is Select.BLANK:
            visibility = self.config.visibility

        # Salvar no config manager
        self.config.set_user_name(user_name)
        self.config.set_history_window(history_window)
        self.config.set_auto_summarize_threshold(auto_summarize)
        self.config.set_idle_timeout_seconds(idle_timeout)
        self.config.set_max_agent_execution_seconds(max_agent_execution)
        self.config.set_theme(str(theme))
        self.config.set_density(str(density))
        threads_changed = threads != getattr(self.quimera_app, "threads", threads)
        self.config.set_threads(threads)

        # Policy e visibility propagam para a sessão viva quando o app expõe
        # os setters (que também persistem); senão persiste-se direto.
        policy_setter = getattr(self.quimera_app, "set_workspace_policy_name", None)
        if callable(policy_setter):
            policy_setter(str(workspace_policy))
        else:
            self.config.set_workspace_policy(str(workspace_policy))
        visibility_setter = getattr(self.quimera_app, "set_visibility_name", None)
        if callable(visibility_setter):
            visibility_setter(str(visibility))
        else:
            self.config.set_visibility(str(visibility))

        # Atualizar dinamicamente
        input_widget = self.parent_app.query_one("#input")
        input_widget.set_prefix(PromptFormatter.format_user_prompt(user_name))
        effective_user_name = user_name or self.config.user_name
        if hasattr(self.quimera_app, "user_name"):
            self.quimera_app.user_name = effective_user_name
        if hasattr(self.quimera_app, "idle_timeout_seconds"):
            self.quimera_app.idle_timeout_seconds = idle_timeout
        agent_client = getattr(self.quimera_app, "agent_client", None)
        if agent_client is not None:
            agent_client.idle_timeout = idle_timeout
            agent_client.max_execution_seconds = max_agent_execution
        memory_selector = getattr(
            getattr(self.quimera_app, "prompt_builder", None), "memory_selector", None
        )
        if memory_selector is not None:
            memory_selector.user_name = effective_user_name

        renderer = getattr(self.quimera_app, "renderer", None)
        if renderer is not None and callable(getattr(renderer, "set_theme", None)):
            renderer.set_theme(str(theme))

        message = "Configurações salvas com sucesso!"
        if threads_changed:
            message += " Threads vale a partir da próxima sessão."
        self.parent_app.notify(message, severity="information")
        self.dismiss()

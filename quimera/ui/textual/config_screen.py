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
        height: auto;
        max-height: 90%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    #config_title {
        text-align: center;
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #config_fields {
        height: auto;
        max-height: 45vh;
        overflow-y: auto;
    }
    #config_fields Label {
        margin-top: 1;
        color: $text-muted;
    }
    #config_buttons {
        margin-top: 1;
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
            yield Label("Configurações do Quimera", id="config_title")

            with Vertical(id="config_fields"):
                yield Label("Nome do Usuário:")
                yield Input(value=self.config.user_name, id="cfg_user_name")

                yield Label("Janela de Histórico:")
                yield Input(value=str(self.config.history_window), id="cfg_history_window")

                yield Label("Limite de Resumo (auto-summarize):")
                yield Input(value=str(self.config.auto_summarize_threshold), id="cfg_auto_summarize")

                yield Label("Timeout Inativo (segundos):")
                yield Input(value=str(self.config.idle_timeout_seconds), id="cfg_idle_timeout")

                yield Label("Política do Workspace:")
                yield Select(
                    [("strict", "strict"), ("autonomous", "autonomous")],
                    value=self.config.workspace_policy,
                    id="cfg_workspace_policy",
                )

                yield Label("Tema:")
                theme_options = [(t, t) for t in theme_names()]
                yield Select(theme_options, value=self.config.theme, id="cfg_theme")

                yield Label("Densidade:")
                density_options = [(d, d) for d in DENSITY_OPTIONS]
                yield Select(density_options, value=self.config.density, id="cfg_density")

            with Horizontal(id="config_buttons"):
                yield Button("Cancelar", id="cfg_cancel")
                yield Button("Aplicar", variant="primary", id="cfg_save")

            yield Label("Enter/Ctrl+S aplica · Esc cancela", id="config_hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Trata o clique dos botões."""
        if event.button.id == "cfg_cancel":
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

        workspace_policy = self.query_one("#cfg_workspace_policy", Select).value
        theme = self.query_one("#cfg_theme", Select).value
        density = self.query_one("#cfg_density", Select).value

        if theme is None or theme is Select.BLANK:
            theme = self.config.theme
        if density is None or density is Select.BLANK:
            density = self.config.density
        if workspace_policy is None or workspace_policy is Select.BLANK:
            workspace_policy = self.config.workspace_policy

        # Salvar no config manager
        self.config.set_user_name(user_name)
        self.config.set_history_window(history_window)
        self.config.set_auto_summarize_threshold(auto_summarize)
        self.config.set_idle_timeout_seconds(idle_timeout)
        self.config.set_workspace_policy(str(workspace_policy))
        self.config.set_theme(str(theme))
        self.config.set_density(str(density))

        # Atualizar dinamicamente
        input_widget = self.parent_app.query_one("#input")
        input_widget.set_prefix(PromptFormatter.format_user_prompt(user_name))

        renderer = getattr(self.quimera_app, "renderer", None)
        if renderer is not None and callable(getattr(renderer, "set_theme", None)):
            renderer.set_theme(str(theme))

        self.parent_app.notify("Configurações salvas com sucesso!", severity="information")
        self.dismiss()

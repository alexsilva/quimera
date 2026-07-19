"""Janela modal para inspecionar o prompt final de um agente."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog


class PromptPreviewLog(RichLog):
    """Visualizador rolável que não permite seleção de texto."""

    ALLOW_SELECT = False


class PromptPreviewScreen(ModalScreen[None]):
    """Exibe o preview do prompt sem adicioná-lo ao feed do chat."""

    ALLOW_SELECT = False

    CSS = """
    PromptPreviewScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }
    #prompt_preview_dialog {
        width: 90%;
        height: 90%;
        max-width: 140;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    #prompt_preview_title {
        height: 1;
        margin-bottom: 1;
        text-align: center;
        text-style: bold;
        color: $accent;
    }
    #prompt_preview_content {
        height: 1fr;
        border: solid $panel;
        background: $background;
        pointer: default;
    }
    #prompt_preview_buttons {
        height: 1;
        margin-top: 1;
        align-horizontal: right;
    }
    #prompt_preview_close {
        height: 1;
        min-width: 12;
        border: none;
        margin-left: 2;
    }
    """

    BINDINGS = [("escape", "close", "Fechar")]
    AUTO_FOCUS = "#prompt_preview_content"

    def __init__(self, agent: str, preview: str) -> None:
        """Inicializa o modal com o agente e o texto já construído."""
        super().__init__()
        self.agent = str(agent or "agente")
        self.preview = str(preview or "")
        self._previous_app_allow_select: bool | None = None

    def compose(self) -> ComposeResult:
        """Monta o conteúdo rolável e o comando de fechamento."""
        with Container(id="prompt_preview_dialog"):
            yield Label(
                f"Prompt Preview - {self.agent}",
                id="prompt_preview_title",
            )
            yield PromptPreviewLog(
                id="prompt_preview_content",
                markup=False,
                highlight=False,
                wrap=False,
                auto_scroll=False,
            )
            with Horizontal(id="prompt_preview_buttons"):
                yield Button(
                    "Fechar",
                    id="prompt_preview_close",
                    variant="primary",
                )

    def on_mount(self) -> None:
        """Carrega o texto no visualizador e posiciona a rolagem no início."""
        self._previous_app_allow_select = bool(self.app.ALLOW_SELECT)
        self.app.ALLOW_SELECT = False
        for screen in self.app.screen_stack:
            screen.clear_selection()
        content = self.query_one("#prompt_preview_content", PromptPreviewLog)
        content.write(self.preview)
        content.scroll_home(animate=False)

    def on_unmount(self) -> None:
        """Restaura a política de seleção vigente antes de abrir o modal."""
        if self._previous_app_allow_select is not None:
            self.app.ALLOW_SELECT = self._previous_app_allow_select
            self._previous_app_allow_select = None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Fecha o modal pelo botão principal."""
        if event.button.id == "prompt_preview_close":
            self.action_close()

    def action_close(self) -> None:
        """Fecha a janela de preview."""
        self.dismiss()

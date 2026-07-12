"""Widgets Textual usados pela aplicação principal."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable

from rich.highlighter import Highlighter
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.geometry import clamp
from textual.worker import WorkerCancelled
from textual.widgets import Header, Input, Static
from textual.widgets._header import HeaderClock, HeaderClockSpace, HeaderIcon, HeaderTitle
from textual.widgets._input import Selection

from quimera.app.completion_dropdown import CompletionDropdown, PromptHistorySuggester
from quimera.clipboard_support import ClipboardManager

logger = logging.getLogger(__name__)

_ATTACHED_IMAGE_LABEL = "🖼 imagem anexada"


class _PrefixDimHighlighter(Highlighter):
    """Aplica estilo dim ao prefixo fixo do input."""

    def __init__(self, get_prefix_len: Callable[[], int]) -> None:
        self._get_prefix_len = get_prefix_len

    def highlight(self, text: Text) -> None:
        n = self._get_prefix_len()
        if n > 0:
            text.stylize("dim", 0, n)


class _CompletionInput(Input):
    """Input com prefixo fixo (>>>: ), autocomplete inline e histórico."""

    BINDINGS = [
        Binding("escape", "escape", "Fechar popup"),
        Binding("ctrl+v", "paste_clipboard", "Colar clipboard"),
        Binding("f8", "paste_clipboard", "Colar clipboard"),
        Binding("ctrl+u", "delete_left_all", "Limpar linha"),
        Binding("ctrl+k", "delete_right_all", "Apagar até fim"),
    ]

    def __init__(
        self,
        *args,
        prefix: str = ">>>: ",
        clipboard_paste_handler: Callable[[], str | None] | None = None,
        **kwargs,
    ):
        self._prefix = prefix
        self._clipboard_paste_handler = clipboard_paste_handler
        kwargs.setdefault("value", prefix)
        kwargs.setdefault("select_on_focus", False)
        kwargs.setdefault("highlighter", _PrefixDimHighlighter(lambda: len(self._prefix)))
        super().__init__(*args, **kwargs)
        self._prompt_history: list[str] = []
        self._history_index = 0
        self._saved_draft = ""
        self._clipboard_manager = ClipboardManager()
        self._attached_image_placeholders: dict[str, str] = {}
        self._attached_image_counter = 0
        self.suggester = PromptHistorySuggester(lambda: self._prompt_history)

    @property
    def user_value(self) -> str:
        """Texto digitado pelo usuário, sem o prefixo."""
        v = self.value
        return v[len(self._prefix):] if v.startswith(self._prefix) else v

    @property
    def submission_value(self) -> str:
        """Texto enviado ao runtime, expandindo anexos exibidos como placeholders."""
        value = self.user_value
        for placeholder, marker in self._attached_image_placeholders.items():
            value = value.replace(placeholder, marker)
        return value

    def validate_selection(self, selection: Selection) -> Selection:
        start, end = selection
        value_length = len(self.value)
        prefix_length = len(self._prefix) if self.value.startswith(self._prefix) else 0
        return Selection(
            clamp(start, prefix_length, value_length),
            clamp(end, prefix_length, value_length),
        )

    def set_prefix(self, prefix: str) -> None:
        """Atualiza o prefixo preservando o texto já digitado."""
        user_text = self.user_value
        self._prefix = prefix
        self.value = prefix + user_text
        self.cursor_position = len(self.value)

    def reset_to_prefix(self) -> None:
        """Limpa o input, deixando apenas o prefixo."""
        self.value = self._prefix
        self.cursor_position = len(self._prefix)
        self._attached_image_placeholders.clear()

    def insert_user_text(self, text: str) -> None:
        """Insere texto respeitando o prefixo fixo."""
        payload = self._prepare_insert_payload(str(text or ""))
        if not payload:
            return
        if self.cursor_position < len(self._prefix):
            self.cursor_position = len(self._prefix)
        self.insert_text_at_cursor(payload)

    def _prepare_insert_payload(self, text: str) -> str:
        images = self._clipboard_manager.iter_images(text)
        if not images:
            return text
        chunks: list[str] = []
        cursor = 0
        for image in images:
            chunks.append(text[cursor:image.start])
            self._attached_image_counter += 1
            placeholder = f"{_ATTACHED_IMAGE_LABEL} {self._attached_image_counter}"
            self._attached_image_placeholders[placeholder] = image.marker
            chunks.append(placeholder)
            cursor = image.end
        chunks.append(text[cursor:])
        return "".join(chunks)

    def action_delete_left(self) -> None:
        if self.cursor_position <= len(self._prefix):
            return
        super().action_delete_left()

    def action_delete_left_word(self) -> None:
        if self.cursor_position <= len(self._prefix):
            return
        super().action_delete_left_word()

    def action_delete_left_all(self) -> None:
        right = self.value[self.cursor_position:]
        self.value = self._prefix + right
        self.cursor_position = len(self._prefix)

    def action_delete_right_all(self) -> None:
        self.value = self.value[:self.cursor_position]
        if not self.value.startswith(self._prefix):
            self.value = self._prefix

    def add_to_history(self, value: str) -> None:
        if value:
            self._prompt_history.append(value)
            self._history_index = 0
            self._saved_draft = ""

    def load_history(self, path: Path | None) -> None:
        """Carrega histórico persistente do input, quando disponível.

        Cada linha do arquivo é um valor JSON (string), preservando entradas
        multi-linha como um único item. Linhas que não são JSON válido são
        aceitas como texto puro, para compatibilidade com arquivos antigos.
        """
        if path is None or not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                value = line.removeprefix("+").strip()
            if isinstance(value, str) and value:
                entries.append(value)
        self._prompt_history = entries[-1000:]
        self._history_index = 0
        self._saved_draft = ""

    def save_history(self, path: Path | None) -> None:
        """Persiste histórico do input para próxima sessão (uma entrada JSON por linha)."""
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            entries = [
                self._clipboard_manager.strip_markers(entry)
                for entry in self._prompt_history[-1000:]
            ]
            lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
            payload = "\n".join(lines)
            path.write_text(f"{payload}\n" if payload else "", encoding="utf-8")
        except OSError:
            return

    async def action_submit(self) -> None:
        await self._await_pending_clipboard_paste()
        dropdown = self.app.query_one(CompletionDropdown)
        selected = dropdown.get_selected()
        if selected is not None:
            self.value = self._prefix + f"{selected} "
            self.cursor_position = len(self.value)
            dropdown.hide()
            return
        await super().action_submit()

    def action_escape(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        dropdown.hide()

    def action_paste_clipboard(self) -> None:
        logger.info("action_paste_clipboard: atalho recebido pelo Textual")
        handler = self._clipboard_paste_handler
        if not callable(handler):
            logger.info("action_paste_clipboard: sem handler configurado")
            return
        self.run_worker(
            self._paste_clipboard_from_handler(handler),
            name="clipboard-paste",
            group="clipboard",
            exclusive=True,
        )

    async def _paste_clipboard_from_handler(self, handler: Callable[[], str | None]) -> None:
        """Lê o clipboard fora do event loop para não congelar a TUI.

        A leitura chama ``subprocess`` síncrono (imagem lê todos os bytes inline);
        rodá-la via ``asyncio.to_thread`` mantém o loop da UI responsivo.
        """
        payload = await asyncio.to_thread(handler)
        logger.info("action_paste_clipboard: payload lido? %s", bool(payload))
        if payload:
            self.insert_user_text(payload)
        else:
            self.app.notify(
                "Clipboard vazio ou sem ferramenta de leitura (instale wl-clipboard ou xclip)",
                title="Colar clipboard",
                severity="warning",
            )

    async def _await_pending_clipboard_paste(self) -> None:
        workers = [
            worker
            for worker in self.workers
            if worker.group == "clipboard" and not worker.is_finished
        ]
        if not workers:
            return
        for worker in workers:
            try:
                await worker.wait()
            except WorkerCancelled:
                continue

    def key_up(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        if dropdown.has_options:
            dropdown.select_prev()
            return
        if not self._prompt_history:
            return
        if self._history_index >= len(self._prompt_history):
            return
        if self._history_index == 0:
            self._saved_draft = self.user_value
        self._history_index += 1
        idx = len(self._prompt_history) - self._history_index
        self.value = self._prefix + self._prompt_history[idx]
        self.cursor_position = len(self.value)

    def key_down(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        if dropdown.has_options:
            dropdown.select_next()
            return
        if self._history_index == 0:
            return
        self._history_index -= 1
        if self._history_index == 0:
            self.value = self._prefix + self._saved_draft
        else:
            idx = len(self._prompt_history) - self._history_index
            self.value = self._prefix + self._prompt_history[idx]
        self.cursor_position = len(self.value)

    def key_tab(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        selected = dropdown.get_selected()
        if selected:
            self.value = self._prefix + f"{selected} "
            self.cursor_position = len(self.value)
            dropdown.hide()
            return

class _BreadcrumbWidget(Static):
    """Breadcrumb de delegação no header."""

class _SummarySpinner(Static):
    """Indicador discreto de resumo, separado do relógio."""

class _SummaryHeader(Header):
    """Header com breadcrumb, spinner próprio antes do relógio."""

    def compose(self) -> ComposeResult:
        yield HeaderIcon().data_bind(Header.icon)
        yield HeaderTitle()
        yield _BreadcrumbWidget("", id="breadcrumb")
        yield _SummarySpinner("", id="summary-spinner")
        yield (
            HeaderClock().data_bind(Header.time_format)
            if self._show_clock
            else HeaderClockSpace()
        )

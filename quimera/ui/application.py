"""QuimeraApplication — layout HSplit persistente via prompt_toolkit.

Passo A: estrutura completa do layout, sem conexão com o chat loop.
Passos B–D adicionam: saída do compositor, submit ao chat_queue, overlays
de approval/ask_user.

Uso (Passos B–C — conectado):
    qapp = QuimeraApplication(submit_fn=lambda text: chat_queue.put(text))
    # em outra thread: qapp.append_output("\\033[32mAgente diz oi\\033[0m\\n")
    qapp.run()

Uso (Passo D — com overlay):
    qapp.request_approval(question, timeout=180)   # bloqueia thread consumidora
    qapp.request_ask_user(question, options, timeout=180)
"""
from __future__ import annotations

import asyncio
import shutil
import threading
from dataclasses import dataclass, field
from typing import Callable

from prompt_toolkit import Application
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.filters import completion_is_selected, has_completions
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import DynamicContainer, Float, FloatContainer, HSplit, Window
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.widgets import HorizontalLine, TextArea
from prompt_toolkit.styles import Style

from ..app.prompt_input import _SlashCommandCompleter

_MAX_OUTPUT_LINES = 10_000


@dataclass
class _PendingPromptRequest:
    """Representa uma pergunta pendente de aprovação ou ask_user."""
    kind: str  # "approval" | "ask_user"
    question: str
    options: list[str] = field(default_factory=list)
    _result: list = field(default_factory=lambda: [None], init=False)
    _done: threading.Event = field(default_factory=threading.Event, init=False)

    def set_result(self, value: str | None) -> None:
        self._result[0] = value
        self._done.set()

    def wait(self, timeout: float | None = None) -> str | None:
        self._done.wait(timeout=timeout)
        return self._result[0]

    def is_done(self) -> bool:
        return self._done.is_set()


class QuimeraApplication:
    """Application prompt_toolkit com dock de input permanente.

    Layout (topo → base)
    --------------------
    output_window  — área de output do agente, rolável, ANSI-passthrough
    ──────────────── separador (1 linha)
    bottom_pane    — DynamicContainer:
        IDLE: TextArea de chat (submit_fn)
        AWAITING_*: label com pergunta + TextArea de resposta
    """

    def __init__(
        self,
        *,
        submit_fn: Callable[[str], None] | None = None,
        history=None,
        toolbar_context_resolver: Callable[[], dict] | None = None,
        command_resolver: Callable[[], list] | None = None,
        argument_resolver: Callable[[str, str], list] | None = None,
    ) -> None:
        self._submit_fn = submit_fn
        self._toolbar_context_resolver = toolbar_context_resolver
        self._output_text: str = ""
        self._output_lock = threading.Lock()
        self._stream_marks: dict[str, int] = {}
        self._app: Application | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._awaiting_response: bool = False

        # Overlay state
        self._dock_state: str = "idle"  # "idle" | "awaiting_input"
        self._pending_req: _PendingPromptRequest | None = None

        history = history or InMemoryHistory()
        completer = _SlashCommandCompleter(command_resolver, argument_resolver) if command_resolver else None

        # Chat input (IDLE state)
        self._input_area = TextArea(
            multiline=False,
            prompt=">>> ",
            history=history,
            accept_handler=self._on_submit,
            focusable=True,
            wrap_lines=True,
            completer=completer,
            complete_while_typing=False,
            auto_suggest=AutoSuggestFromHistory(),
        )

        # Overlay input (AWAITING_* state)
        self._overlay_input = TextArea(
            multiline=False,
            prompt="  > ",
            focusable=True,
            wrap_lines=False,
            accept_handler=self._on_overlay_submit,
        )

        # Output pane
        self._output_control = FormattedTextControl(
            self._get_output_fragments,
            focusable=False,
            show_cursor=False,
        )

        app_kb = self._build_app_key_bindings()

        self._toolbar_control = FormattedTextControl(
            self._get_toolbar_text,
            focusable=False,
            show_cursor=False,
        )

        layout = Layout(
            FloatContainer(
                content=HSplit([
                    Window(
                        content=self._output_control,
                        dont_extend_height=False,
                        wrap_lines=True,
                    ),
                    HorizontalLine(),
                    DynamicContainer(self._get_bottom_pane),
                    Window(
                        content=self._toolbar_control,
                        height=1,
                        dont_extend_height=True,
                        style="class:bottom-toolbar",
                    ),
                ]),
                floats=[
                    Float(
                        xcursor=True,
                        ycursor=True,
                        content=CompletionsMenu(max_height=12, scroll_offset=2),
                    ),
                ],
            ),
            focused_element=self._input_area,
        )

        self._app = Application(
            layout=layout,
            key_bindings=app_kb,
            full_screen=True,
            mouse_support=True,
            style=Style.from_dict({
                "bottom-toolbar": "bg:#252526",
                "toolbar.btn": "bg:#3e3e3e",
                "toolbar.btn.accent": "fg:#5fc3ff bold",
                "toolbar.btn.model": "fg:#9cdcfe",
                "toolbar.btn.info": "fg:#d4d4d4",
                "toolbar.btn.dim": "fg:#9e9e9e",
                "toolbar.btn.err": "fg:#fc7b5f bold",
            }),
        )

    # ------------------------------------------------------------------
    # Layout callbacks
    # ------------------------------------------------------------------

    def _get_bottom_pane(self):
        if self._dock_state != "idle":
            return HSplit([
                Window(
                    content=FormattedTextControl(self._get_overlay_label),
                    height=D(max=8),
                    wrap_lines=True,
                ),
                self._overlay_input,
            ])
        return self._input_area

    def _get_overlay_label(self):
        req = self._pending_req
        if req is None:
            return FormattedText([])
        fragments: list[tuple[str, str]] = []
        fragments.append(("bold", req.question + "\n"))
        if req.options:
            for i, opt in enumerate(req.options):
                fragments.append(("", f"  {i + 1}. {opt}\n"))
        if req.kind == "approval":
            fragments.append(("italic", "  [y=sim / n=não / a=todas · Ctrl-C=cancelar]\n"))
        else:
            fragments.append(("italic", "  [Enter para confirmar · Ctrl-C=cancelar]\n"))
        return FormattedText(fragments)

    def _get_output_fragments(self):
        with self._output_lock:
            text = self._output_text
        return to_formatted_text(ANSI(text)) if text else []

    def _get_toolbar_text(self):
        if self._dock_state != "idle":
            return FormattedText([
                ("class:toolbar.btn.accent", " Enter: confirmar "),
                ("", "  "),
                ("class:toolbar.btn.dim", " Ctrl+C: cancelar "),
            ])

        if self._awaiting_response:
            return FormattedText([
                ("", " "),
                ("class:toolbar.btn.accent", " ⟳ aguardando... "),
            ])

        resolver = self._toolbar_context_resolver
        if not callable(resolver):
            return FormattedText([("class:bottom-toolbar", " Enter: enviar  Ctrl+C: interromper  Ctrl+Q: sair ")])

        def _clip(value: str, max_len: int) -> str:
            if len(value) <= max_len:
                return value
            return value[:max_len - 1].rstrip() + "…"

        try:
            context = resolver() or {}
        except Exception:
            context = {}

        responder = str(context.get("responder", "")).strip()
        model = str(context.get("model", "")).strip()
        branch = str(context.get("branch", "")).strip()
        active_agents = str(context.get("active_agents", "")).strip()
        parallel = str(context.get("parallel", "")).strip()
        open_bugs = str(context.get("open_bugs", "")).strip()
        mode = str(context.get("mode", "")).strip()
        turns = str(context.get("turns", "")).strip()
        session_id = str(context.get("session", "")).strip()
        theme = str(context.get("theme", "")).strip()

        def _btn(text: str, style_cls: str) -> tuple:
            return (f"class:toolbar.{style_cls}", f" {text} ")

        left = []
        if responder:
            left.append(_btn(_clip(responder, 24), "btn.accent"))
        if model:
            left.append(_btn(_clip(model, 24), "btn.model"))
        if branch:
            left.append(_btn(f"⎇ {_clip(branch, 20)}", "btn.info"))
        if active_agents:
            left.append(_btn(f"⚙ {_clip(active_agents, 30)}", "btn.info"))
        if parallel:
            left.append(_btn(f"⚡ {parallel}", "btn.info"))

        right = []
        if open_bugs:
            right.append(_btn(f"✗ {open_bugs}", "btn.err"))
        if turns:
            right.append(_btn(f"↺ {turns}", "btn.dim"))
        if mode:
            right.append(_btn(f"◈ {mode}", "btn.dim"))
        if theme:
            right.append(_btn(f"✨ {_clip(theme, 12)}", "btn.dim"))
        if session_id:
            right.append(_btn(f"\U0001f517 {_clip(session_id, 22)}", "btn.dim"))

        if not left and not right:
            return FormattedText([("class:bottom-toolbar", " Enter: enviar  Ctrl+C: interromper  Ctrl+Q: sair ")])

        term_w = shutil.get_terminal_size(fallback=(80, 24)).columns
        left_visible = sum(len(t) for _, t in left) if left else 0
        right_visible = sum(len(t) for _, t in right) if right else 0
        padding = max(1, term_w - left_visible - right_visible) if right else 0

        fragments = [("", " ")]
        fragments.extend(left)
        if right:
            fragments.append(("", " " * max(0, padding - 1)))
        fragments.extend(right)
        return FormattedText(fragments)

    def _focus_input_area(self) -> None:
        """Foca o TextArea de chat — força o teclado virtual a reabrir no Android."""
        if self._app is not None and self._dock_state == "idle":
            try:
                self._app.layout.focus(self._input_area)
                self._app.invalidate()
            except Exception:
                pass

    def _on_submit(self, buffer) -> None:
        text = buffer.text.strip()
        if text and self._submit_fn is not None:
            self.append_output(f"\033[1;36m>>> \033[0m{text}\n")
            self._awaiting_response = True
            self._submit_fn(text)
        # Re-foca via call_soon para disparar evento de foco no Android
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon(self._focus_input_area)
        self.invalidate()

    def _on_overlay_submit(self, buffer) -> None:
        text = buffer.text.strip()
        if self._pending_req is not None:
            self._resolve_pending(text)

    def _resolve_pending(self, value: str | None) -> None:
        req = self._pending_req
        if req is None:
            return
        req.set_result(value)
        self._pending_req = None
        self._dock_state = "idle"
        self._overlay_input.text = ""
        if self._app is not None:
            try:
                self._app.layout.focus(self._input_area)
            except Exception:
                pass
            self._app.invalidate()

    def _build_app_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        def _ctrl_c(event):
            if self._dock_state != "idle":
                self._resolve_pending(None)
            else:
                event.app.exit()

        @kb.add("c-q")
        def _quit(event):
            if self._dock_state != "idle":
                self._resolve_pending(None)
            event.app.exit()

        # Quando há uma completion selecionada, Enter aplica ela (não submete).
        # eager=True garante prioridade sobre o accept_handler do TextArea.
        @kb.add("enter", filter=has_completions & completion_is_selected, eager=True)
        def _enter_apply_completion(event):
            buf = event.current_buffer
            state = buf.complete_state
            if state and state.current_completion:
                buf.apply_completion(state.current_completion)

        # Tab avança na lista; Shift+Tab recua — idêntico ao PromptSession.
        @kb.add("tab", filter=has_completions, eager=True)
        def _tab_next(event):
            event.current_buffer.complete_next()

        @kb.add("s-tab", filter=has_completions, eager=True)
        def _tab_prev(event):
            event.current_buffer.complete_previous()

        # Escape fecha o menu de completions sem submeter.
        @kb.add("escape", filter=has_completions, eager=True)
        def _escape_close(event):
            event.current_buffer.cancel_completion()

        return kb

    # ------------------------------------------------------------------
    # Public API (thread-safe)
    # ------------------------------------------------------------------

    def append_output(self, ansi_text: str) -> None:
        """Adiciona texto ANSI ao output pane (thread-safe)."""
        was_awaiting = self._awaiting_response
        self._awaiting_response = False
        with self._output_lock:
            self._output_text += ansi_text
            lines = self._output_text.split("\n")
            if len(lines) > _MAX_OUTPUT_LINES:
                self._output_text = "\n".join(lines[-_MAX_OUTPUT_LINES:])
        self.invalidate()
        # Re-foca ao receber primeira resposta do agente para reabrir teclado
        if was_awaiting and self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._focus_input_area)

    def mark_stream_start(self, agent: str) -> None:
        """Marca posição atual no output como início de stream do agente."""
        with self._output_lock:
            self._stream_marks[agent] = len(self._output_text)

    def replace_stream(self, agent: str, ansi_text: str) -> None:
        """Substitui o conteúdo desde o mark do agente pelo bloco formatado final."""
        was_awaiting = self._awaiting_response
        self._awaiting_response = False
        with self._output_lock:
            mark = self._stream_marks.pop(agent, None)
            if mark is not None and mark <= len(self._output_text):
                self._output_text = self._output_text[:mark] + ansi_text
            else:
                self._output_text += ansi_text
            lines = self._output_text.split("\n")
            if len(lines) > _MAX_OUTPUT_LINES:
                self._output_text = "\n".join(lines[-_MAX_OUTPUT_LINES:])
        self.invalidate()
        if was_awaiting and self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._focus_input_area)

    def invalidate(self) -> None:
        """Solicita redraw (thread-safe)."""
        if self._app is not None and self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._app.invalidate)

    def get_loop(self) -> asyncio.AbstractEventLoop | None:
        """Retorna o event loop do Application (disponível após run())."""
        return self._loop

    def run(self) -> None:
        """Bloqueia o main thread até o usuário sair (Ctrl-C ou Ctrl-Q)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._app.run_async())
        finally:
            self._loop = None
            loop.close()

    # ------------------------------------------------------------------
    # Passo D — Overlay de approval / ask_user
    # ------------------------------------------------------------------

    def _enter_overlay(self, req: _PendingPromptRequest) -> None:
        """Transition to overlay mode (must be called from non-UI thread)."""
        self._pending_req = req
        self._dock_state = "awaiting_input"

        def _focus_overlay():
            if self._app is None:
                return
            try:
                self._app.layout.focus(self._overlay_input)
            except Exception:
                pass
            self._app.invalidate()

        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(_focus_overlay)

    def _cleanup_overlay_after_timeout(self, req: _PendingPromptRequest) -> None:
        """Clean up overlay if the request timed out (result not set by user)."""
        if self._pending_req is not req:
            return
        self._pending_req = None
        self._dock_state = "idle"
        self._overlay_input.text = ""

        def _focus_chat():
            if self._app is None:
                return
            try:
                self._app.layout.focus(self._input_area)
            except Exception:
                pass
            self._app.invalidate()

        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(_focus_chat)

    def request_approval(self, question: str, timeout: float | None = None) -> str | None:
        """Exibe pergunta de approval no overlay e bloqueia até resposta ou timeout.

        Retorna a string digitada pelo usuário ("y"/"n"/"a"/etc) ou None
        se o usuário cancelou ou o timeout expirou.
        Deve ser chamado de thread consumidora (não do event loop do app).
        """
        req = _PendingPromptRequest(kind="approval", question=question)
        self._enter_overlay(req)
        result = req.wait(timeout=timeout)
        if not req.is_done():
            self._cleanup_overlay_after_timeout(req)
        return result

    def request_ask_user(
        self,
        question: str,
        options: list[str] | None = None,
        timeout: float | None = None,
    ) -> str | None:
        """Exibe pergunta ask_user no overlay e bloqueia até resposta ou timeout.

        Retorna a string digitada pelo usuário ou None se cancelado/timeout.
        Deve ser chamado de thread consumidora (não do event loop do app).
        """
        req = _PendingPromptRequest(
            kind="ask_user",
            question=question,
            options=list(options) if options else [],
        )
        self._enter_overlay(req)
        result = req.wait(timeout=timeout)
        if not req.is_done():
            self._cleanup_overlay_after_timeout(req)
        return result

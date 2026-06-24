"""Renderer-bound controller for agent window state."""
from __future__ import annotations

from typing import Any

from .events import LiveAbortEvent, LiveStartEvent, LiveStopEvent, LiveUpdateChunkEvent
from .windows import AgentWindowState, sanitize_window_text

_RENDER_MODES = {"plain", "markdown", "auto"}


def _normalize_render_mode(render_mode: str | None) -> str:
    mode = str(render_mode or "auto").strip().lower()
    if mode in _RENDER_MODES:
        return mode
    return "auto"


class AgentWindowController:
    """Renderer-bound behavior for a pure AgentWindowState."""

    def __init__(self, state: AgentWindowState):
        self.state = state

    def start_stream(self, renderer, theme_name: str) -> None:
        """Inicia streaming de output para este agente."""
        if self.state.streaming:
            return
        self.state.streaming = True
        self.state.stream_content = ""
        self.state.stream_theme_name = theme_name
        renderer._emit_ui_event(LiveStartEvent(self.state.agent))

    def update_stream(self, renderer, chunk: Any) -> None:
        """Enfileira chunk de streaming."""
        renderer._emit_ui_event(LiveUpdateChunkEvent(self.state.agent, chunk))

    def finish_stream(self, renderer, final_content: str, render_mode: str = "auto") -> None:
        """Finaliza streaming e persiste conteúdo completo."""
        clean = sanitize_window_text(str(final_content or ""))
        mode = _normalize_render_mode(render_mode)
        with renderer._lock:
            renderer._deck.remember_completed_stream(self.state.agent, clean)
        renderer._emit_ui_event(LiveStopEvent(self.state.agent, clean, mode))

    def abort_stream(self, renderer) -> None:
        """Aborta streaming sem marcar como completo."""
        renderer._emit_ui_event(LiveAbortEvent(self.state.agent))

    def ask_input(self, renderer, input_gate, prompt: str, timeout: float = 300.0) -> str | None:
        """Input livre de texto dentro deste agent window."""
        renderer.clear_agent_transient(self.state.agent)
        composed = self.state.compose_question(prompt)
        if self.state.streaming:
            renderer.set_agent_pending_input(self.state.agent, "ask", composed)
        renderer.flush_quick()

        try:
            return input_gate.read_input_in_terminal(
                composed + "\n",
                timeout,
                owner=self.state.agent,
            )
        finally:
            if self.state.streaming:
                renderer.clear_agent_pending_input(self.state.agent)

    def ask_approval(
        self,
        renderer,
        input_gate,
        question: str,
        prompt: str = "",
        timeout: float = 300.0,
    ) -> str | None:
        """Aprovação y/n/a dentro deste agent window."""
        renderer.clear_agent_transient(self.state.agent)
        composed = self.state.compose_question(question)
        if self.state.streaming:
            renderer.set_agent_pending_input(self.state.agent, "approval", composed)
        renderer.flush_quick()

        try:
            return input_gate.read_approval_in_terminal(
                composed,
                prompt,
                timeout,
                owner=self.state.agent,
            )
        finally:
            if self.state.streaming:
                renderer.clear_agent_pending_input(self.state.agent)

    def ask_selection(
        self,
        renderer,
        input_gate,
        question: str,
        options: list[str],
        timeout: float = 300.0,
    ) -> tuple[int, str] | None:
        """Seleção numerada dentro deste agent window."""
        renderer.clear_agent_transient(self.state.agent)
        renderer.flush_quick()
        return input_gate.read_selection_in_terminal(
            self.state.compose_question(question),
            options,
            timeout,
            owner=self.state.agent,
        )

"""Apresentação do stdout de agentes como eventos de spy."""

import quimera.plugins as plugins
from quimera.agent_events import SpyEvent
from quimera.constants import Visibility

class SpyOutputPresenter:
    """Converte stdout em eventos e aplica a política de visibilidade."""

    def __init__(self, renderer, visibility: Visibility):
        self.renderer = renderer
        self.visibility = visibility
        self.last_message: str | None = None
        self.pending_event: SpyEvent | None = None
        self.current_status_label = ""

    def compose_status_label(self, base_label: str) -> str:
        """Combina o rótulo base com o status transitório atual, sem perder contexto."""
        base = (base_label or "").strip()
        current = (self.current_status_label or "").strip()
        if not current:
            return base
        if not base or current == base:
            return current
        return f"{base} | {current}"

    def format_stdout(self, agent: str | None, line: str) -> list[SpyEvent]:
        """Converte stdout cru em eventos estruturados via plugin ou fallback."""
        if not agent:
            return []
        plugin = plugins.get(agent)
        formatter = getattr(plugin, "spy_stdout_formatter", None) if plugin else None
        if callable(formatter):
            return formatter(line)

        text = line.strip()
        if not text:
            return []
        if len(text) > 200:
            text = text[:197] + "..."
        return [SpyEvent(kind="raw", text=text)]

    def consume_stdout(self, agent: str | None, line: str) -> bool:
        """Processa e emite uma linha de stdout."""
        events = self.format_stdout(agent, line)
        for event in events:
            self.emit(agent, event)
        return bool(events)

    def emit(self, agent: str | None, event: SpyEvent) -> None:
        """Renderiza um evento conforme a visibilidade configurada."""
        if self.visibility != Visibility.SUMMARY:
            self._show(agent, event)
            return

        if event.kind == "clear":
            self.flush(agent)
            return

        if event.kind == "tool":
            self.flush(agent)
            payload = event.text.strip()
            if payload.startswith("✗ "):
                self._show(agent, event)
                self.current_status_label = ""
                return

            if payload.startswith("✓ "):
                self.current_status_label = ""
                return

            self.current_status_label = payload
            return

        if event.kind == "context":
            self.current_status_label = event.text
            return

        if event.kind == "diff":
            self.flush(agent)
            self._show(agent, event)
            return

        if event.kind != "response":
            self.flush(agent)
            self._show(agent, event)
            return

        if not event.text.strip():
            return

        self.flush(agent)
        self._show(agent, event)

    def flush(self, agent: str | None) -> None:
        """Emite o evento agrupado pendente, se existir."""
        if self.pending_event is None:
            return
        self._show(agent, self.pending_event)
        self.pending_event = None

    def reset(self) -> None:
        """Limpa estado interno entre execuções."""
        self.last_message = None
        self.pending_event = None
        self.current_status_label = ""

    def _show(self, agent: str | None, event: SpyEvent) -> None:
        """Renderiza um evento já processado, evitando duplicatas consecutivas."""
        rendered = event.text
        dedupe_key = f"{event.kind}:{rendered}"
        if event.kind != "clear" and dedupe_key == self.last_message:
            return
        self.renderer.show_plain(rendered, agent=agent)
        self.last_message = dedupe_key

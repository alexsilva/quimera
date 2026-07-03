"""Modelo lógico do feed da UI Textual.

Este módulo não depende de Textual nem de widgets. Ele recebe eventos semânticos
e decide como o scrollback lógico deve mudar: anexar itens persistentes,
substituir estados transitórios, acumular stream e limpar previews.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from quimera.ui.text import (
    _apply_stream_diff,
    _normalize_stream_diff,
    strip_ansi,
)
from quimera.ui.textual.events import TextualUiEvent


class AgentLifecycleStatus(str, Enum):
    """Status estruturado de lifecycle de agente no feed Textual."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"
    ABORTED = "aborted"
    RETRYING = "retrying"
    RECONNECTING = "reconnecting"


RUN_BOUNDARY_LIFECYCLE_STATUSES = frozenset({
    AgentLifecycleStatus.COMPLETED,
    AgentLifecycleStatus.FAILED,
    AgentLifecycleStatus.ERROR,
    AgentLifecycleStatus.CANCELLED,
    AgentLifecycleStatus.ABORTED,
    AgentLifecycleStatus.RETRYING,
    AgentLifecycleStatus.RECONNECTING,
})


def _coerce_lifecycle_status(value: object) -> AgentLifecycleStatus:
    """Normaliza status de lifecycle para tipo estruturado."""
    if isinstance(value, AgentLifecycleStatus):
        return value
    normalized = str(value or "").strip().lower()
    aliases = {
        "done": AgentLifecycleStatus.COMPLETED,
        "finished": AgentLifecycleStatus.COMPLETED,
        "complete": AgentLifecycleStatus.COMPLETED,
        "completed": AgentLifecycleStatus.COMPLETED,
        "running": AgentLifecycleStatus.RUNNING,
        "failed": AgentLifecycleStatus.FAILED,
        "failure": AgentLifecycleStatus.FAILED,
        "error": AgentLifecycleStatus.ERROR,
        "cancelled": AgentLifecycleStatus.CANCELLED,
        "canceled": AgentLifecycleStatus.CANCELLED,
        "aborted": AgentLifecycleStatus.ABORTED,
        "retrying": AgentLifecycleStatus.RETRYING,
        "reconnecting": AgentLifecycleStatus.RECONNECTING,
    }
    return aliases.get(normalized, AgentLifecycleStatus.RUNNING)


def _agent_lifecycle_payload(
    message: str,
    *,
    status: AgentLifecycleStatus | str = AgentLifecycleStatus.RUNNING,
) -> dict[str, str]:
    """Cria payload estruturado de lifecycle para a UI."""
    normalized_status = _coerce_lifecycle_status(status)
    return {"status": normalized_status.value, "message": str(message or "")}


@dataclass
class TextualFeedItem:
    """Item lógico do feed Textual."""

    event: TextualUiEvent
    transient: bool = False


@dataclass(frozen=True)
class TextualFeedChange:
    """Resultado da aplicação de um evento no feed Textual."""

    changed: bool
    redraw: bool = False
    appended: TextualFeedItem | None = None


class TextualFeedModel:
    """Modelo testável do feed: transitórios por agente são substituíveis."""

    _TRANSIENT_KINDS = {"stream_start", "stream_chunk", "stream_abort", "agent_update", "agent_lifecycle", "pending_input"}

    _IGNORED_KINDS = {
        "prompt",
        "prompt_clear",
        "input_active",
        "summarizing",
        "window_open",
        "window_clear",
        "theme_changed",
    }

    def __init__(self) -> None:
        self._items: list[TextualFeedItem] = []
        self._transient_index_by_agent: dict[str, int] = {}
        self._stream_buffer_by_agent: dict[str, str] = {}
        self._stream_meta_by_agent: dict[str, dict[str, Any]] = {}
        self._transient_tools_by_agent: dict[str, list[str]] = {}
        self._finalized_agents: set[str] = set()
        self._last_change = TextualFeedChange(False)

    @property
    def items(self) -> list[TextualFeedItem]:
        """Snapshot dos itens atuais do feed."""
        return list(self._items)

    @property
    def last_change(self) -> TextualFeedChange:
        """Última mudança aplicada ao feed."""
        return self._last_change

    def clear(self) -> None:
        """Limpa estado do feed."""
        self._items.clear()
        self._transient_index_by_agent.clear()
        self._stream_buffer_by_agent.clear()
        self._stream_meta_by_agent.clear()
        self._transient_tools_by_agent.clear()
        self._finalized_agents.clear()
        self._last_change = TextualFeedChange(True, redraw=True)

    def apply(self, event: TextualUiEvent) -> bool:
        """Aplica evento e retorna se o feed visual precisa ser redesenhado."""
        self._last_change = TextualFeedChange(False)
        if event.kind in self._IGNORED_KINDS:
            return False
        if event.kind in {"question", "question_clear"}:
            return False
        if event.kind == "visual_reset":
            return self._apply_visual_reset(event)
        if event.kind == "agent_message":
            replaced = self._replace_transient_with_final(event)
            self._last_change = TextualFeedChange(True, redraw=replaced, appended=None if replaced else self._items[-1])
            return True
        if event.kind == "stream_start":
            agent = self._agent_key(event)
            self._finalized_agents.discard(agent)
            self._transient_tools_by_agent.pop(agent, None)
            self._stream_buffer_by_agent[agent] = ""
            self._stream_meta_by_agent[agent] = dict(event.payload or {}) if isinstance(event.payload, dict) else {}
            replaced = self._upsert_transient(event)
            self._last_change = TextualFeedChange(True, redraw=replaced, appended=None if replaced else self._items[-1])
            return True
        if event.kind == "stream_chunk":
            return self._apply_stream_chunk(event)
        if event.kind == "stream_abort":
            self._transient_tools_by_agent.pop(self._agent_key(event), None)
        if event.kind == "tool_preview":
            return self._apply_tool_preview(event)
        if event.kind in self._TRANSIENT_KINDS:
            if self._is_late_completed_lifecycle(event):
                return False
            if self._is_run_boundary_lifecycle(event):
                self._transient_tools_by_agent.pop(self._agent_key(event), None)
            replaced = self._upsert_transient(event)
            self._last_change = TextualFeedChange(True, redraw=replaced, appended=None if replaced else self._items[-1])
            return True
        item = TextualFeedItem(event, transient=False)
        self._items.append(item)
        self._last_change = TextualFeedChange(True, appended=item)
        return True

    def _agent_key(self, event: TextualUiEvent) -> str:
        return str(event.agent or "__global__")

    def _upsert_transient(self, event: TextualUiEvent) -> bool:
        agent = self._agent_key(event)
        item = TextualFeedItem(self._with_transient_tools(event), transient=True)
        index = self._transient_index_by_agent.get(agent)
        if index is not None and 0 <= index < len(self._items):
            self._items[index] = item
            return True
        self._transient_index_by_agent[agent] = len(self._items)
        self._items.append(item)
        return False

    def _replace_transient_with_final(self, event: TextualUiEvent) -> bool:
        agent = self._agent_key(event)
        self._stream_buffer_by_agent.pop(agent, None)
        self._stream_meta_by_agent.pop(agent, None)
        self._transient_tools_by_agent.pop(agent, None)
        self._finalized_agents.add(agent)
        item = TextualFeedItem(event, transient=False)
        index = self._transient_index_by_agent.pop(agent, None)
        if index is not None and 0 <= index < len(self._items):
            self._items[index] = item
            return True
        self._items.append(item)
        return False

    def _is_late_completed_lifecycle(self, event: TextualUiEvent) -> bool:
        if event.kind != "agent_lifecycle":
            return False
        agent = self._agent_key(event)
        if agent not in self._finalized_agents:
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        return _coerce_lifecycle_status(payload.get("status")) is AgentLifecycleStatus.COMPLETED

    @staticmethod
    def _is_run_boundary_lifecycle(event: TextualUiEvent) -> bool:
        """Retorna True quando lifecycle encerra a execução transitória anterior."""
        if event.kind != "agent_lifecycle":
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        return _coerce_lifecycle_status(payload.get("status")) in RUN_BOUNDARY_LIFECYCLE_STATUSES

    def _apply_stream_chunk(self, event: TextualUiEvent) -> bool:
        agent = self._agent_key(event)
        current = self._stream_buffer_by_agent.get(agent, "")
        payload = event.payload
        if isinstance(payload, dict):
            diff = _normalize_stream_diff(payload.get("diff"))
            if diff:
                current = _apply_stream_diff(current, diff)
            elif payload.get("text"):
                current += strip_ansi(str(payload.get("text")))
            else:
                current += strip_ansi(str(payload))
        else:
            current += strip_ansi(str(payload))
        self._stream_buffer_by_agent[agent] = current
        if current.strip():
            payload: Any = current
            meta = self._stream_meta_by_agent.get(agent)
            if meta:
                payload = {**meta, "content": current}
            replaced = self._upsert_transient(TextualUiEvent("stream_chunk", payload, agent=event.agent))
            self._last_change = TextualFeedChange(True, redraw=replaced, appended=None if replaced else self._items[-1])
            return True
        self._last_change = TextualFeedChange(False)
        return False

    def _apply_visual_reset(self, event: TextualUiEvent) -> bool:
        """Remove estado visual transitório sem apagar mensagens persistentes."""
        agent = str(event.agent or "").strip()
        if agent:
            index = self._transient_index_by_agent.pop(agent, None)
            self._stream_buffer_by_agent.pop(agent, None)
            self._stream_meta_by_agent.pop(agent, None)
            self._transient_tools_by_agent.pop(agent, None)
            if index is None or not (0 <= index < len(self._items)):
                self._last_change = TextualFeedChange(False)
                return False
            del self._items[index]
            self._reindex_transients()
            self._last_change = TextualFeedChange(True, redraw=True)
            return True

        before = len(self._items)
        self._items = [item for item in self._items if not item.transient]
        self._transient_index_by_agent.clear()
        self._stream_buffer_by_agent.clear()
        self._stream_meta_by_agent.clear()
        self._transient_tools_by_agent.clear()
        changed = len(self._items) != before
        self._last_change = TextualFeedChange(changed, redraw=changed)
        return changed

    def _reindex_transients(self) -> None:
        self._transient_index_by_agent.clear()
        for index, item in enumerate(self._items):
            if item.transient:
                self._transient_index_by_agent[self._agent_key(item.event)] = index

    def _with_transient_tools(self, event: TextualUiEvent) -> TextualUiEvent:
        """Anexa previews de tools ao evento transitório do agente."""
        agent = self._agent_key(event)
        tool_lines = self._transient_tools_by_agent.get(agent)
        if not tool_lines:
            return event
        payload = event.payload
        if isinstance(payload, dict):
            merged = dict(payload)
        else:
            merged = {"content": str(payload or "")}
        merged["tools"] = list(tool_lines)
        return TextualUiEvent(event.kind, merged, agent=event.agent)

    def _apply_tool_preview(self, event: TextualUiEvent) -> bool:
        """Atualiza previews de tools dentro do bloco transitório do agente."""
        agent = self._agent_key(event)
        content = strip_ansi(str(event.payload or "")).strip()
        if not content:
            self._last_change = TextualFeedChange(False)
            return False
        lines = self._transient_tools_by_agent.setdefault(agent, [])
        lines.append(content)
        if len(lines) > 12:
            del lines[:-12]
        index = self._transient_index_by_agent.get(agent)
        if index is None or not (0 <= index < len(self._items)):
            replaced = self._upsert_transient(TextualUiEvent("agent_update", {"content": "", "tools": list(lines)}, agent=event.agent))
            self._last_change = TextualFeedChange(True, redraw=replaced, appended=None if replaced else self._items[-1])
            return True
        current_event = self._items[index].event
        self._items[index] = TextualFeedItem(self._with_transient_tools(current_event), transient=True)
        self._last_change = TextualFeedChange(True, redraw=True)
        return True

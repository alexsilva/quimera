"""Modelo lógico do feed da UI Textual.

Este módulo não depende de Textual nem de widgets. Ele recebe eventos semânticos
e decide como o scrollback lógico deve mudar: anexar itens persistentes,
substituir estados transitórios, acumular stream e limpar previews.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from quimera.constants import USER_ROLE
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

    def hydrate_from_history(
            self,
            messages: list,
            *,
            user_label: str = ">>>",
            agent_resolver: Callable[[str], tuple[str, str] | None] | None = None,
    ) -> bool:
        """Reconstrói itens persistentes do feed a partir do histórico salvo."""
        hydrated: list[TextualFeedItem] = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip()
            content = strip_ansi(str(message.get("content") or "")).strip("\r\n")
            if not role or not content.strip():
                continue
            if role == USER_ROLE:
                hydrated.append(
                    TextualFeedItem(
                        TextualUiEvent(
                            "user_message",
                            {
                                "content": content,
                                "label": user_label,
                                "style": "green",
                                "render_mode": "plain",
                            },
                        ),
                        transient=False,
                    )
                )
                continue
            style = "cyan"
            label = role
            if callable(agent_resolver):
                try:
                    resolved = agent_resolver(role)
                except Exception:
                    resolved = None
                if resolved:
                    style, label = str(resolved[0] or "cyan"), str(resolved[1] or role)
            hydrated.append(
                TextualFeedItem(
                    TextualUiEvent(
                        "agent_message",
                        {
                            "content": content,
                            "label": label,
                            "style": style,
                            "render_mode": "auto",
                        },
                        agent=role,
                    ),
                    transient=False,
                )
            )
        if not hydrated:
            self._last_change = TextualFeedChange(False)
            return False
        self._items.extend(hydrated)
        self._last_change = TextualFeedChange(True, redraw=True)
        return True

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
            agent = self._agent_key(event)
            if self._is_finalized_agent(agent):
                self._last_change = TextualFeedChange(False)
                return False
            self._transient_tools_by_agent.pop(agent, None)
        if event.kind == "tool_preview":
            return self._apply_tool_preview(event)
        if event.kind in self._TRANSIENT_KINDS:
            agent = self._agent_key(event)
            if self._is_finalized_agent(agent):
                # agent_update e lifecycle RUNNING sinalizam nova run — descarta estado finalizado.
                # Agentes CLI (opencode etc.) não emitem stream_start, então não chegam ao discard
                # acima; este bloco equivalente evita que a segunda run fique invisível.
                is_new_run_signal = event.kind == "agent_update" or (
                    event.kind == "agent_lifecycle"
                    and isinstance(event.payload, dict)
                    and _coerce_lifecycle_status(event.payload.get("status")) == AgentLifecycleStatus.RUNNING
                )
                if is_new_run_signal:
                    self._finalized_agents.discard(agent)
                    self._transient_tools_by_agent.pop(agent, None)
                else:
                    self._last_change = TextualFeedChange(False)
                    return False
            if self._is_run_boundary_lifecycle(event):
                self._transient_tools_by_agent.pop(agent, None)
                if self._is_final_lifecycle(event):
                    self._finalized_agents.add(agent)
                    removed = self._remove_transient_keys([agent])
                    self._last_change = TextualFeedChange(removed, redraw=removed)
                    return removed
            replaced = self._upsert_transient(event)
            self._last_change = TextualFeedChange(True, redraw=replaced, appended=None if replaced else self._items[-1])
            return True
        item = TextualFeedItem(event, transient=False)
        self._items.append(item)
        self._last_change = TextualFeedChange(True, appended=item)
        return True

    def _agent_key(self, event: TextualUiEvent) -> str:
        payload = event.payload if isinstance(event.payload, dict) else {}
        delegation_id = str(payload.get("delegation_id") or "").strip()
        base = str(event.agent or "__global__")
        return f"{base}#{delegation_id}" if delegation_id else base

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

    def _is_finalized_agent(self, agent: str) -> bool:
        return agent in self._finalized_agents

    @staticmethod
    def _is_final_lifecycle(event: TextualUiEvent) -> bool:
        if event.kind != "agent_lifecycle":
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        status = _coerce_lifecycle_status(payload.get("status"))
        message = str(payload.get("message") or "").lower()
        if status is AgentLifecycleStatus.FAILED and ("reconect" in message or "tentativa" in message):
            return False
        return status in {
            AgentLifecycleStatus.COMPLETED,
            AgentLifecycleStatus.FAILED,
            AgentLifecycleStatus.ERROR,
            AgentLifecycleStatus.CANCELLED,
            AgentLifecycleStatus.ABORTED,
        }

    @staticmethod
    def _is_run_boundary_lifecycle(event: TextualUiEvent) -> bool:
        """Retorna True quando lifecycle encerra a execução transitória anterior."""
        if event.kind != "agent_lifecycle":
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        return _coerce_lifecycle_status(payload.get("status")) in RUN_BOUNDARY_LIFECYCLE_STATUSES

    def _apply_stream_chunk(self, event: TextualUiEvent) -> bool:
        agent = self._agent_key(event)
        if self._is_finalized_agent(agent):
            self._last_change = TextualFeedChange(False)
            return False
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
            agent_prefix = f"{agent}#"
            keys = [
                key
                for key in set(
                    self._transient_index_by_agent
                    | self._stream_buffer_by_agent
                    | self._stream_meta_by_agent
                    | self._transient_tools_by_agent
                )
                if key == agent or key.startswith(agent_prefix)
            ]
            removed = self._remove_transient_keys(keys)
            if not removed:
                self._last_change = TextualFeedChange(False)
                return False
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

    def _remove_transient_keys(self, keys: list[str]) -> bool:
        indexes = sorted(
            {
                index
                for key in keys
                if (index := self._transient_index_by_agent.pop(key, None)) is not None
                and 0 <= index < len(self._items)
            },
            reverse=True,
        )
        for key in keys:
            self._stream_buffer_by_agent.pop(key, None)
            self._stream_meta_by_agent.pop(key, None)
            self._transient_tools_by_agent.pop(key, None)
        if not indexes:
            return False
        for index in indexes:
            del self._items[index]
        self._reindex_transients()
        return True

    def _reindex_transients(self) -> None:
        self._transient_index_by_agent.clear()
        for index, item in enumerate(self._items):
            if item.transient:
                self._transient_index_by_agent[self._agent_key(item.event)] = index

    @staticmethod
    def _tool_preview_subject(content: str) -> str:
        """Deriva a identidade de uma tool a partir da linha de preview.

        Remove o marcador de status inicial ("$", "✓", "✗", "⌘") e a anotação
        final "(exit N)" para que as linhas de início e conclusão de um mesmo
        comando/ferramenta compartilhem o mesmo identificador.
        """
        text = str(content or "").strip()
        if not text:
            return ""
        for marker in ("$ ", "✓ ", "✗ ", "⌘ "):
            if text.startswith(marker):
                text = text[len(marker):].strip()
                break
        text = re.sub(r"\s*\(exit\s+-?\d+\)\s*$", "", text).strip()
        return text

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
        if self._is_finalized_agent(agent):
            self._last_change = TextualFeedChange(False)
            return False
        content = strip_ansi(str(event.payload or "")).strip()
        if not content:
            self._last_change = TextualFeedChange(False)
            return False
        lines = self._transient_tools_by_agent.setdefault(agent, [])
        # Uma mesma tool costuma emitir uma linha de início ("$ cmd") e outra de
        # conclusão ("✓ cmd"/"✗ cmd (exit N)"). Em vez de acumular as duas —
        # duplicando a saída no feed — atualizamos a linha existente do mesmo
        # comando no lugar, refletindo a transição running → concluído.
        subject = self._tool_preview_subject(content)
        replaced_line = False
        if subject:
            for idx in range(len(lines) - 1, -1, -1):
                if self._tool_preview_subject(lines[idx]) == subject:
                    lines[idx] = content
                    replaced_line = True
                    break
        if not replaced_line:
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

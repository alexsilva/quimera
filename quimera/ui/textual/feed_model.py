"""Modelo lógico do feed da UI Textual.

Este módulo não depende de Textual nem de widgets. Ele recebe eventos semânticos
e decide como o scrollback lógico deve mudar: anexar itens persistentes,
substituir estados transitórios, acumular stream e limpar previews.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from quimera.constants import USER_ROLE
from quimera.ui.messages import AGENT_EXECUTION_STARTED_MESSAGE
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
    _MCP_HTTP_TOOL_MARKER = "◇"

    _IGNORED_KINDS = {
        "prompt",
        "prompt_clear",
        "input_active",
        "summarizing",
        "window_open",
        "window_clear",
        "theme_changed",
    }

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        tool_preview_dwell_seconds: float = 15.0,
    ) -> None:
        self._clock = clock
        self._tool_preview_dwell_seconds = max(0.0, float(tool_preview_dwell_seconds))
        self._items: list[TextualFeedItem] = []
        self._transient_index_by_agent: dict[str, int] = {}
        self._stream_buffer_by_agent: dict[str, str] = {}
        self._stream_meta_by_agent: dict[str, dict[str, Any]] = {}
        self._transient_tools_by_agent: dict[str, list[str]] = {}
        self._tool_request_lines_by_agent: dict[str, dict[str, str]] = {}
        self._tool_request_seen_at_by_agent: dict[str, dict[str, float]] = {}
        self._tool_request_expiry_by_agent: dict[str, dict[str, float]] = {}
        self._completed_tool_requests_by_agent: dict[str, set[str]] = {}
        self._mcp_http_tool_stats_by_agent: dict[str, dict[str, int]] = {}
        self._pending_turn_summary_by_agent: dict[str, TextualUiEvent] = {}
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
        self._tool_request_lines_by_agent.clear()
        self._tool_request_seen_at_by_agent.clear()
        self._tool_request_expiry_by_agent.clear()
        self._completed_tool_requests_by_agent.clear()
        self._mcp_http_tool_stats_by_agent.clear()
        self._pending_turn_summary_by_agent.clear()
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
            replaced, agent_key = self._replace_transient_with_final(event)
            summary = self._pending_turn_summary_by_agent.pop(agent_key, None)
            if summary is not None:
                self._items.append(TextualFeedItem(summary, transient=False))
                self._last_change = TextualFeedChange(True, redraw=True)
            else:
                self._last_change = TextualFeedChange(
                    True,
                    redraw=replaced,
                    appended=None if replaced else self._items[-1],
                )
            return True
        if event.kind == "stream_start":
            agent = self._agent_key(event)
            self._finalized_agents.discard(agent)
            self._transient_tools_by_agent.pop(agent, None)
            self._pending_turn_summary_by_agent.pop(agent, None)
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
        if event.kind == "tool_state":
            return self._apply_tool_state(event)
        if event.kind == "delegation":
            removed_preview = self._consume_delegate_tool_preview(event)
            item = TextualFeedItem(event, transient=False)
            self._items.append(item)
            self._last_change = TextualFeedChange(
                True,
                redraw=removed_preview,
                appended=None if removed_preview else item,
            )
            return True
        if event.kind == "turn_summary":
            agent = self._agent_key(event)
            if not self._is_finalized_agent(agent):
                self._pending_turn_summary_by_agent[agent] = event
                self._last_change = TextualFeedChange(False)
                return False
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
                    self._pending_turn_summary_by_agent.pop(agent, None)
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
        run_id = str(payload.get("run_id") or "").strip()
        delegation_id = str(payload.get("delegation_id") or "").strip()
        base = str(event.agent or "__global__")
        transport = str(payload.get("transport") or "").strip()
        if transport == "mcp_http":
            # Sessão e run não são identidade visual estável para MCP HTTP:
            # alguns clientes recriam ambos entre chamadas. O clientInfo do
            # handshake representa o cliente lógico e separa consumidores
            # distintos sem fragmentar as tools do mesmo consumidor.
            client_name = str(payload.get("client_name") or "").strip().lower()
            client_version = str(payload.get("client_version") or "").strip().lower()
            if client_name:
                client_key = f"{client_name}@{client_version}" if client_version else client_name
                return f"{base}#mcp-client:{client_key}"
            return base
        if run_id:
            return f"{base}#run:{run_id}"
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

    def _replace_transient_with_final(self, event: TextualUiEvent) -> tuple[bool, str]:
        agent = self._final_replacement_key(event)
        self._stream_buffer_by_agent.pop(agent, None)
        self._stream_meta_by_agent.pop(agent, None)
        self._transient_tools_by_agent.pop(agent, None)
        self._finalized_agents.add(agent)
        item = TextualFeedItem(event, transient=False)
        index = self._transient_index_by_agent.pop(agent, None)
        if index is not None and 0 <= index < len(self._items):
            self._items[index] = item
            return True, agent
        self._items.append(item)
        return False, agent

    def _final_replacement_key(self, event: TextualUiEvent) -> str:
        preferred = self._agent_key(event)
        if preferred in self._transient_index_by_agent:
            return preferred
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("run_id") or payload.get("delegation_id"):
            return preferred
        base = str(event.agent or "__global__")
        prefix = f"{base}#"
        candidates = [
            key
            for key in self._transient_index_by_agent
            if key == base or key.startswith(prefix)
        ]
        return candidates[0] if len(candidates) == 1 else preferred

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
            if isinstance(event.payload, dict):
                stream_payload = event.payload
                runtime_meta = {
                    key: stream_payload[key]
                    for key in (
                        "run_id",
                        "parent_run_id",
                        "delegation_id",
                        "transport",
                        "label",
                        "style",
                        "theme",
                        "orchestrator",
                    )
                    if key in stream_payload
                }
                meta = {**runtime_meta, **(meta or {})}
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
            payload = event.payload if isinstance(event.payload, dict) else {}
            if payload.get("run_id") or payload.get("delegation_id"):
                keys = [self._agent_key(event)]
            else:
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
        self._tool_request_lines_by_agent.clear()
        self._tool_request_seen_at_by_agent.clear()
        self._tool_request_expiry_by_agent.clear()
        self._completed_tool_requests_by_agent.clear()
        self._mcp_http_tool_stats_by_agent.clear()
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
            self._tool_request_lines_by_agent.pop(key, None)
            self._tool_request_seen_at_by_agent.pop(key, None)
            self._tool_request_expiry_by_agent.pop(key, None)
            self._completed_tool_requests_by_agent.pop(key, None)
            self._mcp_http_tool_stats_by_agent.pop(key, None)
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
        markers = ("◇ ", "$ ", "✓ ", "✗ ", "⌘ ", "⚒ ")
        changed = True
        while changed:
            changed = False
            for marker in markers:
                if text.startswith(marker):
                    text = text[len(marker):].strip()
                    changed = True
                    break
        text = re.sub(r"\s*\(exit\s+-?\d+\)\s*$", "", text).strip()
        return text

    @classmethod
    def _tool_preview_content(cls, event: TextualUiEvent) -> str:
        payload = event.payload if isinstance(event.payload, dict) else {}
        content_source = payload.get("content") if payload else event.payload
        content = strip_ansi(str(content_source or "")).strip()
        transport = str(payload.get("transport") or "").strip()
        if content and transport == "mcp_http":
            marker = f"{cls._MCP_HTTP_TOOL_MARKER} "
            if not content.startswith(marker):
                content = f"{marker}{content}"
        return content

    @staticmethod
    def _tool_preview_tool_name(subject: str) -> str:
        """Extrai o nome canônico da tool de uma linha de preview.

        Normaliza o prefixo genérico "usando " e o namespace MCP ("quimera_")
        para que a linha genérica emitida pelo CLI e a linha rica emitida pelo
        executor de tools refiram-se à mesma identidade.
        """
        text = str(subject or "").strip()
        if text.startswith("usando "):
            text = text[7:].strip()
        token = text.split(" ", 1)[0].strip().strip(":")
        for prefix in ("mcp__quimera__", "quimera_"):
            if token.startswith(prefix):
                token = token[len(prefix):]
                break
        return token

    def _merge_generic_tool_line(self, lines: list[str], content: str, subject: str) -> bool:
        """Funde a linha genérica "usando X" com a linha rica da mesma tool.

        A mesma chamada chega por dois canais (stdout do CLI e preview do
        executor); manter as duas duplica o feed. Retorna True quando a linha
        nova foi absorvida por uma existente (descartada ou substituindo-a).
        """
        tool_name = self._tool_preview_tool_name(subject)
        if not tool_name:
            return False
        new_is_generic = str(content).strip().startswith("usando ")
        for idx in range(len(lines) - 1, -1, -1):
            existing_subject = self._tool_preview_subject(lines[idx])
            if self._tool_preview_tool_name(existing_subject) != tool_name:
                continue
            if new_is_generic:
                return True
            if existing_subject.startswith("usando "):
                lines[idx] = content
                return True
            return False
        return False

    def _consume_delegate_tool_preview(self, event: TextualUiEvent) -> bool:
        """Remove a preview genérica quando o cartão rico da delegação chega."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        task = str(payload.get("task") or "").strip()
        task_prefix = task[:80]
        candidates: list[tuple[bool, int, str, int]] = []
        for agent, lines in self._transient_tools_by_agent.items():
            item_index = self._transient_index_by_agent.get(agent, -1)
            for line_index in range(len(lines) - 1, -1, -1):
                line = lines[line_index]
                subject = self._tool_preview_subject(line)
                if self._tool_preview_tool_name(subject) != "delegate":
                    continue
                matches_task = not task_prefix or task_prefix in line
                candidates.append((matches_task, item_index, agent, line_index))
                break
        if not candidates:
            return False
        matching = [candidate for candidate in candidates if candidate[0]]
        _, item_index, agent, line_index = max(
            matching or candidates,
            key=lambda candidate: candidate[1],
        )
        lines = self._transient_tools_by_agent[agent]
        del lines[line_index]
        if not lines:
            self._transient_tools_by_agent.pop(agent, None)
        if 0 <= item_index < len(self._items):
            current_item = self._items[item_index]
            current_payload = current_item.event.payload
            if isinstance(current_payload, dict):
                clean_payload = dict(current_payload)
                clean_payload.pop("tools", None)
            else:
                clean_payload = current_payload
            clean_event = TextualUiEvent(
                current_item.event.kind,
                clean_payload,
                agent=current_item.event.agent,
            )
            self._items[item_index] = TextualFeedItem(
                self._with_transient_tools(clean_event),
                transient=True,
            )
        return True

    def _with_transient_tools(self, event: TextualUiEvent) -> TextualUiEvent:
        """Anexa previews de tools ao estado transitório do agente."""
        agent = self._agent_key(event)
        tool_lines = self._transient_tools_by_agent.get(agent)
        if not tool_lines:
            return event
        payload = event.payload
        if isinstance(payload, dict):
            merged = dict(payload)
        else:
            merged = {"content": str(payload or "")}
        if self._is_mcp_http_payload(merged):
            for key in (
                "tool_total",
                "tool_ok_count",
                "tool_err_count",
                "tool_duration_ms",
            ):
                merged.pop(key, None)
            stats = self._mcp_http_tool_stats_by_agent.get(agent)
            if stats:
                merged.update(
                    {
                        "tool_total": stats["total"],
                        "tool_ok_count": stats["ok_count"],
                        "tool_err_count": stats["err_count"],
                        "tool_duration_ms": stats["duration_ms"],
                    }
                )
        visible_tools = tool_lines[-12:]
        hidden_count = len(tool_lines) - len(visible_tools)
        if hidden_count > 0:
            visible_tools = [
                f"⋮ +{hidden_count} ferramentas anteriores",
                *visible_tools,
            ]
        merged["tools"] = visible_tools
        return TextualUiEvent(event.kind, merged, agent=event.agent)

    @staticmethod
    def _is_lifecycle_placeholder_event(event: TextualUiEvent) -> bool:
        """Retorna True para placeholders que não devem virar cabeçalho das tools."""
        if event.kind != "agent_lifecycle":
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        return payload.get("message") == AGENT_EXECUTION_STARTED_MESSAGE

    def _apply_tool_preview(self, event: TextualUiEvent) -> bool:
        """Atualiza previews de tools dentro do bloco transitório do agente."""
        agent = self._agent_key(event)
        if self._is_finalized_agent(agent):
            self._last_change = TextualFeedChange(False)
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        content = self._tool_preview_content(event)
        if not content:
            self._last_change = TextualFeedChange(False)
            return False
        lines = self._transient_tools_by_agent.setdefault(agent, [])
        is_mcp_http = self._is_mcp_http_payload(payload)
        request_id = self._tool_request_id(payload) if is_mcp_http else ""
        if request_id:
            # MCP HTTP fornece identidade explícita por chamada. Duas requests
            # distintas podem executar exatamente a mesma tool/argumentos e
            # ainda assim precisam ocupar linhas independentes no burst.
            # Portanto só substituímos uma linha quando o request_id é o mesmo;
            # deduplicação textual fica restrita ao caminho CLI sem request_id.
            request_lines = self._tool_request_lines_by_agent.setdefault(agent, {})
            previous_line = request_lines.get(request_id)
            if previous_line and previous_line in lines:
                lines[lines.index(previous_line)] = content
            else:
                lines.append(content)
            request_lines[request_id] = content
            self._tool_request_seen_at_by_agent.setdefault(agent, {}).setdefault(
                request_id,
                self._clock(),
            )
        else:
            # Uma mesma tool CLI costuma emitir uma linha de início ("$ cmd") e
            # outra de conclusão ("✓ cmd"/"✗ cmd (exit N)"). Em vez de acumular
            # as duas, atualizamos a linha existente do mesmo comando no lugar.
            subject = self._tool_preview_subject(content)
            replaced_line = False
            if subject:
                for idx in range(len(lines) - 1, -1, -1):
                    if self._tool_preview_subject(lines[idx]) == subject:
                        lines[idx] = content
                        replaced_line = True
                        break
            if not replaced_line and subject:
                replaced_line = self._merge_generic_tool_line(lines, content, subject)
            if not replaced_line:
                lines.append(content)
        if is_mcp_http:
            self._extend_tool_preview_expiries(agent)
        index = self._transient_index_by_agent.get(agent)
        if index is None or not (0 <= index < len(self._items)):
            transient_payload = dict(payload)
            transient_payload["content"] = ""
            transient_payload["tools"] = list(lines)
            replaced = self._upsert_transient(TextualUiEvent("agent_update", transient_payload, agent=event.agent))
            self._last_change = TextualFeedChange(True, redraw=replaced, appended=None if replaced else self._items[-1])
            return True
        current_event = self._items[index].event
        if self._is_lifecycle_placeholder_event(current_event):
            current_event = TextualUiEvent("agent_update", {"content": ""}, agent=event.agent)
        self._items[index] = TextualFeedItem(self._with_transient_tools(current_event), transient=True)
        self._last_change = TextualFeedChange(True, redraw=True)
        return True

    @staticmethod
    def _tool_request_id(payload: dict[str, Any]) -> str:
        """Identidade estável de uma chamada MCP dentro do agrupamento visual."""
        msg_id = str(payload.get("mcp_msg_id") or payload.get("msg_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        if run_id and msg_id:
            return f"{run_id}#{msg_id}"
        return msg_id or run_id

    @staticmethod
    def _is_mcp_http_payload(payload: dict[str, Any]) -> bool:
        """Retorna se o payload pertence ao transporte MCP HTTP."""
        transport = str(payload.get("transport") or "").strip()
        return transport == "mcp_http"

    @classmethod
    def _terminal_tool_line(cls, preview_line: str, status: str) -> str:
        """Converte a linha running em resultado terminal sem perder argumentos."""
        subject = cls._tool_preview_subject(preview_line)
        marker = "✓" if status == "finished" else "✗"
        has_remote_prefix = str(preview_line).strip().startswith(
            f"{cls._MCP_HTTP_TOOL_MARKER} "
        )
        remote_prefix = f"{cls._MCP_HTTP_TOOL_MARKER} " if has_remote_prefix else ""
        return f"{remote_prefix}{marker} {subject}".strip()

    def _extend_tool_preview_expiries(self, agent: str, *, now: float | None = None) -> None:
        """Mantém o burst MCP HTTP visível enquanto o mesmo cliente segue ativo.

        Agentes CLI limpam previews no fim da execução. MCP HTTP não possui um
        evento equivalente de fim de turno; portanto usamos inatividade do
        cliente como boundary visual e renovamos o prazo de todas as tools
        concluídas quando uma nova chamada chega.
        """
        expiries = self._tool_request_expiry_by_agent.get(agent)
        if not expiries:
            return
        deadline = (self._clock() if now is None else now) + self._tool_preview_dwell_seconds
        for request_id in expiries:
            expiries[request_id] = deadline

    def _apply_tool_state(self, event: TextualUiEvent) -> bool:
        """Finaliza preview MCP HTTP, acumula stats e agenda remoção visual."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        if str(payload.get("transport") or "").strip() != "mcp_http":
            self._last_change = TextualFeedChange(False)
            return False

        agent = self._agent_key(event)
        status = str(payload.get("status") or "").strip().lower()
        if status not in {"finished", "failed", "cancelled"}:
            self._last_change = TextualFeedChange(False)
            return False

        request_id = self._tool_request_id(payload)
        if request_id:
            completed = self._completed_tool_requests_by_agent.setdefault(agent, set())
            if request_id in completed:
                self._last_change = TextualFeedChange(False)
                return False
            completed.add(request_id)
        lines = self._transient_tools_by_agent.get(agent, [])
        request_lines = self._tool_request_lines_by_agent.get(agent, {})
        preview_line = request_lines.get(request_id) if request_id else None
        tool_name = str(payload.get("tool_name") or "").strip()
        if preview_line and preview_line in lines:
            terminal_line = self._terminal_tool_line(preview_line, status)
            lines[lines.index(preview_line)] = terminal_line
            request_lines[request_id] = terminal_line
        elif lines:
            for index in range(len(lines) - 1, -1, -1):
                subject = self._tool_preview_subject(lines[index])
                if self._tool_preview_tool_name(subject) == tool_name:
                    preview_line = lines[index]
                    terminal_line = self._terminal_tool_line(preview_line, status)
                    lines[index] = terminal_line
                    if request_id:
                        request_lines[request_id] = terminal_line
                    break
        if not preview_line:
            preview_line = self._terminal_tool_line(
                f"{self._MCP_HTTP_TOOL_MARKER} ⚒ {tool_name or 'tool'}",
                status,
            )
            lines = self._transient_tools_by_agent.setdefault(agent, [])
            lines.append(preview_line)
            if request_id:
                request_lines = self._tool_request_lines_by_agent.setdefault(agent, {})
                request_lines[request_id] = preview_line

        now = self._clock()
        expiry_id = request_id or f"terminal:{tool_name or 'tool'}"
        self._tool_request_expiry_by_agent.setdefault(agent, {})[expiry_id] = (
            now + self._tool_preview_dwell_seconds
        )
        self._extend_tool_preview_expiries(agent, now=now)

        stats = self._mcp_http_tool_stats_by_agent.setdefault(
            agent,
            {"total": 0, "ok_count": 0, "err_count": 0, "duration_ms": 0},
        )
        stats["total"] += 1
        if status == "finished":
            stats["ok_count"] += 1
        else:
            stats["err_count"] += 1
        duration_ms = payload.get("duration_ms")
        if isinstance(duration_ms, int) and duration_ms >= 0:
            stats["duration_ms"] += duration_ms

        index = self._transient_index_by_agent.get(agent)
        if index is not None and 0 <= index < len(self._items):
            current = self._items[index].event
            self._items[index] = TextualFeedItem(
                self._with_transient_tools(current),
                transient=True,
            )
            self._last_change = TextualFeedChange(True, redraw=True)
            return True

        transient_payload = dict(payload)
        transient_payload["content"] = ""
        replaced = self._upsert_transient(
            TextualUiEvent("agent_update", transient_payload, agent=event.agent)
        )
        self._last_change = TextualFeedChange(
            True,
            redraw=replaced,
            appended=None if replaced else self._items[-1],
        )
        return True

    def expire_tool_previews(self) -> bool:
        """Encerra bursts MCP HTTP inativos como uma resposta final de agente CLI.

        Enquanto houver qualquer tool do cliente ainda rodando, o bloco inteiro
        permanece visível. Quando todas estiverem concluídas e o último prazo de
        inatividade vencer, removemos o transient completo, incluindo stats.
        """
        now = self._clock()
        expired_agents: list[str] = []
        for agent, expiries in list(self._tool_request_expiry_by_agent.items()):
            request_lines = self._tool_request_lines_by_agent.get(agent, {})
            running_request_ids = set(request_lines) - set(expiries)
            if running_request_ids:
                continue
            if not expiries or any(deadline > now for deadline in expiries.values()):
                continue
            expired_agents.append(agent)

        changed = False
        for agent in expired_agents:
            changed = self._remove_transient_keys([agent]) or changed
        self._last_change = TextualFeedChange(changed, redraw=changed)
        return changed

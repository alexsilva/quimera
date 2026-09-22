"""Agent execution event contract used before UI rendering policy.

This module is intentionally small and side-effect free. It gives chat, task
and delegate execution paths a common vocabulary without changing terminal
rendering behavior.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


_FINAL_EVENT_KINDS = frozenset({"finished", "failed", "cancelled", "tool_finished", "tool_failed", "tool_cancelled"})


class ThinkingStreamParser:
    """Extrai blocos <think>/<thinking> de um stream de texto bruto.

    Mantém o último bloco de raciocínio visto (parcial enquanto aberto, completo
    após fechar) e uma cauda limitada do stream bruto como fallback para agentes
    que não emitem tags de raciocínio (ex.: CLIs cujo stdout já é o raciocínio).
    """

    _OPEN_RE = re.compile(r"<think(?:ing)?>")
    _CLOSE_RE = re.compile(r"</think(?:ing)?>")
    _TAIL_KEEP = 12

    def __init__(
        self,
        on_thinking: Callable[[str], None] | None = None,
        tail_limit: int = 400,
    ) -> None:
        self._on_thinking = on_thinking
        self._tail_limit = max(1, int(tail_limit))
        self._buffer = ""
        self._in_think = False
        self._thinking_text = ""
        self._last_thinking = ""
        self._stream_tail = ""

    @property
    def last_thinking(self) -> str:
        """Último bloco de raciocínio observado (parcial ou completo)."""
        return self._last_thinking

    @property
    def stream_tail(self) -> str:
        """Cauda recente do stream bruto, limitada a tail_limit caracteres."""
        return self._stream_tail

    def feed(self, chunk_text: str) -> None:
        """Processa um novo pedaço de texto bruto do stream."""
        if not chunk_text:
            return
        self._stream_tail = (self._stream_tail + chunk_text)[-self._tail_limit:]
        self._buffer += chunk_text
        while True:
            if not self._in_think:
                match = self._OPEN_RE.search(self._buffer)
                if not match:
                    self._buffer = self._buffer[-self._TAIL_KEEP:]
                    return
                self._in_think = True
                self._buffer = self._buffer[match.end():]
                self._thinking_text = ""
                continue
            match = self._CLOSE_RE.search(self._buffer)
            if not match:
                self._thinking_text += self._buffer[:-self._TAIL_KEEP] if len(self._buffer) > self._TAIL_KEEP else ""
                self._buffer = self._buffer[-self._TAIL_KEEP:]
                self._publish()
                return
            self._thinking_text += self._buffer[:match.start()]
            self._buffer = self._buffer[match.end():]
            self._in_think = False
            self._publish()
            self._thinking_text = ""

    def _publish(self) -> None:
        text = self._thinking_text.strip()
        if not text:
            return
        self._last_thinking = text
        if self._on_thinking is not None:
            self._on_thinking(text)


def _event_status(kind: str, explicit: str = "") -> str:
    """Map an event kind to the coarse lifecycle status exposed by the registry."""
    if explicit:
        return explicit
    if kind == "started":
        return "running"
    if kind == "finished":
        return "finished"
    if kind == "failed":
        return "failed"
    if kind == "cancelled":
        return "cancelled"
    if kind == "tool_finished":
        return "finished"
    if kind == "tool_failed":
        return "failed"
    if kind == "tool_cancelled":
        return "cancelled"
    return "running"


@dataclass(frozen=True)
class AgentRunEvent:
    """One normalized event emitted by an agent execution path."""

    kind: str
    agent: str
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    parent_run_id: str = ""
    delegation_id: str = ""
    transport: str = ""
    status: str = ""


@dataclass(frozen=True)
class AgentRunRecord:
    """Thread-safe snapshot of one agent execution run."""

    run_id: str
    agent: str
    status: str
    parent_run_id: str = ""
    delegation_id: str = ""
    transport: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    finished_at: float | None = None
    last_event_kind: str = ""
    last_text: str = ""
    last_thinking: str = ""
    stream_tail: str = ""
    last_activity: str = ""
    event_count: int = 0


class AgentRunSink(Protocol):
    """Consumer for normalized agent execution events."""

    def emit(self, event: AgentRunEvent) -> None:
        """Receive one normalized event."""


class NullAgentRunSink:
    """Default sink that preserves current behavior by ignoring events."""

    def emit(self, event: AgentRunEvent) -> None:
        del event


class AgentRunRegistry:
    """In-memory index of active and recently completed agent runs."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_runs: int = 100,
        on_prune: Callable[[list[str]], None] | None = None,
    ) -> None:
        self._clock = clock
        self._max_runs = max(1, int(max_runs))
        self._on_prune = on_prune
        self._runs: dict[str, AgentRunRecord] = {}
        self._stream_parsers: dict[str, ThinkingStreamParser] = {}
        self._lock = threading.RLock()

    @property
    def max_runs(self) -> int:
        return self._max_runs

    def record(self, event: AgentRunEvent) -> AgentRunRecord | None:
        """Apply one event and return the updated run snapshot when it is traceable."""
        run_id = self._field(event, "run_id")
        if not run_id:
            return None
        now = self._clock()
        with self._lock:
            current = self._runs.get(run_id)
            if event.kind in {"delta", "activity"} and current is not None and current.finished_at is not None:
                # Uma thread leitora de CLI pode encerrar alguns instantes após
                # cancelamento/timeout. Eventos tardios de stream/tool não podem
                # ressuscitar um run já terminal como "running".
                return current
            status = _event_status(event.kind, self._field(event, "status"))
            last_thinking = current.last_thinking if current else ""
            stream_tail = current.stream_tail if current else ""
            last_activity = current.last_activity if current else ""
            if event.kind == "delta" and event.text:
                parser = self._stream_parsers.get(run_id)
                if parser is None:
                    parser = ThinkingStreamParser()
                    self._stream_parsers[run_id] = parser
                parser.feed(str(event.text))
                last_thinking = parser.last_thinking or last_thinking
                stream_tail = parser.stream_tail or stream_tail
            elif event.kind == "activity" and event.text:
                last_activity = str(event.text)
            record = AgentRunRecord(
                run_id=run_id,
                agent=str(event.agent or (current.agent if current else "")),
                status=status,
                parent_run_id=self._field(event, "parent_run_id") or (current.parent_run_id if current else ""),
                delegation_id=self._field(event, "delegation_id") or (current.delegation_id if current else ""),
                transport=self._field(event, "transport") or (current.transport if current else ""),
                started_at=current.started_at if current else now,
                updated_at=now,
                finished_at=now if event.kind in _FINAL_EVENT_KINDS else (current.finished_at if current else None),
                last_event_kind=str(event.kind or ""),
                last_text=str(event.text or ""),
                last_thinking=last_thinking,
                stream_tail=stream_tail,
                last_activity=last_activity,
                event_count=(current.event_count if current else 0) + 1,
            )
            self._runs[run_id] = record
            if event.kind in _FINAL_EVENT_KINDS:
                self._stream_parsers.pop(run_id, None)
            pruned = self._prune_locked()
        self._notify_pruned(pruned)
        return record

    def prune(self) -> list[str]:
        """Drop oldest finished runs when the retention limit is exceeded."""
        with self._lock:
            pruned = self._prune_locked()
        self._notify_pruned(pruned)
        return pruned

    def _prune_locked(self) -> list[str]:
        excess = len(self._runs) - self._max_runs
        if excess <= 0:
            return []
        candidates = [
            run
            for run in self._runs.values()
            if run.finished_at is not None
        ]
        candidates.sort(key=lambda run: (run.finished_at or run.updated_at, run.updated_at, run.run_id))
        pruned: list[str] = []
        for run in candidates[:excess]:
            if self._runs.pop(run.run_id, None) is not None:
                self._stream_parsers.pop(run.run_id, None)
                pruned.append(run.run_id)
        return pruned

    def _notify_pruned(self, pruned: list[str]) -> None:
        if pruned and self._on_prune is not None:
            self._on_prune(list(pruned))

    def get(self, run_id: str) -> AgentRunRecord | None:
        with self._lock:
            return self._runs.get(str(run_id or ""))

    def snapshot(self) -> list[AgentRunRecord]:
        with self._lock:
            return list(self._runs.values())

    def active_runs(self) -> list[AgentRunRecord]:
        with self._lock:
            return [
                run
                for run in self._runs.values()
                if run.status not in {"finished", "failed", "cancelled"}
            ]

    def find_by_delegation(self, delegation_id: str) -> AgentRunRecord | None:
        """Retorna o run mais recente associado a um delegation_id.

        Fallbacks e retries reutilizam o mesmo delegation_id em runs distintos;
        o run com updated_at mais recente é o que reflete o estado atual.
        """
        wanted = str(delegation_id or "")
        if not wanted:
            return None
        with self._lock:
            matches = [run for run in self._runs.values() if run.delegation_id == wanted]
        if not matches:
            return None
        return max(matches, key=lambda run: (run.updated_at, run.run_id))

    def live_delegation_view(self, delegation_id: str) -> dict[str, Any] | None:
        """Snapshot resumido de uma delegação em execução, pronto para exibição.

        last_thinking prioriza o raciocínio extraído de tags <think>; sem tags,
        cai para a cauda recente do stream (comportamento natural para agentes
        CLI, cujo stdout já é o raciocínio).
        """
        record = self.find_by_delegation(delegation_id)
        if record is None:
            return None
        view = {
            "agent": record.agent,
            "status": record.status,
            "last_thinking": record.last_thinking or record.stream_tail,
            "updated_seconds_ago": max(0.0, round(self._clock() - record.updated_at, 1)),
        }
        if record.last_activity:
            view["last_activity"] = record.last_activity
        return view

    @staticmethod
    def _field(event: AgentRunEvent, name: str) -> str:
        value = getattr(event, name, "") or ""
        if value:
            return str(value)
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        return str(metadata.get(name) or "")


class AgentRunController:
    """Coordinates execution-boundary effects that belong to agent runs."""

    def __init__(self, renderer=None, registry: AgentRunRegistry | None = None) -> None:
        self._renderer = renderer
        self._registry = registry or AgentRunRegistry()

    @property
    def registry(self) -> AgentRunRegistry:
        return self._registry

    def set_renderer(self, renderer) -> None:
        self._renderer = renderer

    def emit(self, event: AgentRunEvent) -> None:
        self._registry.record(event)
        if event.kind == "started":
            self._begin_agent_run(event)
        elif event.kind in _FINAL_EVENT_KINDS:
            self._end_agent_run(event)
        if event.kind in {"tool_finished", "tool_failed", "tool_cancelled"}:
            self._show_tool_run_state(event)
        if event.kind == "human_action_requested":
            self._commit_agent_output(event.agent)

    def _begin_agent_run(self, event: AgentRunEvent) -> None:
        if self._renderer is None:
            return
        begin = getattr(self._renderer, "begin_agent_run", None)
        if not callable(begin):
            return
        begin(
            event.agent,
            run_id=AgentRunRegistry._field(event, "run_id"),
            parent_run_id=AgentRunRegistry._field(event, "parent_run_id"),
            delegation_id=AgentRunRegistry._field(event, "delegation_id"),
            transport=AgentRunRegistry._field(event, "transport"),
        )

    def _end_agent_run(self, event: AgentRunEvent) -> None:
        if self._renderer is None:
            return
        end = getattr(self._renderer, "end_agent_run", None)
        if not callable(end):
            return
        end(
            event.agent,
            run_id=AgentRunRegistry._field(event, "run_id"),
            status=_event_status(event.kind, AgentRunRegistry._field(event, "status")),
        )

    def _show_tool_run_state(self, event: AgentRunEvent) -> None:
        if self._renderer is None:
            return
        transport = AgentRunRegistry._field(event, "transport")
        if transport != "mcp_http":
            return
        show_state = getattr(self._renderer, "show_tool_run_state", None)
        if not callable(show_state):
            return
        metadata = dict(event.metadata) if isinstance(event.metadata, dict) else {}
        metadata.update(
            {
                "run_id": AgentRunRegistry._field(event, "run_id"),
                "parent_run_id": AgentRunRegistry._field(event, "parent_run_id"),
                "transport": transport,
                "status": _event_status(event.kind, AgentRunRegistry._field(event, "status")),
                "tool_name": str(event.text or metadata.get("tool_name") or ""),
            }
        )
        show_state(event.agent, metadata)

    def _commit_agent_output(self, agent: str) -> None:
        if self._renderer is not None:
            self._renderer.commit_agent_stream(agent)


def coerce_agent_run_sink(sink: AgentRunSink | None) -> AgentRunSink:
    """Return a sink object; never expose None to call sites."""
    return sink if sink is not None else NullAgentRunSink()

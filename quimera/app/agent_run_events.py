"""Agent execution event contract used before UI rendering policy.

This module is intentionally small and side-effect free. It gives chat, task
and delegate execution paths a common vocabulary without changing terminal
rendering behavior.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


_FINAL_EVENT_KINDS = frozenset({"finished", "failed", "cancelled", "tool_finished", "tool_failed", "tool_cancelled"})


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
            status = _event_status(event.kind, self._field(event, "status"))
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
                event_count=(current.event_count if current else 0) + 1,
            )
            self._runs[run_id] = record
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

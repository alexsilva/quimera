"""Agent execution event contract used before UI rendering policy.

This module is intentionally small and side-effect free. It gives chat, task
and delegate execution paths a common vocabulary without changing terminal
rendering behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class AgentRunEvent:
    """One normalized event emitted by an agent execution path."""

    kind: str
    agent: str
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentRunSink(Protocol):
    """Consumer for normalized agent execution events."""

    def emit(self, event: AgentRunEvent) -> None:
        """Receive one normalized event."""


class NullAgentRunSink:
    """Default sink that preserves current behavior by ignoring events."""

    def emit(self, event: AgentRunEvent) -> None:
        del event


class AgentRunController:
    """Coordinates execution-boundary effects that belong to agent runs."""

    def __init__(self, renderer=None) -> None:
        self._renderer = renderer

    def set_renderer(self, renderer) -> None:
        self._renderer = renderer

    def emit(self, event: AgentRunEvent) -> None:
        if event.kind == "human_action_requested":
            self._commit_agent_output(event.agent)

    def _commit_agent_output(self, agent: str) -> None:
        renderer = self._renderer
        commit = getattr(renderer, "commit_agent_stream", None) if renderer is not None else None
        if callable(commit):
            commit(agent)


def coerce_agent_run_sink(sink: AgentRunSink | None) -> AgentRunSink:
    """Return a sink object; never expose None to call sites."""
    return sink if sink is not None else NullAgentRunSink()

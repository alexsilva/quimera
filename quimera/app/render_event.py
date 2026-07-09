"""Evento de UI produzido por workers, consumido pelo main thread."""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any


@dataclass(slots=True)
class RenderEvent:
    """Evento de render thread-safe entregue ao main loop."""

    TEXT = "TEXT"
    SYSTEM = "SYSTEM"
    WARNING = "WARNING"
    ERROR = "ERROR"
    SPINNER_START = "SPINNER_START"
    SPINNER_STOP = "SPINNER_STOP"
    TURN_SUMMARY = "TURN_SUMMARY"
    DELEGATION = "DELEGATION"
    AGENT_ACTIVITY = "AGENT_ACTIVITY"
    EVENT = "EVENT"
    REDISPLAY = "REDISPLAY"
    HEARTBEAT = "HEARTBEAT"

    type: str
    payload: Any
    timestamp: float = field(default_factory=time.time)
    agent: str | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        normalized = {
            "agent_msg": self.TEXT,
            "system": self.SYSTEM,
            "warning": self.WARNING,
            "error": self.ERROR,
            "turn_summary": self.TURN_SUMMARY,
            "delegation": self.DELEGATION,
            "agent_activity": self.AGENT_ACTIVITY,
            "event": self.EVENT,
            "post_agent_flush": self.REDISPLAY,
            "heartbeat": self.HEARTBEAT,
        }.get(str(self.type).lower())
        if normalized is not None:
            self.type = normalized

    @property
    def kind(self) -> str:
        return self.type

    @property
    def content(self) -> Any:
        return self.payload

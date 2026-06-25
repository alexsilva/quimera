"""Typed events exchanged with the terminal renderer writer thread."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PrintEvent:
    renderable: Any
    kwargs: dict = field(default_factory=dict)
    kind: str = "generic"


@dataclass
class LiveStartEvent:
    agent: str


@dataclass
class LiveUpdateChunkEvent:
    agent: str
    chunk: Any


@dataclass
class LiveStopEvent:
    agent: str
    final_content: str
    render_mode: str = "auto"


@dataclass
class LiveAbortEvent:
    agent: str


@dataclass
class NoopEvent:
    done: threading.Event
    force_flush: bool = False


@dataclass
class OutputControlEvent:
    """Pede ao compositor para suspender ou retomar a saída terminal."""

    suspend: bool
    done: threading.Event | None = None
    render_anchored_windows: bool = False


@dataclass
class TransientWindowEvent:
    """Substitui a janela transient no modo prompt ativo com substituição in-place."""

    text: str
    count: int
    buf_version: int = 0


@dataclass
class TransientClearEvent:
    """Limpa a janela transient no modo prompt ativo."""

    buf_version: int = 0


@dataclass
class TerminalResizeEvent:
    """Terminal foi redimensionado — reseta contador de linhas do overlay."""


@dataclass
class PendingInputEvent:
    """Sinaliza que uma janela de agente aguarda input do usuário."""

    agent: str
    kind: str
    question: str = ""

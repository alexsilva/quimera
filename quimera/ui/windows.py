"""Window domain model for Quimera terminal UI.

This module owns declarative window state and stacking policy metadata. It must
not write to the terminal or depend on TerminalRenderer internals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Callable

_ANSI_REAL_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_ANSI_ORPHANED_RE = re.compile(r"\[[0-9;?]+[A-Za-z]")
_UNICODE_CONTROL_RE = re.compile(
    r"[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u061C\u200B-\u200F\u202A-\u202E\u2060-\u2069\uFEFF]"
)


def sanitize_window_text(value: Any) -> str:
    """Normalize text stored in window state without renderer dependencies."""
    text = str(value or "")
    text = _ANSI_REAL_RE.sub("", text)
    text = _ANSI_ORPHANED_RE.sub("", text)
    return _UNICODE_CONTROL_RE.sub("", text)


class WindowKind(str, Enum):
    AGENT = "agent"
    INPUT = "input"
    APPROVAL = "approval"
    SELECTION = "selection"
    EDITOR = "editor"
    SYSTEM = "system"
    TRANSIENT = "transient"
    TERMINAL_FLOOR = "terminal_floor"


class WindowLayer(str, Enum):
    CONTENT = "content"
    LIVE = "live"
    OVERLAY = "overlay"
    MODAL = "modal"
    EXTERNAL = "external"


class WindowModality(str, Enum):
    NON_BLOCKING = "non_blocking"
    BLOCKING = "blocking"
    EXCLUSIVE_TERMINAL = "exclusive_terminal"


class RestorePolicy(str, Enum):
    KEEP = "keep"
    DISCARD_ON_CLOSE = "discard_on_close"
    RESTORE_DECK_AFTER_CLOSE = "restore_deck_after_close"


@dataclass
class RenderWindowState:
    """Generic window managed by the deck.

    Anything that overlays, blocks input, or requires exclusive terminal control
    should be represented as a RenderWindowState instead of manipulating the
    terminal directly.
    """

    id: str
    kind: WindowKind
    layer: WindowLayer
    modality: WindowModality = WindowModality.NON_BLOCKING
    owner: str | None = None
    title: str = ""
    restore_policy: RestorePolicy = RestorePolicy.KEEP
    metadata: dict[str, Any] = field(default_factory=dict)
    active: bool = True


@dataclass
class AgentWindowState:
    """Declarative state for a vertical agent window."""

    agent: str
    label: str
    style: str
    streaming: bool = False
    stream_content: str = ""
    stream_theme_name: str = ""
    transient_active: bool = False
    elapsed: float | None = None
    transient: list[str] = field(default_factory=list)
    pending_kind: str = ""
    pending_question: str = ""
    transient_limit: int = 10

    def compose_question(self, question: str, options: list[str] | None = None) -> str:
        """Build the textual body displayed below an agent banner."""
        lines = [sanitize_window_text(str(question or "")).strip()]
        for index, option in enumerate(options or []):
            lines.append(f"  {index + 1}. {sanitize_window_text(option)}")
        return "\n".join(line for line in lines if line)

    def push_transient(self, message: str) -> bool:
        """Append a rolling transient message. Returns True when state changed."""
        clean = sanitize_window_text(str(message or "")).strip("\r\n")
        if not clean:
            return False
        if self.transient and self.transient[-1] == clean:
            return False
        self.transient.append(clean)
        self.transient = self.transient[-self.transient_limit:]
        return True

    def clear_transient_buffer(self) -> None:
        """Clear rolling transient messages for this agent window."""
        self.transient.clear()
        self.transient_active = False


@dataclass
class WindowDeck:
    """Single source of truth for terminal windows.

    `windows` stores per-agent visual state. Renderer-specific subclasses may
    extend AgentWindowState while the event loop is being decomposed.
    """

    windows: dict[Any, AgentWindowState] = field(default_factory=dict)
    completed_streams: dict[Any, str] = field(default_factory=dict)
    managed_windows: dict[str, RenderWindowState] = field(default_factory=dict)

    def get_or_create(self, key: Any, factory: Callable[[Any], Any]) -> Any:
        window = self.windows.get(key)
        if window is None:
            window = factory(key)
            self.windows[key] = window
        return window

    def get(self, key: Any) -> Any | None:
        return self.windows.get(key)

    def active_streams(self) -> dict[Any, Any]:
        return {
            key: window
            for key, window in self.windows.items()
            if bool(getattr(window, "streaming", False))
        }

    def remember_completed_stream(self, key: Any, content: str) -> None:
        self.completed_streams[key] = str(content or "")

    def consume_completed_stream(self, key: Any) -> str | None:
        return self.completed_streams.pop(key, None)

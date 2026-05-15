"""API pública de `quimera.ui`."""

from .audit import RenderAuditLogger
from . import renderer as _renderer
from .renderer import TerminalRenderer
from .renderer import _RICH_AVAILABLE
from .renderer import _agent_style
from .renderer import _apply_stream_diff
from .renderer import _extract_text_from_renderable
from .renderer import _highlight_tags
from .renderer import _is_interactive_terminal
from .renderer import _normalize_stream_diff
from .renderer import os
from .renderer import strip_ansi
from .renderer import sys

if hasattr(_renderer, "Console"):
    Console = _renderer.Console
if hasattr(_renderer, "Live"):
    Live = _renderer.Live
if hasattr(_renderer, "Markdown"):
    Markdown = _renderer.Markdown
if hasattr(_renderer, "Panel"):
    Panel = _renderer.Panel

__all__ = [
    "Console",
    "Live",
    "Markdown",
    "Panel",
    "RenderAuditLogger",
    "TerminalRenderer",
    "strip_ansi",
]

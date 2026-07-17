"""API pública de `quimera.ui`."""

from .audit import RenderAuditLogger
from . import renderer as _renderer
from .renderer import TerminalRenderer
from .renderer import _RICH_AVAILABLE
from .renderer import _agent_style
from .renderer import _is_interactive_terminal
from .renderer import os
from .renderer import sys
from .text import (
    _apply_stream_diff,
    _extract_text_from_renderable,
    _highlight_tags,
    _normalize_stream_diff,
    strip_ansi,
)

if hasattr(_renderer, "Console"):
    Console = _renderer.Console
if hasattr(_renderer, "Group"):
    Group = _renderer.Group
if hasattr(_renderer, "Live"):
    Live = _renderer.Live
if hasattr(_renderer, "Markdown"):
    Markdown = _renderer.Markdown
if hasattr(_renderer, "markup_escape"):
    markup_escape = _renderer.markup_escape
if hasattr(_renderer, "Panel"):
    Panel = _renderer.Panel
if hasattr(_renderer, "Rule"):
    Rule = _renderer.Rule
if hasattr(_renderer, "Text"):
    Text = _renderer.Text

__all__ = [
    "Console",
    "Group",
    "Live",
    "Markdown",
    "Panel",
    "Rule",
    "RenderAuditLogger",
    "TerminalRenderer",
    "Text",
    "_RICH_AVAILABLE",
    "_agent_style",
    "_apply_stream_diff",
    "_extract_text_from_renderable",
    "_highlight_tags",
    "_is_interactive_terminal",
    "_normalize_stream_diff",
    "markup_escape",
    "os",
    "strip_ansi",
    "sys",
]

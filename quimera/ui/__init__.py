"""API pública de `quimera.ui`.

Dois renderers implementam o contrato de :class:`RendererBase`:
``TextualRenderer`` (em ``quimera.ui.textual``) para a TUI e
:class:`PlainTextRenderer` para stdin em pipe e ``--interactive-test``.
"""

from .audit import RenderAuditLogger
from .base import RendererBase
from .plain import PlainTextRenderer
from .text import (
    _agent_style,
    _extract_text_from_renderable,
    _highlight_tags,
    strip_ansi,
)

__all__ = [
    "PlainTextRenderer",
    "RenderAuditLogger",
    "RendererBase",
    "_agent_style",
    "_extract_text_from_renderable",
    "_highlight_tags",
    "strip_ansi",
]

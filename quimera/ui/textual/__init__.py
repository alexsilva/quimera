"""Componentes da interface Textual do Quimera."""
from __future__ import annotations

from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.app import run_textual_quimera_app
from quimera.ui.textual.feed_model import (
    AgentLifecycleStatus,
    TextualFeedChange,
    TextualFeedItem,
    TextualFeedModel,
    _agent_lifecycle_payload,
)
from quimera.ui.textual.input_gate import TextualInputGate
from quimera.ui.textual.renderables import (
    _build_question_overlay,
    _build_stream_renderable,
    _render_event,
)
from quimera.ui.textual.renderer import TextualRenderer
from quimera.ui.textual.styles import TEXTUAL_APP_CSS

__all__ = [
    "AgentLifecycleStatus",
    "TextualFeedChange",
    "TextualFeedItem",
    "TextualFeedModel",
    "TextualInputGate",
    "TextualRenderer",
    "TEXTUAL_APP_CSS",
    "TextualUiBridge",
    "TextualUiEvent",
    "_agent_lifecycle_payload",
    "_build_question_overlay",
    "_build_stream_renderable",
    "_render_event",
    "run_textual_quimera_app",
]

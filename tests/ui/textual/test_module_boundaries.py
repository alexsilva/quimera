"""Tests for Textual UI module boundaries."""

from quimera.ui.textual.app import run_textual_quimera_app
from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import TextualFeedModel
from quimera.ui.textual.input_gate import TextualInputGate
from quimera.ui.textual.renderables import (
    _resolve_transport_label,
    _TRANSPORT_MARKERS,
    _render_event,
)
from quimera.ui.textual.renderer import TextualRenderer
from quimera.ui.textual.styles import TEXTUAL_APP_CSS


def test_textual_public_modules_export_core_components():
    bridge = TextualUiBridge()

    assert isinstance(TextualFeedModel(), TextualFeedModel)
    assert isinstance(TextualInputGate(bridge), TextualInputGate)
    assert isinstance(TextualRenderer(bridge), TextualRenderer)
    assert callable(run_textual_quimera_app)


def test_renderables_module_renders_user_message_event():
    rendered = _render_event(TextualUiEvent("user_message", {"content": "oi", "label": "Alex"}))

    assert rendered is not None


def test_textual_styles_module_owns_layout_css():
    assert "#main" in TEXTUAL_APP_CSS
    assert "#feed" in TEXTUAL_APP_CSS
    assert "#toolbar" in TEXTUAL_APP_CSS


def test_transport_marker_map_has_mcp_http_entry():
    assert _TRANSPORT_MARKERS["mcp_http"] == "☁"


def test_resolve_transport_label_adds_icon_for_mcp_http():
    payload = {"label": "🤖  Claude", "transport": "mcp_http"}
    assert _resolve_transport_label(payload) == "☁ 🤖  Claude"


def test_resolve_transport_label_no_icon_for_local_socket():
    payload = {"label": "🤖  Claude", "transport": ""}
    assert _resolve_transport_label(payload) == "🤖  Claude"


def test_resolve_transport_label_no_icon_for_unknown_transport():
    payload = {"label": "🤖  Claude", "transport": "task"}
    assert _resolve_transport_label(payload) == "🤖  Claude"


def test_resolve_transport_label_handles_non_dict_payload():
    assert _resolve_transport_label(None, "claude") == "🤖 claude"

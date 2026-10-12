"""Wiring de ``--debug`` com a TUI: audit de render ligado e tui_screenshot exposta."""
from __future__ import annotations

import os

import pytest

from quimera.app import QuimeraApp
from quimera.runtime.drivers.tool_schemas import resolve_tool_schemas
from quimera.runtime.models import ToolCall
from quimera.ui.base import RendererBase


class _FakeInputGate:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def is_active(self):
        return False

    def get_owner_thread_id(self):
        return None

    def run_in_terminal_message(self, callback=None, *args, **kwargs):
        return callback() if callable(callback) else None

    def set_toolbar_context_resolver(self, resolver):
        return None

    def set_theme_cycle_handler(self, handler):
        return None

    def read_input(self, _prompt, timeout=None):
        return None

    def read_input_in_terminal(self, _prompt, timeout=None, metadata=None):
        return None

    def read_selection_in_terminal(self, _question, _options, metadata=None):
        return None

    def read_approval_in_terminal(self, _question, metadata=None):
        return None


class _ScreenshotRenderer(RendererBase):
    """Renderer mínimo com o contrato da TUI: audit injetável e captura de tela."""

    supports_screenshot = True
    theme_name = "fake"
    density = "compact"

    def __init__(self):
        self.audit_logger = None
        self.closed = False
        self.captures: list[str | None] = []

    def show_system(self, message):
        return None

    def set_theme(self, name):
        return name

    def set_density(self, density):
        return None

    def set_audit_logger(self, audit_logger):
        self.audit_logger = audit_logger

    def log_debug_event(self, event, **payload):
        if self.audit_logger is not None:
            self.audit_logger.log_event(event, **payload)

    def capture_screenshot(self, *, title=None):
        self.captures.append(title)
        return type(
            "Shot",
            (),
            {"text": "linha", "svg": "<svg/>", "width": 10, "height": 1, "feed": {"slots": 1}},
        )()

    def close(self, timeout=5.0):
        self.closed = True
        if self.audit_logger is not None:
            self.audit_logger.close()


class _PlainRenderer(_ScreenshotRenderer):
    supports_screenshot = False


@pytest.fixture
def make_app(tmp_path):
    created = []
    previous_job_id = os.environ.get("QUIMERA_CURRENT_JOB_ID")

    def factory(renderer, *, debug):
        app = QuimeraApp(
            tmp_path,
            renderer_override=renderer,
            input_gate_factory=lambda **kwargs: _FakeInputGate(**kwargs),
            debug=debug,
        )
        created.append(app)
        return app

    try:
        yield factory
    finally:
        for app in created:
            app.close()
        if previous_job_id is None:
            os.environ.pop("QUIMERA_CURRENT_JOB_ID", None)
        else:
            os.environ["QUIMERA_CURRENT_JOB_ID"] = previous_job_id


def test_debug_attaches_events_only_audit_logger_to_the_tui_renderer(make_app):
    renderer = _ScreenshotRenderer()
    app = make_app(renderer, debug=True)

    audit = renderer.audit_logger
    assert audit is not None
    assert audit.events_path == app.session_paths.render_log_path_for(app.storage.session_id)
    # O Textual escreve no terminal pelo driver próprio: não há stream ANSI.
    assert audit.ansi_path is None
    assert not app.session_paths.render_ansi_path_for(app.storage.session_id).exists()

    renderer.log_debug_event("tui_resize", width=80, height=24)
    audit.close()
    assert "tui_resize" in audit.events_path.read_text(encoding="utf-8")


def test_debug_exposes_tui_screenshot_and_points_the_prompt_at_it(make_app):
    renderer = _ScreenshotRenderer()
    app = make_app(renderer, debug=True)

    executor = app.tool_executor
    assert executor.is_tui_screenshot_available() is True
    assert "tui_screenshot" in {s["function"]["name"] for s in resolve_tool_schemas(executor)}

    result = executor.execute(ToolCall(name="tui_screenshot", arguments={"filename": "boot"}))
    assert result.ok is True, result.error
    screenshots_dir = app.session_paths.render_screenshots_dir
    assert result.data["path"] == str(screenshots_dir / "boot.svg")
    assert (screenshots_dir / "boot.svg").read_text(encoding="utf-8") == "<svg/>"
    assert renderer.captures == [None]

    state = app.prompt_builder.session_state
    assert state["render_debug_active"] is True
    assert state["tui_screenshot_available"] is True
    assert state["render_screenshots_dir"] == str(screenshots_dir)
    assert state["render_ansi_path"] == ""
    assert "tui_screenshot" in app.prompt_builder.build(
        agent="codex", history=[{"role": "human", "content": "olhe a tela"}]
    )


def test_without_debug_the_tool_stays_hidden_and_no_audit_is_attached(make_app):
    renderer = _ScreenshotRenderer()
    app = make_app(renderer, debug=False)

    assert renderer.audit_logger is None
    assert app.tool_executor.is_tui_screenshot_available() is False
    assert "tui_screenshot" not in {
        s["function"]["name"] for s in resolve_tool_schemas(app.tool_executor)
    }
    state = app.prompt_builder.session_state
    assert state["tui_screenshot_available"] is False
    assert state["render_screenshots_dir"] == ""


def test_debug_without_screenshot_capable_renderer_keeps_the_tool_hidden(make_app):
    renderer = _PlainRenderer()
    app = make_app(renderer, debug=True)

    assert renderer.audit_logger is not None
    assert app.tool_executor.is_tui_screenshot_available() is False
    assert app.prompt_builder.session_state["tui_screenshot_available"] is False

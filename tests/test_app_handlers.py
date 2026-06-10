import logging
from types import SimpleNamespace
from unittest.mock import Mock

from quimera.app.handlers import PromptAwareStderrHandler


def test_prompt_aware_stderr_handler_routes_warning_to_app_callback():
    """Verifica que prompt aware stderr handler routes warning to app callback."""
    handler = PromptAwareStderrHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s: %(message)s"))
    app = SimpleNamespace(
        _nonblocking_input_status="reading",
        show_warning_message=Mock(),
    )
    handler.bind_app(app)

    record = logging.LogRecord(
        name="quimera.staging",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="retry for agent=%s",
        args=("claude",),
        exc_info=None,
    )

    handler.emit(record)

    app.show_warning_message.assert_called_once()
    rendered = app.show_warning_message.call_args[0][0]
    assert "retry for agent=claude" in rendered


def test_prompt_aware_stderr_handler_suppresses_mcp_info_while_prompt_reading_without_debug():
    """Verifica que prompt aware stderr handler suppresses mcp info while prompt reading without debug."""
    handler = PromptAwareStderrHandler()
    app = SimpleNamespace(
        _nonblocking_input_status="reading",
        debug_prompt_metrics=False,
        show_muted_message=Mock(),
    )
    handler.bind_app(app)

    record = logging.LogRecord(
        name="quimera.runtime.mcp.server",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="MCP tools/call done tool=call_agent ok=True duration_ms=10",
        args=(),
        exc_info=None,
    )

    handler.emit(record)
    app.show_muted_message.assert_not_called()


def test_prompt_aware_stderr_handler_shows_mcp_info_while_prompt_reading_in_debug():
    """Verifica que prompt aware stderr handler shows mcp info while prompt reading in debug."""
    handler = PromptAwareStderrHandler()
    app = SimpleNamespace(
        _nonblocking_input_status="reading",
        debug_prompt_metrics=True,
        show_muted_message=Mock(),
    )
    handler.bind_app(app)

    record = logging.LogRecord(
        name="quimera.runtime.mcp.server",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="MCP tools/call done tool=call_agent ok=True duration_ms=10",
        args=(),
        exc_info=None,
    )

    handler.emit(record)
    app.show_muted_message.assert_called_once()

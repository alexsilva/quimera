import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import quimera.app.config as app_config
from quimera.app.handlers import PromptAwareStderrHandler


def _app_with_system_layer(**extra):
    system_layer = extra.pop("system_layer", None) or Mock()
    return SimpleNamespace(system_layer=system_layer, **extra)


def test_prompt_aware_stderr_handler_routes_warning_to_app_callback():
    """Verifica que prompt aware stderr handler routes warning to app callback."""
    handler = PromptAwareStderrHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s: %(message)s"))
    app = _app_with_system_layer(
        _nonblocking_input_status="reading",
        system_layer=Mock(show_warning_message=Mock()),
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

    app.system_layer.show_warning_message.assert_called_once()
    rendered = app.system_layer.show_warning_message.call_args[0][0]
    assert "retry for agent=claude" in rendered


def test_prompt_aware_stderr_handler_suppresses_mcp_info_while_prompt_reading_without_debug():
    """Verifica que prompt aware stderr handler suppresses mcp info while prompt reading without debug."""
    handler = PromptAwareStderrHandler()
    app = _app_with_system_layer(
        _nonblocking_input_status="reading",
        debug_prompt_metrics=False,
        system_layer=Mock(show_muted_message=Mock()),
    )
    handler.bind_app(app)

    record = logging.LogRecord(
        name="quimera.runtime.mcp.server",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="MCP tools/call done tool=delegate ok=True duration_ms=10",
        args=(),
        exc_info=None,
    )

    handler.emit(record)
    app.system_layer.show_muted_message.assert_not_called()


def test_prompt_aware_stderr_handler_shows_mcp_info_while_prompt_reading_in_debug():
    """Verifica que prompt aware stderr handler shows mcp info while prompt reading in debug."""
    handler = PromptAwareStderrHandler()
    app = _app_with_system_layer(
        _nonblocking_input_status="reading",
        debug_prompt_metrics=True,
        system_layer=Mock(show_muted_message=Mock()),
    )
    handler.bind_app(app)

    record = logging.LogRecord(
        name="quimera.runtime.mcp.server",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="MCP tools/call done tool=delegate ok=True duration_ms=10",
        args=(),
        exc_info=None,
    )

    handler.emit(record)
    app.system_layer.show_muted_message.assert_called_once()


def test_quimera_root_logger_does_not_route_internal_warnings_to_ui():
    system_layer = Mock(show_warning_message=Mock())
    app = _app_with_system_layer(
        _nonblocking_input_status="reading",
        system_layer=system_layer,
    )
    previous_app = app_config.handler._app
    previous_callbacks = app_config.handler._callbacks
    app_config.handler.bind_app(app)
    try:
        logging.getLogger("quimera.runtime.process_supervisor").warning(
            "registrando processo durante shutdown"
        )
    finally:
        app_config.handler._app = previous_app
        app_config.handler._callbacks = previous_callbacks

    system_layer.show_warning_message.assert_not_called()


def test_quimera_root_logger_remains_audited_after_log_file_change(tmp_path):
    log_path = tmp_path / "quimera.log"
    previous_log_path = Path(app_config._file_handler.baseFilename)
    try:
        app_config.set_app_log_file(log_path)

        logging.getLogger("quimera.runtime.process_supervisor").warning("audit-only")

        for handler in logging.getLogger("quimera").handlers:
            handler.flush()

        assert "audit-only" in log_path.read_text(encoding="utf-8")
    finally:
        app_config.set_app_log_file(previous_log_path)

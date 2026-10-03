import logging
from io import StringIO
from pathlib import Path

import quimera.app.config as app_config
from quimera.app.handlers import PromptAwareStderrHandler


def _record(level, msg, name="quimera.staging"):
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1, msg=msg, args=(), exc_info=None
    )


def _handler():
    stream = StringIO()
    handler = PromptAwareStderrHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    return handler, stream


def test_screen_handler_keeps_runtime_warnings_and_errors_off_the_screen():
    """Com a UI ativa, WARNING/ERROR do logger de tela não vão ao terminal nem ficam retidos."""
    handler, stream = _handler()
    handler.mark_ui_active()

    handler.emit(_record(logging.WARNING, "retry for agent=claude"))
    handler.emit(_record(logging.ERROR, "falha no backend agent=claude"))
    handler.drain_to_stderr()

    assert stream.getvalue() == ""


def test_screen_handler_buffers_startup_warnings_until_drained():
    """Antes da UI, WARNING+ ficam retidos e saem no stderr apenas no drain."""
    handler, stream = _handler()

    handler.emit(_record(logging.INFO, "boot info"))
    handler.emit(_record(logging.WARNING, "plugin sem config"))
    assert stream.getvalue() == ""

    handler.drain_to_stderr()
    assert stream.getvalue() == "WARNING plugin sem config\n"

    handler.drain_to_stderr()
    assert stream.getvalue() == "WARNING plugin sem config\n"


def test_staging_logger_errors_stay_in_the_log_file(tmp_path):
    """O arquivo de log segue recebendo o ERROR que deixou de aparecer no chat."""
    log_path = tmp_path / "quimera.log"
    previous_log_path = Path(app_config._file_handler.baseFilename)
    previous_state = app_config.handler._ui_active
    try:
        app_config.set_app_log_file(log_path)
        app_config.handler.mark_ui_active()

        app_config.logger.error("falha no backend agent=%s", "claudecloud-fable")

        for handler in app_config.logger.handlers:
            handler.flush()
        assert "falha no backend agent=claudecloud-fable" in log_path.read_text(encoding="utf-8")
    finally:
        app_config.handler._ui_active = previous_state
        app_config.set_app_log_file(previous_log_path)


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

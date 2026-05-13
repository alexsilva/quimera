import logging
from types import SimpleNamespace
from unittest.mock import Mock

from quimera.app.handlers import PromptAwareStderrHandler


def test_prompt_aware_stderr_handler_routes_warning_to_app_callback():
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

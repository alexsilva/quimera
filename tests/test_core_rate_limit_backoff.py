import pytest
from pathlib import Path
from quimera.app.core import QuimeraApp


def test_rate_limit_backoff_seconds_default(tmp_path: Path):
    """Test that RATE_LIMIT_BACKOFF_SECONDS defaults to 30 when not set."""
    app = QuimeraApp(cwd=tmp_path)
    # The task_services should be initialized after QuimeraApp __init__
    assert app.task_services is not None
    # The internal getter for rate limit backoff should return the default
    backoff_seconds = app.task_services._get_rate_limit_backoff_seconds()
    assert backoff_seconds == 30


def test_rate_limit_backoff_seconds_can_be_set(tmp_path: Path, monkeypatch):
    """Test that RATE_LIMIT_BACKOFF_SECONDS can be set on the app instance."""
    app = QuimeraApp(cwd=tmp_path)
    # Set the attribute on the instance
    app.RATE_LIMIT_BACKOFF_SECONDS = 60
    backoff_seconds = app.task_services._get_rate_limit_backoff_seconds()
    assert backoff_seconds == 60
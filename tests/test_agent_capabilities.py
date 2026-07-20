"""Compatibilidade da fronteira pública de AgentClient."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from quimera.agents.capabilities import (
    get_cancel_event,
    get_pause_idle_if,
    is_agent_running,
    is_user_cancelled,
    mark_user_cancelled,
    share_cancel_event,
)
from quimera.agents.client import AgentClient


def test_public_agent_client_capabilities_are_consistent() -> None:
    client = AgentClient(MagicMock())
    event = threading.Event()

    client.share_cancel_event(event)
    client.user_cancelled = True

    assert client.cancel_event is event
    assert client.user_cancelled is True
    assert client.agent_running is False
    assert get_cancel_event(client) is event
    assert is_user_cancelled(client) is True
    assert get_pause_idle_if(client) is client.pause_idle_if


def test_capabilities_preserve_legacy_clients() -> None:
    event = threading.Event()
    legacy = SimpleNamespace(
        _cancel_event=event,
        _user_cancelled=False,
        _agent_running=True,
        _pause_idle_if="pause",
    )

    mark_user_cancelled(legacy)

    assert legacy._user_cancelled is True
    assert event.is_set()
    assert is_user_cancelled(legacy) is True
    assert is_agent_running(legacy) is True
    assert get_pause_idle_if(legacy) == "pause"


def test_magic_mock_does_not_fabricate_public_capabilities() -> None:
    client = MagicMock(spec=[])

    assert is_user_cancelled(client) is False
    assert is_agent_running(client) is False
    assert get_cancel_event(client) is None
    assert get_pause_idle_if(client) is None


def test_share_cancel_event_uses_legacy_fallback() -> None:
    legacy = SimpleNamespace()
    event = threading.Event()

    share_cancel_event(legacy, event)

    assert legacy._cancel_event is event

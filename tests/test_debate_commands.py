import pytest

from quimera.debate.commands import DebateCommandError, parse_debate_command
from quimera.debate.models import DebateMode


def test_parse_debate_start_with_strict_options():
    command = parse_debate_command(
        "/debate --mode workflow --agents claude,codex --rounds 3 "
        "--timeout 120 --quorum 2 --context "
        '"planejar entrega"'
    )

    assert command.action == "start"
    assert command.topic == "planejar entrega"
    assert command.include_context is True
    assert command.mode == DebateMode.WORKFLOW
    assert command.agents == ("claude", "codex")
    assert command.rounds == 3
    assert command.timeout_seconds == 120
    assert command.quorum == 2


def test_parse_debate_context_defaults_to_disabled():
    command = parse_debate_command("/debate tema simples")
    assert command.include_context is False


@pytest.mark.parametrize(
    ("raw", "action", "debate_id"),
    [
        ("/debate status", "status", ""),
        ("/debate cancel deb-1", "cancel", "deb-1"),
        ("/debate show deb-2", "show", "deb-2"),
        ("/debate apply deb-3", "apply", "deb-3"),
        ("/debate list", "list", ""),
    ],
)
def test_parse_debate_control_commands(raw, action, debate_id):
    parsed = parse_debate_command(raw)
    assert parsed.action == action
    assert parsed.debate_id == debate_id


@pytest.mark.parametrize(
    "raw",
    [
        "/debate",
        "/debate --rounds 0 tema",
        "/debate --timeout 2 tema",
        "/debate --unknown tema",
        "/debate apply",
        "/debate list extra",
        "/debate " + ("x" * 8_001),
    ],
)
def test_parse_debate_rejects_invalid_input(raw):
    with pytest.raises(DebateCommandError):
        parse_debate_command(raw)

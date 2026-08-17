import json

from quimera.debate.models import (
    DebateContribution,
    DebateLimits,
    DebateMode,
    DebateSession,
    DebateStatus,
)
from quimera.debate.prompts import build_contribution_prompt, build_synthesis_prompt


def _session(context=""):
    return DebateSession(
        id="deb-1",
        session_id="session-1",
        topic="decidir arquitetura",
        mode=DebateMode.VERDICT,
        status=DebateStatus.RUNNING,
        participants=("claude", "codex"),
        moderator="claude",
        limits=DebateLimits(max_rounds=2, timeout_seconds=60, quorum=2),
        context=context,
    )


def _snapshot(prompt):
    return json.loads(prompt.split("SNAPSHOT_JSON:\n", 1)[1])


def test_contribution_prompt_includes_context_in_snapshot():
    prompt = build_contribution_prompt(
        _session(context="bug intermitente no feed"),
        round_index=1,
        agent="claude",
        previous=(),
        candidate=None,
    )
    snapshot = _snapshot(prompt)
    assert snapshot["context"] == "bug intermitente no feed"
    assert "unverified claim" in prompt


def test_contribution_prompt_omits_empty_context():
    prompt = build_contribution_prompt(
        _session(),
        round_index=1,
        agent="claude",
        previous=(),
        candidate=None,
    )
    assert _snapshot(prompt)["context"] is None


def test_contribution_prompt_shares_current_round_and_urges_independence():
    earlier = DebateContribution(
        debate_id="deb-1",
        round_index=1,
        agent="claude",
        position="posicao previa",
    )
    prompt = build_contribution_prompt(
        _session(),
        round_index=1,
        agent="codex",
        previous=(),
        candidate=None,
        current=(earlier,),
    )
    snapshot = _snapshot(prompt)
    assert [item["agent"] for item in snapshot["current_round_contributions"]] == [
        "claude"
    ]
    assert "Stay independent" in prompt


def test_contribution_prompt_suggests_read_only_without_quimera_tool_names():
    prompt = build_contribution_prompt(
        _session(),
        round_index=1,
        agent="claude",
        previous=(),
        candidate=None,
    )
    assert "grep_search" not in prompt
    assert "web_fetch" not in prompt
    assert "read-only investigation" in prompt
    assert "whatever tools your environment provides" in prompt


def test_synthesis_prompt_includes_context_in_snapshot():
    prompt = build_synthesis_prompt(
        _session(context="bug intermitente no feed"),
        round_index=1,
        contributions=(),
        previous_candidate=None,
    )
    snapshot = _snapshot(prompt)
    assert snapshot["context"] == "bug intermitente no feed"
    assert "must not be cited as evidence" in prompt

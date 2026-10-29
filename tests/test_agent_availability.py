"""Agente que falha fica indisponível por prazo, em vez de sair do pool da sessão."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quimera.app.agent_failure_tracker import AgentFailureTracker
from quimera.app.agent_pool import AgentPool
from quimera.app.system_layer import AppSystemLayer
from quimera.tasks.failover import TaskFailoverPolicy
from quimera.ui.commands import build_agents_help, format_cooldown


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tracker(pool, **overrides):
    kwargs = dict(
        normalize_agent_name=lambda agent: str(agent),
        agent_pool=pool,
        release_agent_tasks=lambda _name: None,
    )
    kwargs.update(overrides)
    return AgentFailureTracker(**kwargs)


# ── AgentPool ──────────────────────────────────────────────────────────


def test_suspend_keeps_membership_but_hides_from_available():
    pool = AgentPool(["claude", "codex"], clock=FakeClock())

    pool.suspend("codex", seconds=300, reason="2 falhas seguidas")

    assert pool.agents == ["claude", "codex"]
    assert "codex" in pool
    assert pool.available_agents() == ["claude"]
    assert [s.name for s in pool.suspensions()] == ["codex"]


def test_suspend_requires_membership():
    pool = AgentPool(["claude"])
    with pytest.raises(ValueError):
        pool.suspend("codex", seconds=10, reason="x")


def test_take_primary_skips_suspended_and_rotates_from_the_picked_agent():
    pool = AgentPool(["codex", "claude", "gemini"], clock=FakeClock())
    pool.suspend("codex", seconds=300, reason="2 falhas seguidas")

    assert pool.take_primary() == "claude"
    assert pool.agents == ["codex", "gemini", "claude"]
    assert pool.take_primary() == "gemini"
    assert pool.take_primary() == "claude"
    assert pool.primary == "gemini"


def test_suspension_expires_with_the_clock():
    clock = FakeClock()
    pool = AgentPool(["claude", "codex"], clock=clock)
    pool.suspend("codex", seconds=300, reason="2 falhas seguidas")

    clock.advance(299)
    assert pool.available_agents() == ["claude"]
    clock.advance(1)
    assert pool.available_agents() == ["claude", "codex"]
    assert pool.suspensions() == []


def test_frozen_agent_suspended_falls_back_and_returns_when_available_again():
    clock = FakeClock()
    pool = AgentPool(["claude", "codex"], clock=clock)
    pool.set_orchestrator("codex")

    pool.suspend("codex", seconds=60, reason="2 falhas seguidas")
    assert pool.primary == "claude"
    assert pool.take_primary() == "claude"
    # O orquestrador não é perdido: só sai da rotação enquanto suspenso.
    assert pool.orchestrator_agent == "codex"

    clock.advance(60)
    assert pool.take_primary() == "codex"
    assert pool.take_primary() == "codex"


def test_all_suspended_probes_the_agent_that_returns_first():
    pool = AgentPool(["claude", "codex"], clock=FakeClock())
    pool.suspend("claude", seconds=600, reason="2 falhas seguidas")
    pool.suspend("codex", seconds=300, reason="2 falhas seguidas")

    assert pool.primary == "codex"
    assert pool.take_primary() == "codex"
    assert pool.agents == ["claude", "codex"]  # sem rotação na sondagem
    assert pool.available_agents() == []


def test_resume_reports_whether_a_suspension_was_active():
    pool = AgentPool(["claude"], clock=FakeClock())
    pool.suspend("claude", seconds=300, reason="2 falhas seguidas")

    assert pool.resume("claude") is True
    assert pool.resume("claude") is False
    assert pool.available_agents() == ["claude"]


def test_set_and_remove_drop_suspensions_of_departed_agents():
    pool = AgentPool(["claude", "codex", "gemini"], clock=FakeClock())
    pool.suspend("codex", seconds=300, reason="2 falhas seguidas")
    pool.suspend("gemini", seconds=300, reason="2 falhas seguidas")

    pool.set(["claude", "codex"])
    pool.remove("codex")
    pool.add("codex")
    pool.add("gemini")

    assert pool.suspensions() == []
    assert pool.available_agents() == ["claude", "codex", "gemini"]


def test_suspensions_report_reason_and_remaining_time():
    clock = FakeClock(100.0)
    pool = AgentPool(["claude"], clock=clock)
    pool.suspend("claude", seconds=300, reason="2 falhas seguidas")
    clock.advance(45)

    (suspended,) = pool.suspensions()
    assert suspended.name == "claude"
    assert suspended.reason == "2 falhas seguidas"
    assert suspended.remaining_seconds == pytest.approx(255)


# ── AgentFailureTracker ────────────────────────────────────────────────


def test_cooldown_doubles_per_strike_up_to_one_hour():
    assert [AgentFailureTracker.cooldown_seconds(n) for n in range(1, 7)] == [
        300, 600, 1200, 2400, 3600, 3600,
    ]


def test_single_failure_does_not_suspend():
    pool = AgentPool(["claude"], clock=FakeClock())
    _tracker(pool).record_failure("claude")
    assert pool.available_agents() == ["claude"]


def test_two_failures_suspend_for_five_minutes_and_release_tasks():
    pool = AgentPool(["claude", "codex"], clock=FakeClock())
    released = []
    tracker = _tracker(pool, release_agent_tasks=released.append)

    tracker.record_failure("claude")
    tracker.record_failure("claude")

    assert pool.agents == ["claude", "codex"]
    assert pool.available_agents() == ["codex"]
    (suspended,) = pool.suspensions()
    assert suspended.remaining_seconds == pytest.approx(300)
    assert suspended.reason == "2 falhas seguidas"
    assert released == ["claude"]


def test_success_resets_counters_and_resumes_the_agent():
    pool = AgentPool(["claude"], clock=FakeClock())
    tracker = _tracker(pool)
    tracker.record_failure("claude")
    tracker.record_failure("claude")

    tracker.record_success("claude")

    assert pool.available_agents() == ["claude"]
    assert tracker.failures["claude"] == 0
    # Depois do sucesso a sequência recomeça: uma falha não suspende.
    tracker.record_failure("claude")
    assert pool.available_agents() == ["claude"]
    # E a próxima suspensão volta ao prazo inicial.
    tracker.record_failure("claude")
    assert pool.suspensions()[0].remaining_seconds == pytest.approx(300)


def test_repeated_failures_after_the_cooldown_double_the_next_one():
    clock = FakeClock()
    pool = AgentPool(["claude"], clock=clock)
    tracker = _tracker(pool)
    tracker.record_failure("claude")
    tracker.record_failure("claude")
    clock.advance(300)
    assert pool.available_agents() == ["claude"]

    tracker.record_failure("claude")  # a sondagem falhou

    assert pool.available_agents() == []
    assert pool.suspensions()[0].remaining_seconds == pytest.approx(600)


def test_tracker_tolerates_agents_outside_the_pool():
    pool = AgentPool(["claude"], clock=FakeClock())
    tracker = _tracker(pool)

    tracker.record_failure("gemini")
    tracker.record_failure("gemini")
    tracker.record_success("gemini")

    assert pool.available_agents() == ["claude"]
    assert tracker.failures["gemini"] == 0


def test_bug_is_filed_once_per_streak_even_with_repeated_suspensions():
    clock = FakeClock()
    pool = AgentPool(["claude"], clock=clock)
    file_bug = Mock()
    tracker = _tracker(pool, file_bug=file_bug, get_session_id=lambda: "s")

    for _ in range(3):
        tracker.record_failure("claude")
        clock.advance(4000)

    assert file_bug.call_count == 1


# ── Consumidores do pool ───────────────────────────────────────────────


def test_failover_policy_on_available_agents_ignores_suspended_until_expiry():
    clock = FakeClock()
    pool = AgentPool(["claude", "codex"], clock=clock)
    profile = SimpleNamespace(name="x", prefix="/x", aliases=(), supports_tools=True, is_mcp_capable=True)
    policy = TaskFailoverPolicy(
        active_agents=pool.available_agents,
        get_agent_profile=lambda name: SimpleNamespace(**{**vars(profile), "name": name, "prefix": f"/{name}"}),
        repository=SimpleNamespace(can_reassign_task=lambda task_id, candidates: bool(candidates)),
    )
    pool.suspend("codex", seconds=300, reason="2 falhas seguidas")

    assert policy.is_active_agent("codex") is False
    assert policy.review_agents_for("claude") == []
    assert policy.can_failover(1, "claude") is False

    clock.advance(300)
    assert policy.is_active_agent("codex") is True
    assert policy.can_failover(1, "claude") is True


def test_agents_command_marks_suspended_agents_with_reason_and_return_time():
    renderer = SimpleNamespace(show_system=Mock(), show_warning=Mock(), flush=Mock())
    pool = AgentPool(["claude", "codex"], clock=FakeClock())
    pool.suspend("codex", seconds=300, reason="2 falhas seguidas")
    layer = AppSystemLayer(
        agent_pool=pool,
        renderer=renderer,
        clear_screen=Mock(),
        context_manager=SimpleNamespace(),
        approval_handler_getter=Mock(return_value=None),
    )

    assert layer.handle_command("/agents") is True

    text = renderer.show_system.call_args.args[0]
    assert "- /claude <mensagem>: Claude responde\n" in text
    assert "- /codex <mensagem>: Codex responde — indisponível: 2 falhas seguidas, volta em 5 min" in text


def test_build_agents_help_without_notes_is_unchanged():
    assert build_agents_help(["claude"]) == "\nAgentes:\n- /claude <mensagem>: Claude responde"
    assert build_agents_help([]) == "\nAgentes:\n- nenhum"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0 s"), (45, "45 s"), (59.6, "1 min"), (300, "5 min"), (3599, "59 min"), (3600, "1 h"), (5400, "1 h 30 min")],
)
def test_format_cooldown(seconds, expected):
    assert format_cooldown(seconds) == expected

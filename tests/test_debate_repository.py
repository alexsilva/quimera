from dataclasses import replace

from quimera.debate.models import (
    DebateLimits,
    DebateEvidence,
    DebateMode,
    DebateSession,
    DebateStatus,
    DebateSynthesis,
    WorkItem,
)
from quimera.debate.repository import DebateRepository
from quimera.tasks.repository import TaskRepository


def _session(debate_id="deb-1"):
    return DebateSession(
        id=debate_id,
        session_id="session-1",
        topic="decidir arquitetura",
        mode=DebateMode.WORKFLOW,
        status=DebateStatus.CREATED,
        participants=("claude", "codex"),
        moderator="claude",
        limits=DebateLimits(max_rounds=2, timeout_seconds=60, quorum=2),
    )


def test_repository_persists_result_and_workflow(tmp_path):
    db_path = str(tmp_path / "state.db")
    TaskRepository(db_path)
    repository = DebateRepository(db_path)
    session = _session()
    repository.create_session(session)
    synthesis = DebateSynthesis(
        debate_id=session.id,
        round_index=1,
        moderator="claude",
        summary="sintese",
        verdict="plano",
        evidence=(
            DebateEvidence(
                "E1",
                "quimera/debate/service.py",
                1,
                1,
                "Asynchronous coordinator",
                "o servico coordena debates",
            ),
        ),
        evidence_ids=("E1",),
        work_items=(
            WorkItem(
                "T1",
                "base",
                "implementar base",
                assigned_to="codex",
                evidence_ids=("E1",),
            ),
            WorkItem(
                "T2",
                "review",
                "revisar",
                assigned_to="claude",
                dependencies=("T1",),
                evidence_ids=("E1",),
            ),
        ),
    )

    repository.add_synthesis(synthesis)
    repository.set_status(
        session.id,
        DebateStatus.EXHAUSTED,
        current_round=2,
        result=replace(synthesis, round_index=2),
    )

    loaded = repository.get_session(session.id)
    assert loaded is not None
    assert loaded.status == DebateStatus.EXHAUSTED
    assert loaded.result is not None
    assert loaded.result.verdict == "plano"
    assert loaded.result.evidence[0].source == "quimera/debate/service.py"
    assert loaded.result.evidence_ids == ("E1",)
    assert loaded.result.work_items[0].evidence_ids == ("E1",)
    assert [item.id for item in repository.get_work_items(session.id)] == ["T1", "T2"]


def test_repository_recovers_incomplete_sessions(tmp_path):
    db_path = str(tmp_path / "state.db")
    TaskRepository(db_path)
    repository = DebateRepository(db_path)
    repository.create_session(_session("deb-running"))
    repository.set_status("deb-running", DebateStatus.RUNNING)

    assert repository.recover_incomplete(reason="restart") == 1
    loaded = repository.get_session("deb-running")
    assert loaded is not None
    assert loaded.status == DebateStatus.FAILED
    assert loaded.error == "restart"


def test_repository_recovers_only_expired_active_sessions(tmp_path):
    db_path = str(tmp_path / "state.db")
    TaskRepository(db_path)
    repository = DebateRepository(db_path)
    expired = replace(
        _session("deb-expired"),
        limits=DebateLimits(max_rounds=2, timeout_seconds=60, quorum=2),
    )
    live = replace(
        _session("deb-live"),
        limits=DebateLimits(max_rounds=2, timeout_seconds=1_000_000_000, quorum=2),
    )
    repository.create_session(expired)
    repository.create_session(live)
    repository.set_status(expired.id, DebateStatus.RUNNING)
    repository.set_status(live.id, DebateStatus.SYNTHESIZING)

    assert repository.recover_expired(now="2030-01-01 00:00:00") == 1
    assert repository.get_session(expired.id).status == DebateStatus.FAILED
    assert repository.get_session(live.id).status == DebateStatus.SYNTHESIZING

"""Contratos de plano, handoff, persistência atômica, claim e fences dos workflows."""
import threading

import pytest

from quimera.constants import TaskStatus
from quimera.workflows.handoff import Handoff, evaluate, parse_handoff, verify_files
from quimera.workflows.models import (
    WorkflowError,
    WorkflowLimits,
    WorkflowStatus,
    can_transition_workflow,
)
from quimera.workflows.plan import PlanValidationError, parse_plan, plan_hash
from quimera.workflows.repository import PlanVersionConflict
from tests.workflow_harness import Harness, handoff_text, plan_payload, task_spec

LIMITS = WorkflowLimits()


def _plan(tasks, **kw):
    return parse_plan(plan_payload(tasks, **kw), coordinator="claude", limits=LIMITS)


# ── Planejamento ─────────────────────────────────────────────────────────────


def test_plan_accepts_valid_dag_and_derives_write_mode():
    plan = _plan([
        task_spec("A"),
        task_spec("B", task_type="code_edit", deps=["A"]),
        task_spec("C", deps=["A", "B"], writes=True),
    ])
    assert [spec.writes for spec in plan.tasks] == [False, True, True]
    assert plan.objective == "Corrigir sincronizacao"


@pytest.mark.parametrize(
    "tasks, fragment",
    [
        ([task_spec("A"), task_spec("A")], "duplicados"),
        ([task_spec("A", deps=["Z"])], "desconhecidos"),
        ([task_spec("A", deps=["B"]), task_spec("B", deps=["A"])], "ciclo"),
        ([task_spec("A", deps=["A"])], "si mesmo"),
        ([{**task_spec("A"), "acceptance_criteria": []}], "criterio de aceite"),
        ([{**task_spec("A"), "task_type": "magia"}], "task_type invalido"),
        ([{**task_spec("A"), "writes": "sim"}], "booleano"),
        ([], "nao vazia"),
    ],
)
def test_plan_rejects_invalid_structures(tasks, fragment):
    with pytest.raises(PlanValidationError, match=fragment):
        _plan(tasks)


def test_plan_rejects_limits_and_missing_objective():
    many = [task_spec(f"T{i}") for i in range(LIMITS.max_tasks + 1)]
    with pytest.raises(PlanValidationError, match="limite"):
        _plan(many)
    with pytest.raises(PlanValidationError, match="objective"):
        parse_plan({"work_items": [task_spec("A")]}, coordinator="c", limits=LIMITS)
    with pytest.raises(PlanValidationError):
        parse_plan("nao e objeto", coordinator="c", limits=LIMITS)


def test_plan_hash_is_stable_and_content_sensitive():
    a = _plan([task_spec("A")])
    assert plan_hash(a) == plan_hash(_plan([task_spec("A")]))
    assert plan_hash(a) != plan_hash(_plan([task_spec("A", evidence=["log"])]))


def test_workflow_state_machine():
    assert can_transition_workflow("ready", "running")
    assert can_transition_workflow("running", "blocked")
    assert can_transition_workflow("completed", "running")
    assert not can_transition_workflow("cancelled", "running")
    assert not can_transition_workflow("ready", "completed")
    assert not can_transition_workflow("running", "inexistente")


# ── Handoff ──────────────────────────────────────────────────────────────────


def _spec(**kw):
    return _plan([task_spec("A", **kw)]).tasks[0]


def test_handoff_structured_roundtrip_and_acceptance():
    text = handoff_text("achei a causa", files=["a.py"], checks=["pytest -q"], refs=["log.txt"])
    handoff = parse_handoff(text, task_id=7, attempt=1, agent="codex")
    assert handoff.structured and handoff.status == "success"
    assert Handoff.from_json(handoff.to_json()) == handoff
    assert evaluate(handoff, _spec(evidence=["teste"])) == (True, "achei a causa")


def test_unstructured_response_is_not_evidence():
    handoff = parse_handoff("Tudo certo, concluido!", task_id=1, attempt=0, agent="x")
    assert not handoff.structured
    ok, reason = evaluate(handoff, _spec(evidence=["saida de teste"]))
    assert not ok and "nao e estruturado" in reason
    assert evaluate(handoff, _spec())[0]  # sem evidencia esperada, aceita com limitacao
    assert handoff.limitations


def test_handoff_rejects_missing_evidence_failed_checks_and_partial():
    spec = _spec(evidence=["teste"])
    empty = parse_handoff(handoff_text("ok"), task_id=1, attempt=0, agent="x")
    assert not evaluate(empty, spec)[0]
    partial = parse_handoff(
        handoff_text("quase", status="partial", refs=["r"], limitations=["falta X"]),
        task_id=1, attempt=0, agent="x",
    )
    assert "parcial" in evaluate(partial, spec)[1]
    text = handoff_text("rodei", checks=["pytest"]).replace('"passed"', '"failed"')
    failed = parse_handoff(text, task_id=1, attempt=0, agent="x")
    assert "verificacoes falharam" in evaluate(failed, spec)[1]


def test_handoff_verifies_declared_files(tmp_path):
    (tmp_path / "real.py").write_text("x")
    text = handoff_text("mudei", files=["real.py", "fantasma.py", "../fora.py"], refs=["r"])
    handoff = verify_files(parse_handoff(text, task_id=1, attempt=0, agent="x"), tmp_path)
    assert handoff.unverified_files == ("fantasma.py", "../fora.py")
    assert "inexistentes" in evaluate(handoff, _spec())[1]


def test_handoff_truncation_is_flagged_and_bounded():
    handoff = parse_handoff(handoff_text("x" * 5000, refs=["r"]), task_id=1, attempt=0, agent="a")
    assert handoff.truncated and len(handoff.summary) <= 2000


# ── Persistência: publicação atômica e idempotente ───────────────────────────


def test_publish_is_atomic_and_tasks_not_claimable_until_start(tmp_path):
    h = Harness(tmp_path, ["a1"])
    payload = plan_payload([task_spec("A"), task_spec("B", deps=["A"])])
    result = h.coordinator.create(payload, requested_by="claude")
    assert result.created and result.workflow.status == WorkflowStatus.READY
    snap = h.workflows.snapshot(result.workflow.id)
    assert [v.status for v in snap.tasks] == ["pending", "pending"]
    assert h.tasks.claim_task("a1") is None  # workflow ready: nada reivindicavel
    h.coordinator.start(result.workflow.id)
    first = h.tasks.claim_task("a1")
    assert first == snap.tasks[0].link.task_id
    assert h.tasks.claim_task("a1") is None  # B depende de A


def test_publish_rolls_back_when_creation_fails_midway(tmp_path, monkeypatch):
    h = Harness(tmp_path, ["a1"])
    original = h.tasks.insert_task
    calls = []

    def flaky(conn, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disco cheio")
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(h.tasks, "insert_task", flaky)
    with pytest.raises(RuntimeError):
        h.coordinator.create(plan_payload([task_spec("A"), task_spec("B")]), requested_by="c")
    assert h.tasks.list_tasks() == []
    assert h.workflows.list_workflows() == []


def test_publish_is_idempotent_even_concurrently(tmp_path):
    h = Harness(tmp_path, ["a1"])
    payload = plan_payload([task_spec("A"), task_spec("B", deps=["A"])])
    results, errors = [], []

    def publish():
        try:
            results.append(h.coordinator.create(payload, requested_by="claude"))
        except Exception as exc:  # noqa: BLE001 - o teste exige zero erros
            errors.append(exc)

    threads = [threading.Thread(target=publish) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len({r.workflow.id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    assert len(h.tasks.list_tasks()) == 2
    wf_id = results[0].workflow.id
    task_ids = [v.link.task_id for v in h.workflows.snapshot(wf_id).tasks]
    assert h.tasks.list_task_dependencies(task_ids[1]) == [task_ids[0]]


def test_open_workflow_limit(tmp_path):
    h = Harness(tmp_path, ["a1"], limits=WorkflowLimits(max_open_workflows=1))
    h.coordinator.create(plan_payload([task_spec("A")], objective="um"), requested_by="c")
    with pytest.raises(WorkflowError, match="workflows abertos"):
        h.coordinator.create(plan_payload([task_spec("A")], objective="dois"), requested_by="c")


# ── Claim, fences e isolamento de escrita ────────────────────────────────────


def test_pause_blocks_new_claims_and_resume_restores(tmp_path):
    h = Harness(tmp_path, ["a1"])
    wf = h.coordinator.create(plan_payload([task_spec("A"), task_spec("B")]), requested_by="c").workflow
    h.coordinator.start(wf.id)
    h.coordinator.pause(wf.id)
    assert h.tasks.claim_task("a1") is None
    h.coordinator.resume(wf.id)
    assert h.tasks.claim_task("a1") is not None


def test_two_concurrent_claims_reserve_the_task_once(tmp_path):
    """Cenário E: tentativas simultâneas pela mesma task."""
    h = Harness(tmp_path, [f"a{i}" for i in range(8)])
    wf = h.coordinator.create(plan_payload([task_spec("A")]), requested_by="c").workflow
    h.coordinator.start(wf.id)
    barrier = threading.Barrier(8)
    winners = []

    def claim(agent):
        barrier.wait()
        task_id = h.tasks.claim_task(agent)
        if task_id:
            winners.append((agent, task_id))

    threads = [threading.Thread(target=claim, args=(f"a{i}",)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1


def test_stale_attempt_cannot_overwrite_newer_run(tmp_path):
    h = Harness(tmp_path, ["a1", "a2"])
    wf = h.coordinator.create(plan_payload([task_spec("A")]), requested_by="c").workflow
    h.coordinator.start(wf.id)
    task_id = h.tasks.claim_task("a1")
    stale_attempt = h.tasks.list_tasks({"id": task_id})[0].attempt_count
    assert h.tasks.requeue_task(task_id, "a1", reason="timeout")
    assert h.tasks.claim_task("a2") == task_id
    assert not h.tasks.complete_task(task_id, result="velho", expected_attempt=stale_attempt)
    assert not h.tasks.submit_for_review(task_id, result="velho", expected_attempt=stale_attempt)
    assert not h.tasks.fail_task(task_id, reason="velho", expected_attempt=stale_attempt)
    assert h.tasks.list_tasks({"id": task_id})[0].status == TaskStatus.IN_PROGRESS
    current = h.tasks.list_tasks({"id": task_id})[0].attempt_count
    assert h.tasks.complete_task(task_id, result="novo", expected_attempt=current)


def test_write_tasks_are_serialized_while_reads_stay_parallel(tmp_path):
    """Cenário I: política conservadora de exclusividade de escrita."""
    h = Harness(tmp_path, ["a1", "a2", "a3"])
    wf = h.coordinator.create(
        plan_payload([
            task_spec("W1", task_type="code_edit"),
            task_spec("W2", task_type="code_edit"),
            task_spec("R1"),
        ]),
        requested_by="c",
    ).workflow
    h.coordinator.start(wf.id)
    snap = h.workflows.snapshot(wf.id)
    ids = {v.link.item_id: v.link.task_id for v in snap.tasks}
    assert h.tasks.claim_task("a1") == ids["W1"]
    assert h.tasks.claim_task("a2") == ids["R1"]  # leitura segue em paralelo
    assert h.tasks.claim_task("a3") is None  # W2 espera a escrita exclusiva
    assert h.tasks.submit_for_review(ids["W1"], result="r")  # em review ainda segura o recurso
    assert h.tasks.claim_task("a3") is None
    assert h.tasks.claim_review_task("a3") == ids["W1"]
    assert h.tasks.complete_task(ids["W1"], result="r")
    assert h.tasks.claim_task("a3") == ids["W2"]


def test_review_claims_follow_workflow_state(tmp_path):
    h = Harness(tmp_path, ["a1", "a2"])
    wf = h.coordinator.create(plan_payload([task_spec("A")]), requested_by="c").workflow
    h.coordinator.start(wf.id)
    task_id = h.tasks.claim_task("a1")
    h.tasks.submit_for_review(task_id, result="r")
    h.coordinator.pause(wf.id)
    assert h.tasks.claim_review_task("a2") is None
    h.coordinator.resume(wf.id)
    assert h.tasks.claim_review_task("a2") == task_id


# ── Replanejamento ───────────────────────────────────────────────────────────


def _running(h, tasks, **kw):
    wf = h.coordinator.create(plan_payload(tasks, **kw), requested_by="claude").workflow
    return h.coordinator.start(wf.id)


def test_replan_preserves_valid_tasks_and_invalidates_affected(tmp_path):
    h = Harness(tmp_path, ["a1"])
    wf = _running(h, [task_spec("A"), task_spec("B", deps=["A"]), task_spec("C", deps=["B"])])
    ids = {v.link.item_id: v.link.task_id for v in h.workflows.snapshot(wf.id).tasks}
    claimed = h.tasks.claim_task("a1")
    assert h.tasks.complete_task(claimed, result="ok")
    revised = plan_payload([
        task_spec("A"),
        {**task_spec("B", deps=["A"]), "description": "nova abordagem"},
        task_spec("C", deps=["B"]),
        task_spec("D", deps=["C"]),
    ])
    result = h.coordinator.replan(wf.id, revised, base_version=1)
    assert result.applied
    assert result.kept_task_ids == (ids["A"],)  # A concluida e identica: preservada
    assert set(result.retired_task_ids) == {ids["B"], ids["C"]}  # B mudou; C em cascata
    snap = h.workflows.snapshot(wf.id)
    assert {v.link.item_id for v in snap.tasks} == {"A", "B", "C", "D"}
    assert snap.workflow.plan_version == 2 and snap.workflow.replans == 1
    assert h.workflows.plan(wf.id, 1).tasks[1].item.description != "nova abordagem"  # historico
    old = {t.id: t.status for t in h.tasks.list_tasks({"ids": [ids["B"], ids["C"]]})}
    assert set(old.values()) == {"cancelled"}
    new_b = next(v for v in snap.tasks if v.link.item_id == "B")
    assert h.tasks.list_task_dependencies(new_b.link.task_id) == [ids["A"]]


def test_replan_is_idempotent_and_guards_version(tmp_path):
    h = Harness(tmp_path, ["a1"])
    wf = _running(h, [task_spec("A")])
    revised = plan_payload([task_spec("A"), task_spec("B", deps=["A"])])
    first = h.coordinator.replan(wf.id, revised, base_version=1)
    again = h.coordinator.replan(wf.id, revised, base_version=2)
    assert first.applied and not again.applied
    assert len(h.tasks.list_tasks({"origin": "workflow"})) == 2
    with pytest.raises(PlanVersionConflict):
        h.coordinator.replan(wf.id, plan_payload([task_spec("Z")]), base_version=1)


def test_replan_limit_is_enforced(tmp_path):
    h = Harness(tmp_path, ["a1"], limits=WorkflowLimits(max_replans=1))
    wf = _running(h, [task_spec("A")])
    h.coordinator.replan(wf.id, plan_payload([task_spec("A"), task_spec("B")]), base_version=1)
    with pytest.raises(WorkflowError, match="limite"):
        h.coordinator.replan(
            wf.id, plan_payload([task_spec("A"), task_spec("B"), task_spec("C")]), base_version=2
        )


# ── Regressões da revisão independente ───────────────────────────────────────


@pytest.mark.parametrize("eligible", [True, False])
def test_unrunnable_pending_task_fails_and_blocks_instead_of_hanging(tmp_path, eligible):
    """Task requeued a pending sem tentativas restantes (ou sem agente) falha de fato."""
    agents = ["a1", "a2"] if eligible else ["a1"]
    h = Harness(tmp_path, agents, limits=WorkflowLimits(max_task_attempts=1 if eligible else 3))
    wf = _running(h, [task_spec("A")])
    task_id = h.tasks.claim_task("a1")
    assert h.tasks.requeue_task(task_id, "a1", reason="review rejeitada")
    assert h.tasks.list_tasks({"id": task_id})[0].status == TaskStatus.PENDING
    record = h.coordinator.reconcile(wf.id)
    assert h.tasks.list_tasks({"id": task_id})[0].status == TaskStatus.FAILED
    assert record.status == WorkflowStatus.BLOCKED  # nunca fica preso em running


def test_replan_keeps_completed_task_when_only_assignee_spelling_differs(tmp_path):
    h = Harness(tmp_path, ["a1"])
    tasks = [{**task_spec("A"), "assigned_to": "/A1"}, task_spec("B", deps=["A"])]
    wf = _running(h, tasks)
    ids = {v.link.item_id: v.link.task_id for v in h.workflows.snapshot(wf.id).tasks}
    assert h.tasks.claim_task("a1") == ids["A"]
    assert h.tasks.complete_task(ids["A"], result="ok")
    result = h.coordinator.replan(
        wf.id, plan_payload([*tasks, task_spec("C", deps=["A"])]), base_version=1
    )
    assert set(result.kept_task_ids) == {ids["A"], ids["B"]}  # nada concluido e refeito
    assert result.retired_task_ids == ()


def test_paused_workflow_review_does_not_stall_other_workflows_writes(tmp_path):
    h = Harness(tmp_path, ["a1", "a2"])
    first = _running(h, [task_spec("W", task_type="code_edit")])
    writer = h.tasks.claim_task("a1")
    assert h.tasks.submit_for_review(writer, result="r")
    h.coordinator.pause(first.id)  # revisao de W nao progride enquanto pausado
    second = h.coordinator.create(
        plan_payload([task_spec("X", task_type="code_edit")], objective="outro"),
        requested_by="claude",
    ).workflow
    h.coordinator.start(second.id)
    other = h.workflows.snapshot(second.id).tasks[0].link.task_id
    assert h.tasks.claim_task("a2") == other


def test_settle_does_not_fail_a_task_claimed_after_the_snapshot(tmp_path):
    """Defeito ao vivo: o limite de tentativas falhou uma task ja reivindicada por outro worker."""
    h = Harness(tmp_path, ["a1", "a2"], limits=WorkflowLimits(max_task_attempts=1))
    wf = _running(h, [task_spec("A")])
    task_id = h.tasks.claim_task("a1")
    assert h.tasks.requeue_task(task_id, "a1", reason="falhou")
    stale = h.workflows.snapshot(wf.id)  # pending, tentativas esgotadas
    assert h.tasks.claim_task("a2") == task_id  # worker reivindica antes da decisao
    h.coordinator._settle_unrunnable(stale)
    assert h.tasks.list_tasks({"id": task_id})[0].status == TaskStatus.IN_PROGRESS


def test_released_run_marks_the_removed_agent_and_counts_one_attempt(tmp_path):
    """Defeito ao vivo: agente removido do pool reivindicava de novo e gastava 2 tentativas."""
    h = Harness(tmp_path, ["a1", "a2"])
    wf = _running(h, [task_spec("A")])
    task_id = h.tasks.claim_task("a1")
    h.tasks.release_agent_tasks("a1")  # AgentFailureTracker removeu a1 do pool
    task = h.tasks.list_tasks({"id": task_id})[0]
    assert (task.status, task.attempt_count) == (TaskStatus.PENDING, 1)
    assert h.tasks.claim_task("a1") is None  # a1 nao reivindica a mesma task de novo
    assert h.tasks.claim_task("a2") == task_id
    assert h.workflows.get(wf.id).status == WorkflowStatus.RUNNING


# ── Regressões da revisão final ──────────────────────────────────────────────


def test_released_legacy_task_keeps_previous_semantics(tmp_path):
    """Só tasks de workflow ganham tentativa/failed_agents ao liberar o agente removido."""
    h = Harness(tmp_path, ["a1"])
    task_id = h.tasks.create_task(h.job_id, "task de /task")
    assert h.tasks.claim_task("a1") == task_id
    h.tasks.release_agent_tasks("a1")
    task = h.tasks.list_tasks({"id": task_id})[0]
    assert (task.status, task.attempt_count) == (TaskStatus.PENDING, 0)
    assert h.tasks.claim_task("a1") == task_id  # o agente que volta ao pool pode retomá-la


def test_requeue_at_the_attempt_limit_fails_without_a_claim_window(tmp_path):
    h = Harness(tmp_path, ["a1", "a2", "a3"], limits=WorkflowLimits(max_task_attempts=2))
    wf = _running(h, [task_spec("A")])
    task_id = h.tasks.claim_task("a1")
    assert h.tasks.requeue_task(task_id, "a1", reason="falhou", max_attempts=2)
    assert h.tasks.list_tasks({"id": task_id})[0].status == TaskStatus.PENDING
    assert h.tasks.claim_task("a2") == task_id
    assert h.tasks.submit_for_review(task_id, result="r")
    assert h.tasks.claim_review_task("a3") == task_id
    assert h.tasks.requeue_task_after_review(task_id, "a2", notes="rejeitada", max_attempts=2)
    task = h.tasks.list_tasks({"id": task_id})[0]
    assert (task.status, task.attempt_count) == (TaskStatus.FAILED, 2)
    assert "limite de 2 tentativas" in task.notes
    assert h.tasks.claim_task("a3") is None
    assert h.coordinator.reconcile(wf.id).status == WorkflowStatus.BLOCKED


def test_pending_review_without_possible_reviewer_blocks_the_workflow(tmp_path):
    h = Harness(tmp_path, ["a1", "a2"])
    wf = _running(h, [task_spec("A")])
    task_id = h.tasks.claim_task("a1")
    assert h.tasks.submit_for_review(task_id, result="r")
    assert h.coordinator.reconcile(wf.id).status == WorkflowStatus.RUNNING  # a2 pode revisar
    h.active = ["a1"]  # o unico revisor possivel saiu do pool
    record = h.coordinator.reconcile(wf.id)
    assert record.status == WorkflowStatus.BLOCKED
    assert "revisao sem revisor elegivel" in record.status_reason
    assert h.tasks.list_tasks({"id": task_id})[0].status == TaskStatus.PENDING_REVIEW


def test_conclusion_is_bound_to_the_plan_version_it_verified(tmp_path):
    """Um replan aplicado entre o snapshot e a conclusao invalida a decisao."""
    h = Harness(tmp_path, ["a1"])
    wf = _running(h, [task_spec("A")])
    task_id = h.tasks.claim_task("a1")
    assert h.tasks.complete_task(task_id, result="ok")
    original = h.workflows.snapshot

    def racing(workflow_id):
        snapshot = original(workflow_id)
        h.workflows.snapshot = original
        h.coordinator.replan(
            workflow_id, plan_payload([task_spec("A"), task_spec("B", deps=["A"])]), base_version=1
        )
        return snapshot

    h.workflows.snapshot = racing
    record = h.coordinator.reconcile(wf.id)
    assert (record.status, record.plan_version) == (WorkflowStatus.RUNNING, 2)
    assert h.statuses(wf.id) == {"A": "completed", "B": "pending"}
    assert h.workflows.set_status(
        wf.id, WorkflowStatus.COMPLETED, expected=[WorkflowStatus.RUNNING], expected_version=1
    ) is None


# ── Regressões da revisão independente (Codex/Sonnet) ────────────────────────


def _age(h, task_id, stamp="2000-01-01 00:00:00"):
    with h.tasks.transaction() as conn:
        conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, task_id))


def test_orphan_requeue_does_not_touch_a_newer_claim(tmp_path):
    """Snapshot viu a tentativa antiga órfã; ela foi encerrada e um novo claim veio antes do requeue."""
    h = Harness(tmp_path, ["a1", "a2"])
    wf = _running(h, [task_spec("A")])
    task_id = h.tasks.claim_task("a1")
    _age(h, task_id)
    stale = h.workflows.snapshot(wf.id)
    observed = {v.link.task_id: (v.status, v.attempt_count, v.updated_at) for v in stale.tasks}
    assert h.tasks.requeue_task(task_id, "a1", reason="falhou", max_attempts=5)
    assert h.tasks.claim_task("a2") == task_id  # nova tentativa viva
    assert h.workflows.requeue_orphans(wf.id, observed, "lease expirado") == ()
    task = h.tasks.list_tasks({"id": task_id})[0]
    assert (task.status, task.assigned_to, task.attempt_count) == (TaskStatus.IN_PROGRESS, "a2", 1)


def test_orphan_requeue_respects_the_attempt_limit(tmp_path):
    h = Harness(tmp_path, ["a1"], limits=WorkflowLimits(max_task_attempts=1))
    wf = _running(h, [task_spec("A")])
    task_id = h.tasks.claim_task("a1")
    h.workflows.begin_attempt(task_id, 0, "a1", "run")
    _age(h, task_id)
    view = h.workflows.snapshot(wf.id).tasks[0]
    observed = {task_id: (view.status, view.attempt_count, view.updated_at)}
    assert h.workflows.requeue_orphans(wf.id, observed, "lease expirado") == (task_id,)
    task = h.tasks.list_tasks({"id": task_id})[0]
    assert task.status == TaskStatus.FAILED and "limite de 1" in task.notes
    assert h.tasks.claim_task("a1") is None
    assert h.workflows.list_attempts(task_id)[-1].status == "interrupted"


def test_recover_pauses_in_the_same_transaction_as_adoption(tmp_path):
    """Nenhum claim do novo job entre a adoção e a pausa."""
    h = Harness(tmp_path, ["a1"])
    wf = _running(h, [task_spec("A")])
    task_id = h.tasks.claim_task("a1")
    h.workflows.begin_attempt(task_id, 0, "a1", "run")
    assert h.workflows.adopt(wf.id, h.job_id, pause_reason="reinicio") is True
    assert h.workflows.get(wf.id).status == WorkflowStatus.PAUSED
    assert h.tasks.list_tasks({"id": task_id})[0].status == TaskStatus.PENDING
    assert h.tasks.claim_task("a1") is None  # workflow pausado: gate fechado
    assert h.workflows.list_attempts(task_id)[-1].status == "interrupted"


def test_result_cas_rejects_a_stale_status_or_version(tmp_path):
    h = Harness(tmp_path, ["a1"])
    wf = _running(h, [task_spec("A")])
    h.coordinator.cancel(wf.id)
    assert not h.workflows.set_result(
        wf.id, "resposta tardia", expected_status=WorkflowStatus.BLOCKED, expected_version=1
    )
    assert h.workflows.get(wf.id).result is None


def test_late_attempt_outcome_never_overwrites_a_recorded_verdict(tmp_path):
    h = Harness(tmp_path, ["a1"])
    _running(h, [task_spec("A")])
    task_id = h.tasks.claim_task("a1")
    h.workflows.begin_attempt(task_id, 0, "a1", "run")
    h.workflows.finish_attempt(task_id, 0, "a1", "submitted", handoff="{}")
    h.workflows.finish_attempt(task_id, 0, "a1", "accepted")
    h.workflows.finish_attempt(task_id, 0, "a1", "failed", failure_reason="tardio")
    h.workflows.finish_attempt(task_id, 0, "a1", "rejected", failure_reason="tardio")
    attempt = h.workflows.list_attempts(task_id)[-1]
    assert (attempt.status, attempt.failure_reason) == ("accepted", None)


def test_cancel_closes_open_attempts(tmp_path):
    h = Harness(tmp_path, ["a1"])
    wf = _running(h, [task_spec("A")])
    task_id = h.tasks.claim_task("a1")
    h.workflows.begin_attempt(task_id, 0, "a1", "run")
    h.coordinator.cancel(wf.id)
    assert h.workflows.list_attempts(task_id)[-1].status == "cancelled"

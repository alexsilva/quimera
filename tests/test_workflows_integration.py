"""Cenários A–H: workflows ponta a ponta com executores, runner e reviewer reais."""
import threading
import time

import pytest

from quimera.domain.task_states import TaskStatus
from quimera.workflows.models import WorkflowLimits, WorkflowStatus
from quimera.workflows.presentation import render_text
from tests.workflow_harness import Harness, handoff_text, plan_payload, task_spec

REVIEW = "task-review"


def _is_review(delegation):
    return str(delegation["delegation_id"]).startswith(REVIEW)


def accepting(execute):
    """Agente cujo trabalho é ``execute(delegation)`` e que aceita qualquer revisão."""

    def script(agent, delegation):
        if _is_review(delegation):
            return "ACEITE\nEvidencia conferida."
        return execute(agent, delegation)

    return script


@pytest.fixture
def make(tmp_path):
    created = []

    def build(agents, **kw):
        harness = Harness(tmp_path, agents, **kw)
        created.append(harness)
        return harness

    yield build
    for harness in created:
        harness.stop()


def _start(h, tasks, **kw):
    wf = h.coordinator.create(plan_payload(tasks, **kw), requested_by="claude").workflow
    h.coordinator.start(wf.id)
    return wf


def test_scenario_a_parallel_then_dependent_waits_for_both(make):
    h = make(["a1", "a2", "a3"])
    barrier = threading.Barrier(2, timeout=10)
    seen_at_t3 = {}

    def execute(agent, delegation):
        item = delegation["task"]
        if "T1" in item or "T2" in item:
            barrier.wait()  # só passa se T1 e T2 estiverem executando ao mesmo tempo
            time.sleep(0.2)
            return handoff_text(f"{item} ok", refs=[f"{item}.log"], checks=["inspecao"])
        seen_at_t3.update(h.statuses(wf.id))
        return handoff_text("T3 ok", refs=["t3.log"], checks=["integracao"])

    h.scripts["*"] = accepting(execute)
    wf = _start(h, [task_spec("T1"), task_spec("T2"), task_spec("T3", deps=["T1", "T2"])])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    assert seen_at_t3["T1"] == "completed" and seen_at_t3["T2"] == "completed"
    assert h.statuses(wf.id) == {"T1": "completed", "T2": "completed", "T3": "completed"}
    assert h.consults and h.consults[0][1].startswith("completed:v1")  # decisao final consultada 1x


def test_scenario_b_context_handoff_is_traceable_and_scoped(make):
    h = make(["a1", "a2"])

    def execute(agent, delegation):
        item = delegation["task"]
        if "T1" in item:
            return handoff_text(
                "causa: corrida no flush", files=["quimera/x.py"], checks=["pytest tests/x"],
                refs=["data/logs/t1.log"],
            )
        if "T2" in item:
            return handoff_text("reproduzi o erro", refs=["data/logs/t2.log"], checks=["repro.sh"])
        return handoff_text("corrigido", refs=["r"], checks=["pytest"])

    (h.tmp_path / "quimera").mkdir()
    (h.tmp_path / "quimera" / "x.py").write_text("pass")
    h.scripts["*"] = accepting(execute)
    wf = _start(h, [task_spec("T1"), task_spec("T2"), task_spec("T3", deps=["T1", "T2"])])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    context = next(d["context"] for _a, d in h.delegations if "T3" in d["task"] and not _is_review(d))
    assert "Corrigir sincronizacao" in context  # objetivo do workflow
    assert "causa: corrida no flush" in context and "data/logs/t1.log" in context  # T1
    assert "reproduzi o erro" in context and "data/logs/t2.log" in context  # T2
    assert 'untrusted="true"' in context and "task_id=" in context
    assert "nao alterar migracoes" in context  # restricoes aplicaveis
    t1_context = next(d["context"] for _a, d in h.delegations if "T1" in d["task"] and not _is_review(d))
    assert "reproduzi o erro" not in t1_context  # sem vazar resultados nao relacionados
    runs = [d for _a, d in h.delegations]
    # Investigacao roda com workspace somente leitura; run ids rastreiam workflow/task/tentativa.
    assert all(d["read_only"] is True for d in runs if not _is_review(d))
    assert all(d["run_id"].startswith(f"agentrun:wf{wf.id}-t") for d in runs)
    assert any("-r0-" in d["run_id"] for d in runs if _is_review(d))


def test_scenario_c_rejected_review_keeps_dependents_blocked(make):
    h = make(["a1", "a2"])
    first_review = threading.Event()
    gate = threading.Event()
    t2_started_before_accept = []

    def script(agent, delegation):
        item = delegation["task"]
        if _is_review(delegation):
            if not first_review.is_set():
                first_review.set()
                return "RETENTATIVA\nSem evidencia suficiente."
            gate.set()
            return "ACEITE\nok"
        if "T2" in item and not gate.is_set():
            t2_started_before_accept.append(agent)
        return handoff_text(f"{item} feito", refs=["r"], checks=["c"])

    h.scripts["*"] = script
    wf = _start(h, [task_spec("T1"), task_spec("T2", deps=["T1"])])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    assert t2_started_before_accept == []  # dependente so rodou apos o aceite
    t1 = next(v for v in h.workflows.snapshot(wf.id).tasks if v.link.item_id == "T1")
    attempts = h.workflows.list_attempts(t1.link.task_id)
    assert [a.status for a in attempts].count("rejected") == 1
    assert attempts[-1].status == "accepted" and t1.attempt_count == 1


def test_scenario_d_failure_blocks_then_replan_recovers_without_duplication(make):
    h = make(["a1", "a2"], consult_answer=None)
    broken = {"on": True}

    def execute(agent, delegation):
        item = delegation["task"]
        if "T2" in item and broken["on"]:
            return "Nao consigo executar isso."
        return handoff_text(f"{item} ok", refs=["r"], checks=["c"])

    h.scripts["*"] = accepting(execute)
    wf = _start(h, [task_spec("T1"), task_spec("T2", deps=["T1"]), task_spec("T3", deps=["T2"])])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.BLOCKED)
    statuses = h.statuses(wf.id)
    assert statuses == {"T1": "completed", "T2": "failed", "T3": "pending"}  # T3 nunca liberada
    assert h.consults and h.consults[-1][1].startswith("blocked:v1:T2")
    t1_id = next(v.link.task_id for v in h.workflows.snapshot(wf.id).tasks if v.link.item_id == "T1")

    broken["on"] = False
    revised = plan_payload([
        task_spec("T1"),
        {**task_spec("T2", deps=["T1"]), "description": "abordagem alternativa"},
        task_spec("T3", deps=["T2"]),
    ])
    result = h.coordinator.replan(wf.id, revised, base_version=1)
    assert result.kept_task_ids == (t1_id,)
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    live = h.workflows.snapshot(wf.id)
    assert [v.link.item_id for v in live.tasks] == ["T1", "T2", "T3"]
    assert sum(1 for t in h.tasks.list_tasks({"origin": "workflow"}) if t.description == "Tarefa T1") == 1
    assert live.workflow.replans == 1


def test_scenario_d_insufficient_evidence_returns_to_the_same_agent(make):
    h = make(["a1", "a2"])
    t1_runs = []

    def execute(agent, delegation):
        if "T1" in delegation["task"]:
            t1_runs.append((agent, delegation["context"]))
            if len(t1_runs) == 1:
                return "Pronto, resolvi."  # sem handoff: evidencia esperada ausente
        return handoff_text("ok", refs=["r"], checks=["c"])

    h.scripts["*"] = accepting(execute)
    wf = _start(
        h,
        [
            task_spec("T1", evidence=["saida de teste"], assigned_to="a1"),
            task_spec("T2", deps=["T1"]),
        ],
    )
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    t1 = next(v for v in h.workflows.snapshot(wf.id).tasks if v.link.item_id == "T1")
    attempts = h.workflows.list_attempts(t1.link.task_id)
    assert [a.status for a in attempts] == ["insufficient_evidence", "accepted"]
    # A entrega volta para quem a fez, com o motivo; nenhuma afirmacao textual virou sucesso.
    assert [agent for agent, _ in t1_runs] == ["a1", "a1"]
    assert (t1.assigned_to, t1.rework_count) == ("a1", 1)
    assert "Entrega anterior devolvida" in t1_runs[1][1]
    assert "evidencia esperada" in t1_runs[1][1]


def test_blocked_when_no_eligible_agent_remains(make):
    h = make(["a1"], consult_answer="preciso de decisao")
    h.scripts["*"] = accepting(lambda agent, d: None)  # sem resposta e sem outro agente
    wf = _start(h, [task_spec("T1"), task_spec("T2", deps=["T1"])])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.BLOCKED)
    record = h.workflows.get(wf.id)
    assert record.result == "preciso de decisao"  # sintese do agente guardada, nao inventada
    assert h.statuses(wf.id)["T2"] == "pending"


def test_coordinator_can_confirm_blocked_after_automatic_block(make):
    """Consultado no bloqueio automático, o principal confirma com finish blocked sem erro."""
    h = make(["a1"], consult_answer=None)
    h.scripts["*"] = accepting(lambda agent, d: "Nao consigo.")
    wf = _start(h, [task_spec("T1")])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.BLOCKED)
    record = h.coordinator.finish(wf.id, outcome="blocked", summary="falta acesso ao banco")
    assert record.status == WorkflowStatus.BLOCKED
    assert record.result == "falta acesso ao banco"


def test_task_attempt_limit_fails_instead_of_looping(make):
    h = make(["a1", "a2"], limits=WorkflowLimits(max_task_attempts=1))
    h.scripts["*"] = accepting(lambda agent, d: "Nao consigo.")
    wf = _start(h, [task_spec("T1")])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.BLOCKED)
    assert h.statuses(wf.id)["T1"] == "failed"


def test_scenario_g_restart_reconciles_without_declaring_completion(make):
    h = make(["a1", "a2"])
    wf = _start(h, [task_spec("T1"), task_spec("T2", deps=["T1"])])
    t1 = h.workflows.snapshot(wf.id).tasks[0].link.task_id
    assert h.tasks.claim_task("a1") == t1  # execucao em andamento quando o app morre
    interrupted_attempt = h.tasks.list_tasks({"id": t1})[0].attempt_count

    coordinator = h.new_session()  # novo processo, novo job
    recovered = coordinator.recover()
    assert [w.status for w in recovered] == [WorkflowStatus.PAUSED]
    record = h.workflows.get(wf.id)
    assert record.job_id == h.job_id and "recuperado" in record.status_reason
    task = h.tasks.list_tasks({"id": t1})[0]
    assert task.status == TaskStatus.PENDING and task.attempt_count == interrupted_attempt + 1
    # a execucao antiga, se ainda responder, nao sobrescreve nem conclui nada
    assert not h.tasks.complete_task(t1, result="tardio", expected_attempt=interrupted_attempt)
    assert h.tasks.claim_task("a1") is None  # pausado ate o operador retomar

    h.scripts["*"] = accepting(lambda agent, d: handoff_text("ok", refs=["r"], checks=["c"]))
    coordinator.resume(wf.id)
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)


def test_expired_lease_without_live_run_is_requeued(make):
    h = make(["a1"], limits=WorkflowLimits(lease_seconds=0))
    wf = _start(h, [task_spec("T1")])
    t1 = h.workflows.snapshot(wf.id).tasks[0].link.task_id
    assert h.tasks.claim_task("a1") == t1
    time.sleep(1.1)
    h.coordinator.reconcile(wf.id)
    task = h.tasks.list_tasks({"id": t1})[0]
    assert task.status == TaskStatus.PENDING and task.attempt_count == 1
    h.live.add(t1)  # execucao viva nao e tocada
    assert h.tasks.claim_task("a1") == t1
    time.sleep(1.1)
    h.coordinator.reconcile(wf.id)
    assert h.tasks.list_tasks({"id": t1})[0].status == TaskStatus.IN_PROGRESS


def test_scenario_h_cancel_stops_only_its_own_runs(make):
    h = make(["a1", "a2", "a3"])
    started, release = threading.Event(), threading.Event()

    def execute(agent, delegation):
        if "A1" in delegation["task"]:
            started.set()
            release.wait(10)  # execucao longa que sera cancelada
            return handoff_text("tardio", refs=["r"], checks=["c"])
        return handoff_text("ok", refs=["r"], checks=["c"])

    h.scripts["*"] = accepting(execute)
    wf_a = _start(h, [task_spec("A1"), task_spec("A2", deps=["A1"])], objective="workflow A")
    h.start()
    assert started.wait(10)
    wf_b = _start(h, [task_spec("B1")], objective="workflow B")  # nao relacionado

    h.coordinator.cancel(wf_a.id, reason="usuario cancelou")
    a1 = next(v.link.task_id for v in h.workflows.snapshot(wf_a.id).tasks if v.link.item_id == "A1")
    assert h.interrupted and a1 in h.interrupted[-1]  # execucao ativa foi alcancada
    b_tasks = {v.link.task_id for v in h.workflows.snapshot(wf_b.id).tasks}
    assert not (b_tasks & {tid for batch in h.interrupted for tid in batch})
    assert set(h.statuses(wf_a.id).values()) == {"cancelled"}

    release.set()  # resultado chega depois do cancelamento
    assert h.wait_for(lambda: h.workflows.get(wf_b.id).status == WorkflowStatus.COMPLETED)
    time.sleep(0.3)
    assert set(h.statuses(wf_a.id).values()) == {"cancelled"}  # nada ressuscitou
    attempts = h.workflows.list_attempts(a1)
    assert attempts[-1].status == "cancelled"
    assert h.workflows.get(wf_a.id).status == WorkflowStatus.CANCELLED
    assert h.coordinator.cancel(wf_a.id).status == WorkflowStatus.CANCELLED  # idempotente


def test_agent_pool_change_releases_assignment_to_unavailable_agent(make):
    h = make(["a1", "a2"])
    wf = _start(h, [{**task_spec("T1"), "assigned_to": "a1"}])
    outside = h.tasks.create_task(h.job_id, "task fora do workflow", assigned_to="a1")
    h.active.remove("a1")
    h.agents = ["a2"]
    h.coordinator.reconcile(wf.id)
    # Só a task do workflow é redistribuída; trabalho do agente fora dele não é tocado.
    assert h.tasks.list_tasks({"id": outside})[0].assigned_to == "a1"
    h.scripts["*"] = accepting(lambda agent, d: handoff_text("ok", refs=["r"], checks=["c"]))
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    assert h.statuses(wf.id) == {"T1": "completed"}



def test_cancelled_run_interrupts_without_blaming_the_agent(tmp_path):
    """Cancelar o workflow interrompe a execucao e encerra a tentativa como cancelada."""
    from quimera.tasks.classifiers import classify_task_execution_result
    from quimera.tasks.failover import TaskFailoverPolicy
    from quimera.tasks.runner import TaskRunner
    from quimera.workflows.execution import WorkflowTaskHooks
    from tests.workflow_harness import _Profile, _Silent

    h = Harness(tmp_path, ["a1", "a2"])
    wf = _start(h, [task_spec("T1")])
    task_id = h.tasks.claim_task("a1")
    task = h.tasks.list_tasks({"id": task_id})[0]

    class _Interrupted:
        def delegate(self, agent, **kwargs):
            h.coordinator.cancel(wf.id)  # cancelamento chega durante a execucao
            return None  # o AgentClient interrompido nao devolve resposta

    policy = TaskFailoverPolicy(
        active_agents=lambda: ["a1", "a2"],
        get_agent_profile=lambda name: _Profile(name=name),
        repository=h.tasks,
    )
    runner = TaskRunner(
        _Interrupted(), _Silent(), h.tasks, policy, classify_task_execution_result,
        lambda: False,
        task_hooks=WorkflowTaskHooks(h.workflows, h.tasks, tmp_path),
    )
    assert runner.run(task, "a1") is False
    assert h.tasks.list_tasks({"id": task_id})[0].status == TaskStatus.CANCELLED
    assert h.workflows.list_attempts(task_id)[-1].status == "cancelled"


def test_runner_rework_at_the_limit_is_final_without_another_claim(tmp_path):
    """Com max_task_attempts=1 a entrega reprovada pelo runtime encerra a task na hora."""
    h = Harness(tmp_path, ["a1", "a2"], limits=WorkflowLimits(max_task_attempts=1))
    h.scripts["*"] = lambda agent, delegation: "Nao consigo executar isso."  # sem handoff
    wf = h.coordinator.create(plan_payload([task_spec("A")]), requested_by="claude").workflow
    h.coordinator.start(wf.id)
    h.start()
    try:
        # Sem tick do coordenador: quem encerra a task e o proprio requeue do runner.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and h.statuses(wf.id)["A"] != "failed":
            time.sleep(0.05)
        time.sleep(0.3)  # janela em que o outro executor reivindicaria uma task pending
    finally:
        h.stop()
    assert h.statuses(wf.id)["A"] == "failed"
    assert len(h.delegations) == 1  # nenhum segundo agente reivindicou a task
    assert h.coordinator.reconcile(wf.id).status == WorkflowStatus.BLOCKED


# ── Dono da task, troca por indisponibilidade e devolução (caso ao vivo 2026-11-02) ──


def test_live_case_rejected_delivery_goes_back_to_the_planned_agent(make):
    """Workflow 2 do PasswordManager: haiku pediu RETENTATIVA e a task foi parar no
    claudecloud-fable, sem cota, que travou. A entrega devolvida volta para o opus,
    o revisor sem cota não conta contra ele e o fable nunca executa a task."""
    h = make(["opus", "haiku", "fable"])
    reviews = []
    runs = []

    def script(agent, delegation):
        if agent == "fable":
            return None  # sem cota: nunca responde
        if _is_review(delegation):
            reviews.append(agent)
            if reviews.count("haiku") == 1:
                return "RETENTATIVA\nFalta o parecer em arquivo."
            return "ACEITE\nParecer conferido."
        runs.append((agent, delegation["context"]))
        return handoff_text("parecer", refs=["parecer.md"], checks=["reproducao"])

    h.scripts["*"] = script
    wf = _start(h, [task_spec("T1", assigned_to="opus")])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    assert [agent for agent, _ in runs] == ["opus", "opus"]
    assert "Falta o parecer em arquivo" in runs[1][1]  # o motivo da devolucao chega ao dono
    t1 = h.workflows.snapshot(wf.id).tasks[0]
    assert (t1.assigned_to, t1.rework_count) == ("opus", 1)
    attempts = h.workflows.list_attempts(t1.link.task_id)
    assert [(a.agent, a.status) for a in attempts] == [("opus", "rejected"), ("opus", "accepted")]
    assert "opus (tentativa 2, devolvida 1x)" in render_text(h.workflows.snapshot(wf.id))


def test_owner_without_response_hands_the_task_over_without_spending_a_delivery(make):
    h = make(["a1", "a2"])

    def execute(agent, delegation):
        if agent == "a1":
            return None  # dono do plano sem cota
        return handoff_text("ok", refs=["r"], checks=["c"])

    h.scripts["*"] = accepting(execute)
    wf = _start(h, [task_spec("T1", assigned_to="a1")])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    t1 = h.workflows.snapshot(wf.id).tasks[0]
    assert (t1.assigned_to, t1.rework_count) == ("a2", 0)
    attempts = h.workflows.list_attempts(t1.link.task_id)
    assert [(a.agent, a.status) for a in attempts] == [("a1", "failed"), ("a2", "accepted")]


def test_substitute_failing_midway_passes_the_task_on_again(make):
    """Rejeitada, a entrega volta ao substituto; se ele falhar depois, vai para outro."""
    h = make(["a1", "a2", "a3"], limits=WorkflowLimits(max_task_attempts=5))
    executions = []
    reviews = []

    def script(agent, delegation):
        if _is_review(delegation):
            reviews.append(agent)
            return "RETENTATIVA\nfalta evidencia" if len(reviews) == 1 else "ACEITE\nok"
        executions.append(agent)
        if agent == "a1" or (len(executions) == 3 and agent == executions[1]):
            return None  # dono sem cota; depois o substituto cai no meio da rodada
        return handoff_text("ok", refs=["r"], checks=["c"])

    h.scripts["*"] = script
    wf = _start(h, [task_spec("T1", assigned_to="a1")])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    first, substitute, again, last = executions
    assert (first, again) == ("a1", substitute)  # a devolucao voltou ao substituto
    assert last not in {"a1", substitute}  # a falha dele passou a task adiante
    t1 = h.workflows.snapshot(wf.id).tasks[0]
    assert (t1.assigned_to, t1.rework_count) == (last, 1)


def test_reviewer_without_response_does_not_count_against_the_executor(make):
    h = make(["a1", "a2", "a3"])

    def script(agent, delegation):
        if _is_review(delegation):
            return None if agent == "a2" else "ACEITE\nok"
        if agent != "a1":
            return None
        return handoff_text("ok", refs=["r"], checks=["c"])

    h.scripts["*"] = script
    wf = _start(h, [task_spec("T1", assigned_to="a1")])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    t1 = h.workflows.snapshot(wf.id).tasks[0]
    assert (t1.assigned_to, t1.attempt_count, t1.rework_count) == ("a1", 0, 0)
    assert h.tasks.list_tasks({"id": t1.link.task_id})[0].reviewed_by == "a3"


def test_structured_report_that_mentions_cannot_is_not_discarded(make):
    """Defeito ao vivo (task 574): relatório de 5 mil caracteres descartado por conter 'cannot'."""
    h = make(["a1", "a2"])
    report = "O atacante cannot bypass o token; nao consigo reproduzir sem o APK, ver limitacoes."

    def execute(agent, delegation):
        return report + "\n" + handoff_text("auditoria concluida", refs=["parecer.md"], checks=["poc"])

    h.scripts["*"] = accepting(execute)
    wf = _start(h, [task_spec("T1")])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    t1 = h.workflows.snapshot(wf.id).tasks[0]
    assert (t1.attempt_count, t1.rework_count) == (0, 0)


# ── Indisponível não é falha na task (revisão do workflow 4, V02/V03/V12) ──


def test_short_rate_limit_with_no_other_agent_waits_instead_of_failing(make):
    """V03: 429 de segundos no único agente. Antes a task falhava na hora."""
    h = make(["a1"])
    calls = []

    def script(agent, delegation):
        calls.append(agent)
        if len(calls) == 1:
            h.available = []  # o tracker suspende a1 pelo retry-after
            threading.Timer(0.3, lambda: setattr(h, "available", None)).start()
            return None
        return handoff_text("ok", refs=["r"], checks=["c"])

    h.scripts["*"] = script
    wf = _start(h, [task_spec("T1", assigned_to="a1")])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    t1 = h.workflows.snapshot(wf.id).tasks[0]
    assert calls == ["a1", "a1"]
    assert h.tasks.can_reassign_task(t1.link.task_id, ["a1"]) is True  # não foi excluído
    assert t1.rework_count == 0
    attempts = h.workflows.list_attempts(t1.link.task_id)
    # Sem revisor no pool a entrega conclui direto (fica "submitted" no histórico).
    assert [(a.agent, a.status) for a in attempts] == [("a1", "failed"), ("a1", "submitted")]


def test_reviewer_without_verdict_and_no_other_reviewer_blocks_keeping_the_delivery(make):
    """V02a/V12: antes a task falhava (resultado sobrescrito) ou o workflow ficava running sem revisor."""
    h = make(["a1", "r1"])

    def script(agent, delegation):
        if _is_review(delegation):
            return ""  # revisor disponível que não responde
        return handoff_text("entrega", refs=["r"], checks=["c"])

    h.scripts["*"] = script
    wf = _start(h, [task_spec("T1", assigned_to="a1")])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.BLOCKED)
    workflow = h.workflows.get(wf.id)
    assert workflow.status_reason == "revisao sem revisor elegivel no pool: T1"
    t1 = h.workflows.snapshot(wf.id).tasks[0]
    task = h.tasks.list_tasks({"id": t1.link.task_id})[0]
    assert task.status == TaskStatus.PENDING_REVIEW
    assert '"summary": "entrega"' in (task.result or "")


def test_review_waits_for_a_suspended_reviewer_and_says_so(make):
    """Revisor suspenso (rate limit) não trava nem falha: a revisão espera e o feed avisa."""
    h = make(["a1", "r1"])
    h.available = ["a1"]

    def script(agent, delegation):
        if _is_review(delegation):
            return "ACEITE\nok"
        return handoff_text("entrega", refs=["r"], checks=["c"])

    h.scripts["*"] = script
    wf = _start(h, [task_spec("T1", assigned_to="a1")])
    h.start()
    assert h.wait_for(lambda: h.statuses(wf.id)["T1"] == "pending_review")
    h.coordinator.reconcile(wf.id)
    assert h.workflows.get(wf.id).status == WorkflowStatus.RUNNING
    waiting = [n for n in h.notices if "aguardando revisor disponivel" in n]
    assert waiting == [f"[workflow {wf.id}] task {h.workflows.snapshot(wf.id).tasks[0].link.task_id} "
                       "(T1): aguardando revisor disponivel (suspensos: r1)"]
    h.available = None
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)


def test_report_reaches_reviewer_dependent_rework_and_coordinator(make):
    """Defeito A do workflow 4: só o JSON do handoff era gravado; a lista de achados se perdia."""
    from quimera.workflows.presentation import snapshot_payload

    h = make(["a1", "a2"])
    report = "## Achados\n1. reviewer.py:283 falha a task\n2. coordinator.py:464 ignora revisores"
    contexts = {"review": [], "T1": [], "T2": []}

    def script(agent, delegation):
        if _is_review(delegation):
            contexts["review"].append(delegation["context"])
            if len(contexts["review"]) == 1:
                return "RETENTATIVA\nfalta o cenario do achado 2"
            return "ACEITE\nachados conferidos"
        item = "T1" if "T1" in delegation["task"] else "T2"
        contexts[item].append(delegation["context"])
        return report + "\n" + handoff_text(f"{item}: 2 achados", refs=["r"], checks=["c"])

    h.scripts["*"] = script
    wf = _start(h, [task_spec("T1", assigned_to="a1"), task_spec("T2", deps=["T1"], assigned_to="a1")])
    h.start()
    assert h.wait_for(lambda: h.workflows.get(wf.id).status == WorkflowStatus.COMPLETED)
    assert "relatorio:\n## Achados\n1. reviewer.py:283" in contexts["review"][0]
    assert "relatorio entregue:\n## Achados" in contexts["T1"][1]  # refação parte do que entregou
    assert "relatorio:\n## Achados\n1. reviewer.py:283" in contexts["T2"][0]  # dependente recebe a entrega
    payload = snapshot_payload(h.workflows.snapshot(wf.id), include_results=True)
    assert payload["work_items"][0]["report"] == report + "\nTrabalho feito."  # texto fora do bloco
    assert payload["work_items"][0]["summary"] == "T1: 2 achados"


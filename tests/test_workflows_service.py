"""Comando /workflow, monitor, consulta ao agente principal e cancelamento seletivo no pool."""
import threading
import time
from types import SimpleNamespace

import pytest

from quimera.prompt_kinds import PromptKind
from quimera.tasks.classifiers import classify_task_execution_result
from quimera.tasks.runner import TaskRunner
from quimera.workflows.execution import WorkflowTaskHooks
from quimera.tasks.executor_pool import TaskExecutorPool, _BackgroundDispatchPerCall, _task_id_of
from quimera.workflows.commands import USAGE, WorkflowCommandHandler
from quimera.workflows.models import WorkflowStatus
from quimera.workflows.service import WorkflowService, build_decision_consult
from tests.workflow_harness import Harness, plan_payload, task_spec


def _handler(h):
    out = SimpleNamespace(system=[], warnings=[])
    handler = WorkflowCommandHandler(
        lambda: h.coordinator, show_system=out.system.append, show_warning=out.warnings.append
    )
    return handler, out


def test_workflow_command_lists_shows_and_controls(tmp_path):
    h = Harness(tmp_path, ["a1"])
    handler, out = _handler(h)
    handler.handle("/workflow")
    assert out.system == ["Nenhum workflow aberto."]
    wf = h.coordinator.create(plan_payload([task_spec("T1"), task_spec("T2", deps=["T1"])]), requested_by="claude").workflow
    h.coordinator.start(wf.id)

    handler.handle("/workflow list")
    text = out.system[-1]
    assert f"workflow {wf.id} [running]" in text and "Corrigir sincronizacao" in text
    assert "coordenador claude" in text and "[T2]" in text and "<- T1" in text
    assert "total" not in text or "tasks: 2" in text  # contagens vindas do banco
    handler.handle(f"/workflow pause {wf.id}")
    assert h.workflows.get(wf.id).status == WorkflowStatus.PAUSED
    handler.handle(f"/workflow resume {wf.id}")
    assert h.workflows.get(wf.id).status == WorkflowStatus.RUNNING
    handler.handle(f"/workflow cancel {wf.id}")
    assert h.workflows.get(wf.id).status == WorkflowStatus.CANCELLED
    assert out.warnings == []


def test_workflow_command_reports_errors_instead_of_hiding_them(tmp_path):
    h = Harness(tmp_path, ["a1"])
    handler, out = _handler(h)
    handler.handle("/workflow show abc")
    handler.handle("/workflow explode")
    handler.handle("/workflow show 999")
    assert out.warnings[0] == USAGE and out.warnings[1] == USAGE
    assert "nao encontrado" in out.warnings[2]
    unavailable = WorkflowCommandHandler(
        lambda: None, show_system=out.system.append, show_warning=out.warnings.append
    )
    unavailable.handle("/workflow")
    assert "indisponiveis" in out.warnings[-1]


def test_monitor_reconciles_in_background_and_stops(tmp_path):
    h = Harness(tmp_path, ["a1"])
    h.consult_answer = "tudo conferido"
    refreshed = []
    service = WorkflowService(h.coordinator, refresh_state=lambda: refreshed.append(1), poll_seconds=0.05)
    service.start()  # a recuperacao de partida roda aqui, antes de qualquer workflow desta sessao
    try:
        wf = h.coordinator.create(plan_payload([task_spec("T1")]), requested_by="claude").workflow
        h.coordinator.start(wf.id)
        task_id = h.workflows.snapshot(wf.id).tasks[0].link.task_id
        assert h.tasks.claim_task("a1") == task_id
        assert h.tasks.complete_task(task_id, result="ok")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and h.workflows.get(wf.id).result is None:
            service.wake()
            time.sleep(0.05)
        record = h.workflows.get(wf.id)
        assert record.status == WorkflowStatus.COMPLETED and record.result == "tudo conferido"
        assert refreshed  # shared_state atualizado a partir do banco
    finally:
        service.stop()
    assert len(h.consults) == 1  # consulta unica por decisao, mesmo com varios ciclos


def test_decision_consult_builds_delegation_and_surfaces_transport_failure():
    calls, notices = [], []
    workflow = SimpleNamespace(id=7, status=WorkflowStatus.BLOCKED, coordinator="claude")

    def delegate(agent, **kwargs):
        calls.append((agent, kwargs))
        return "decisao"

    consult = build_decision_consult(delegate, notify=notices.append)
    assert consult(workflow, "blocked:v1:T2", "estado persistido") == "decisao"
    agent, kwargs = calls[0]
    assert agent == "claude" and kwargs["prompt_kind"] == PromptKind.DELEGATION
    assert kwargs["delegation_only"] and kwargs["persist_history"] is False
    assert "estado persistido" in kwargs["delegation"]["context"]
    assert "workflows_finish" in kwargs["delegation"]["expected"]
    assert "workflows_replan" in kwargs["delegation"]["expected"]

    def broken(agent, **kwargs):
        raise ConnectionError("rede caiu")

    assert build_decision_consult(broken, notify=notices.append)(workflow, "x", "y") is None
    assert "falhou" in notices[0] and "rede caiu" in notices[0]  # falha visivel, estado preservado


def test_consult_budget_is_bounded_and_not_repeated(tmp_path):
    h = Harness(tmp_path, ["a1"])
    wf = h.coordinator.create(plan_payload([task_spec("T1")]), requested_by="c").workflow
    assert h.workflows.claim_consult(wf.id, "blocked:v1:T1")
    assert not h.workflows.claim_consult(wf.id, "blocked:v1:T1")  # mesmo motivo nao reconsulta
    for index in range(h.workflows.limits.max_consults):
        h.workflows.claim_consult(wf.id, f"motivo-{index}")
    assert h.workflows.get(wf.id).consults == h.workflows.limits.max_consults
    assert not h.workflows.claim_consult(wf.id, "outro-motivo")  # teto de consultas


# ── Pool: execuções vivas e cancelamento seletivo ────────────────────────────


class _Client:
    def __init__(self):
        self.cancelled = 0

    def cancel_active_work(self):
        self.cancelled += 1


def test_pool_tracks_runs_per_task_and_cancels_only_requested():
    pool = TaskExecutorPool()
    a, b, c = _Client(), _Client(), _Client()
    pool._track_task_run(10, a)
    pool._track_task_run(11, b)
    pool._track_task_run(11, c)
    assert pool.live_task_ids() == {10, 11}
    assert pool.cancel_task_runs([11, 99]) == [11]  # 99 nao tem execucao viva
    assert (a.cancelled, b.cancelled, c.cancelled) == (0, 1, 1)
    pool._untrack_task_run(11, b)
    pool._untrack_task_run(11, c)
    pool._untrack_task_run(10, a)
    assert pool.live_task_ids() == set()


@pytest.mark.parametrize(
    "delegation, expected",
    [
        ({"delegation_id": "task-12"}, 12),
        ({"delegation_id": "task-review-34"}, 34),
        ({"delegation_id": "workflow-3-decision"}, None),
        ({"delegation_id": "abc"}, None),
        ("texto", None),
        (None, None),
    ],
)
def test_task_id_is_derived_from_delegation_identity(delegation, expected):
    assert _task_id_of(delegation) == expected


def test_background_dispatch_registers_the_run_while_it_executes():
    seen = {}
    client = _Client()

    class _Bg:
        _agent_client_override = client
        _tool_executor_override = None

        def delegate(self, agent, **kwargs):
            seen["live"] = pool.live_task_ids()
            return "ok"

        def close(self):
            seen["closed"] = True

    pool = TaskExecutorPool()
    pool._create_background_dispatch_services = lambda: _Bg()
    pool.get_dispatch_services = lambda: None
    result = _BackgroundDispatchPerCall(pool).delegate("a1", delegation={"delegation_id": "task-5"})
    assert result == "ok" and seen == {"live": {5}, "closed": True}
    assert pool.live_task_ids() == set()  # desregistrada ao terminar


def test_run_registered_after_cancel_never_reaches_the_agent():
    """Cancelamento que chega entre o claim e o dispatch barra a execução no pool."""
    called = []

    class _Bg:
        _agent_client_override = _Client()
        _tool_executor_override = None

        def delegate(self, agent, **kwargs):
            called.append(agent)
            return "ok"

        def close(self):
            pass

    pool = TaskExecutorPool()
    pool._create_background_dispatch_services = lambda: _Bg()
    pool.get_dispatch_services = lambda: None
    assert pool.cancel_task_runs([5]) == []  # nada vivo ainda
    result = _BackgroundDispatchPerCall(pool).delegate("a1", delegation={"delegation_id": "task-5"})
    assert result is None and called == []
    assert pool.live_task_ids() == set()
    other = _BackgroundDispatchPerCall(pool).delegate("a1", delegation={"delegation_id": "task-6"})
    assert other == "ok" and called == ["a1"]  # outras tasks seguem normais


def test_runner_skips_dispatch_when_task_cancelled_after_claim(tmp_path):
    """Workflow cancelado entre o claim e o dispatch: o runner não chama o agente."""
    h = Harness(tmp_path, ["a1"])
    wf = h.coordinator.create(plan_payload([task_spec("T1")]), requested_by="c").workflow
    h.coordinator.start(wf.id)
    task_id = h.tasks.claim_task("a1")
    task = h.tasks.list_tasks({"id": task_id})[0]
    h.coordinator.cancel(wf.id)
    called = []

    class _Dispatch:
        def delegate(self, agent, **kwargs):
            called.append(agent)
            return "nao deveria rodar"

    policy = SimpleNamespace(review_agents_for=lambda _a: [], can_failover=lambda *_a: False)
    runner = TaskRunner(
        _Dispatch(), SimpleNamespace(show_muted_message=lambda _m: None), h.tasks, policy,
        classify_task_execution_result, lambda: False,
        task_hooks=WorkflowTaskHooks(h.workflows, h.tasks, tmp_path),
    )
    assert runner.run(task, "a1") is False
    assert called == []
    attempts = h.workflows.list_attempts(task_id)
    assert attempts and attempts[-1].status == "cancelled"
    assert h.tasks.list_tasks({"id": task_id})[0].status == "cancelled"


def test_cancel_via_coordinator_reaches_the_pool_run(tmp_path):
    h = Harness(tmp_path, ["a1"])
    pool = TaskExecutorPool()
    client = _Client()
    h.coordinator._interrupt_tasks = pool.cancel_task_runs
    wf = h.coordinator.create(plan_payload([task_spec("T1")]), requested_by="c").workflow
    h.coordinator.start(wf.id)
    task_id = h.tasks.claim_task("a1")
    pool._track_task_run(task_id, client)
    other = _Client()
    pool._track_task_run(task_id + 100, other)  # execucao de outra task nao e tocada
    h.coordinator.cancel(wf.id)
    assert client.cancelled == 1 and other.cancelled == 0
    assert threading.active_count() >= 1


def test_read_only_workflow_task_runs_with_read_only_mode():
    from quimera.modes import MODES, read_only_mode

    seen = []

    class _Bg:
        _agent_client_override = None
        _tool_executor_override = None

        def delegate(self, agent, **kwargs):
            return "ok"

    pool = TaskExecutorPool()
    pool.get_dispatch_services = lambda: None
    pool.get_execution_mode = lambda: None

    def create(**kwargs):
        seen.append(kwargs.get("execution_mode_override"))
        return _Bg()

    pool._create_background_dispatch_services = create
    per_call = _BackgroundDispatchPerCall(pool)
    per_call.delegate("a1", delegation={"delegation_id": "task-1", "read_only": True})
    per_call.delegate("a1", delegation={"delegation_id": "task-2", "read_only": False})
    mode = seen[0]
    assert mode.read_only_fs and "write_file" in mode.blocked_tools
    assert "run_shell" not in mode.blocked_tools  # leitura ainda pode rodar verificacoes
    assert seen[1] is None  # tarefa de escrita segue o modo da sessao
    # Nunca afrouxa um modo mais restrito ja ativo na sessao.
    assert read_only_mode(MODES["/review"]) is MODES["/review"]
    execute = read_only_mode(MODES["/execute"])
    assert execute.read_only_fs and "apply_patch" in execute.blocked_tools


def test_monitor_sleeps_without_running_workflows(tmp_path):
    h = Harness(tmp_path, ["a1"])
    reconciled = []
    original = h.coordinator.list_running

    def counting():
        reconciled.append(1)
        return original()

    h.coordinator.list_running = counting
    service = WorkflowService(h.coordinator, poll_seconds=0.02)
    service.start()
    try:
        time.sleep(0.3)
        assert len(reconciled) == 1  # so a verificacao de partida: sem poll ocioso no banco
        wf = h.coordinator.create(plan_payload([task_spec("T1")]), requested_by="c").workflow
        h.coordinator.start(wf.id)
        service.wake()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(reconciled) <= 3:
            time.sleep(0.02)
        assert len(reconciled) > 3  # com workflow em running o poll volta a cobrir eventos perdidos
    finally:
        service.stop()


def test_only_one_session_coordinates_the_workspace(tmp_path):
    """Revisao final: a segunda sessao nao adota nem pausa workflows vivos da primeira."""
    from quimera.workflows.models import WorkflowError
    from quimera.workflows.service import CoordinationLock

    h = Harness(tmp_path, ["a1"])
    first_job = h.job_id
    lock_path = tmp_path / "tasks.workflows.lock"
    first = WorkflowService(h.coordinator, poll_seconds=0.02, lock=CoordinationLock(lock_path))
    first.start()
    wf = h.coordinator.create(plan_payload([task_spec("T1")]), requested_by="c").workflow
    h.coordinator.start(wf.id)
    second_coordinator = h.new_session()
    second = WorkflowService(second_coordinator, poll_seconds=0.02, lock=CoordinationLock(lock_path))
    second.start()
    try:
        assert first.coordinating and not second.coordinating
        record = h.workflows.get(wf.id)
        assert (record.status, record.job_id) == (WorkflowStatus.RUNNING, first_job)
        with pytest.raises(WorkflowError, match="outra sessao"):
            second.coordinator
        out = SimpleNamespace(system=[], warnings=[])
        handler = WorkflowCommandHandler(
            lambda: second.coordinator, show_system=out.system.append, show_warning=out.warnings.append
        )
        handler.handle("/workflow list")
        assert out.warnings and "outra sessao" in out.warnings[0]

        first.stop()  # o lock fica livre: a segunda sessao assume e recupera
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not second.coordinating:
            time.sleep(0.02)
        assert second.coordinating
        record = h.workflows.get(wf.id)
        assert (record.status, record.job_id) == (WorkflowStatus.PAUSED, h.job_id)
    finally:
        second.stop()
        first.stop()


def test_review_of_a_read_only_task_runs_read_only(tmp_path):
    from quimera.workflows.execution import WorkflowTaskHooks

    h = Harness(tmp_path, ["a1", "a2"])
    h.coordinator.start(
        h.coordinator.create(
            plan_payload([task_spec("R"), task_spec("W", task_type="code_edit")]), requested_by="c"
        ).workflow.id
    )
    hooks = WorkflowTaskHooks(h.workflows, h.tasks, tmp_path)
    flags = {}
    for task in h.tasks.list_tasks({"origin": "workflow"}):
        delegation = hooks.review_delegation(task, "a2", {"delegation_id": f"task-review-{task.id}"})
        flags[task.description] = delegation["read_only"]
    assert flags == {"Tarefa R": True, "Tarefa W": False}


def test_pool_hooks_use_the_configured_attempt_limit(tmp_path):
    """Revisao final: o runner/reviewer do pool aplica o mesmo limite do coordenador."""
    from quimera.tasks.repository import TaskRepository
    from quimera.workflows.models import WorkflowLimits

    def hooks_for(pool):
        pool._build_task_repository = lambda: TaskRepository(str(tmp_path / "tasks.db"))
        pool.get_workspace = lambda: None
        return pool._build_workflow_hooks()

    configured = TaskExecutorPool(get_workflow_limits=lambda: WorkflowLimits(max_task_attempts=5))
    assert hooks_for(configured).max_attempts == 5
    assert hooks_for(TaskExecutorPool()).max_attempts == WorkflowLimits().max_task_attempts


def _count_reconciles(h):
    reconciles = []
    original = h.coordinator.reconcile

    def counting(workflow_id):
        reconciles.append(workflow_id)
        return original(workflow_id)

    h.coordinator.reconcile = counting
    return reconciles


def test_monitor_does_not_spin_while_a_workflow_runs(tmp_path):
    """Um reconcile que termina não reacorda o monitor: sem evento novo, só o poll reavalia."""
    h = Harness(tmp_path, ["a1"])
    reconciles = _count_reconciles(h)
    service = WorkflowService(h.coordinator, poll_seconds=30)
    service.start()
    try:
        wf = h.coordinator.create(plan_payload([task_spec("T1")]), requested_by="c").workflow
        h.coordinator.start(wf.id)
        service.wake()
        time.sleep(0.5)
        # Antes eram centenas por segundo: o fim de cada reconcile disparava o próximo.
        assert reconciles == [wf.id]
    finally:
        service.stop()


def test_wake_during_a_reconcile_runs_exactly_one_more(tmp_path):
    """Pedido chegado com o workflow ocupado não se perde: uma reavaliação ao terminar, e só uma."""
    h = Harness(tmp_path, ["a1"])
    reconciles = _count_reconciles(h)
    entered, release = threading.Event(), threading.Event()
    counting = h.coordinator.reconcile

    def blocking(workflow_id):
        if not entered.is_set():
            entered.set()
            release.wait(5)
        return counting(workflow_id)

    h.coordinator.reconcile = blocking
    service = WorkflowService(h.coordinator, poll_seconds=30)
    service.start()
    try:
        wf = h.coordinator.create(plan_payload([task_spec("T1")]), requested_by="c").workflow
        h.coordinator.start(wf.id)
        service.wake()
        assert entered.wait(5)
        service.wake()  # chega enquanto o primeiro reconcile ainda roda
        time.sleep(0.2)
        assert reconciles == []  # ocupado: nada roda em paralelo para o mesmo workflow
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(reconciles) < 2:
            time.sleep(0.02)
        assert reconciles == [wf.id, wf.id]
        time.sleep(0.3)
        assert len(reconciles) == 2  # e nenhum ciclo extra por conta própria
    finally:
        service.stop()


def test_reconcile_publishes_the_overview_without_a_new_cycle(tmp_path):
    """A conclusão decidida no reconcile chega ao shared_state sem reacordar o monitor."""
    h = Harness(tmp_path, ["a1"])
    h.consult_answer = "tudo conferido"
    reconciles = _count_reconciles(h)
    refreshed = []
    service = WorkflowService(
        h.coordinator, refresh_state=lambda: refreshed.append(1), poll_seconds=30
    )
    service.start()
    try:
        wf = h.coordinator.create(plan_payload([task_spec("T1")]), requested_by="claude").workflow
        h.coordinator.start(wf.id)
        task_id = h.workflows.snapshot(wf.id).tasks[0].link.task_id
        assert h.tasks.claim_task("a1") == task_id
        assert h.tasks.complete_task(task_id, result="ok")
        service.wake()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(refreshed) < 2:
            time.sleep(0.02)
        record = h.workflows.get(wf.id)
        assert record.status == WorkflowStatus.COMPLETED and record.result == "tudo conferido"
        # Um refresh ao despachar (workflow em andamento) e outro do próprio reconcile (concluído).
        assert len(refreshed) == 2 and service.open_overview() == []
        assert reconciles == [wf.id]
    finally:
        service.stop()

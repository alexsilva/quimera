"""Testes para TaskReviewer — review isolado de tasks sem subir o app."""

from quimera.constants import TaskStatus
from quimera.prompt_kinds import PromptKind
from quimera.tasks.reviewer import TaskReviewer
from quimera.runtime.models import TaskRecord


class DispatchStub:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def delegate(self, agent_name, **kwargs):
        self.calls.append((agent_name, kwargs))
        if self.error:
            raise self.error
        return self.response


class SystemLayerSpy:
    def __init__(self):
        self.messages = []

    def show_system_message(self, message):
        self.messages.append(message)

    def show_muted_message(self, message):
        self.messages.append(message)


class RepositorySpy:
    def __init__(self):
        self.transition_calls = []
        self.requeue_calls = []
        self.complete_calls = []
        self.fail_calls = []
        self.transition_result = True
        self.requeue_result = True
        self.complete_result = True

    def transition_task(self, task_id, to_status, *, result=None, notes=None, approved_by=None):
        self.transition_calls.append((task_id, to_status, result, notes, approved_by))
        return self.transition_result

    def requeue_task_after_review(self, task_id, failed_agent, result=None, notes=None):
        self.requeue_calls.append((task_id, failed_agent, result, notes))
        return self.requeue_result

    def complete_task(self, task_id, result=None, reviewed_by=None):
        self.complete_calls.append((task_id, result, reviewed_by))
        return self.complete_result

    def fail_task(self, task_id, reason=None):
        self.fail_calls.append((task_id, reason))
        return True


class FailoverPolicyStub:
    def __init__(self, has_review_failover=False):
        self.has_review_failover_value = has_review_failover

    def has_review_failover(self, executor_agent, failed_reviewer):
        return self.has_review_failover_value


def test_reviewer_rejects_self_review():
    """Verifica que o revisor rejeita revisão do mesmo agente que executou a tarefa."""
    dispatch = DispatchStub(response=None)
    system = SystemLayerSpy()
    repo = RepositorySpy()
    reviewer = TaskReviewer(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = reviewer.review(
        TaskRecord(id=7, job_id=0, description="", status="reviewing", assigned_to="codex", result="ok", notes=None),
        agent_name="codex",
    )

    assert ok is False
    assert repo.transition_calls == [(7, TaskStatus.PENDING_REVIEW, "ok", None, None)]
    assert repo.complete_calls == []
    assert system.messages[-1] == "[task 7] codex: review rejeitado, aguardando outro agente"


def test_reviewer_completes_task_when_verdict_is_aceite():
    """Verifica que o revisor conclui a tarefa quando o veredito é ACEITE."""

    dispatch = DispatchStub(response="ACEITE\nEvidência ok")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    reviewer = TaskReviewer(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = reviewer.review(
        TaskRecord(id=8, job_id=0, description="ajuste", body="escopo", status="reviewing", assigned_to="codex", result="ok"),
        agent_name="pickle",
    )

    assert ok is True
    assert repo.complete_calls == [(8, "ok", "pickle")]
    assert repo.requeue_calls == []
    delegation = dispatch.calls[0][1]["delegation"]
    assert dispatch.calls[0][1]["prompt_kind"] is PromptKind.TASK_REVIEWER
    assert delegation["delegation_id"] == "task-review-8"
    assert "Task original:\najuste" in delegation["context"]
    assert "Resultado do executor:\nok" in delegation["context"]
    assert "ACEITE, RETENTATIVA, REPLANEJAR ou REJEITAR" in delegation["expected"]
    assert "review concluído" in system.messages[-1]


def test_reviewer_requeues_when_verdict_not_accepted():
    """Verifica que o revisor recoloca a tarefa em fila quando o veredito não é ACEITE."""
    dispatch = DispatchStub(response="RETENTATIVA\nFaltou teste")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    reviewer = TaskReviewer(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(),
        classify_task_review_result=lambda response: (False, "RETENTATIVA", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = reviewer.review(
        TaskRecord(id=9, job_id=0, description="ajuste", body="escopo", status="reviewing", assigned_to="codex", result="ok"),
        agent_name="pickle",
    )

    assert ok is False
    assert repo.requeue_calls == [(9, "codex", "ok", "RETENTATIVA\nFaltou teste")]
    assert repo.complete_calls == []
    assert system.messages[-1] == "[task 9] pickle: review pediu retentativa, task voltou para pending"


def test_reviewer_returns_to_pending_review_when_exception_has_fallback():
    """Verifica que o revisor retorna a tarefa para pending_review quando há fallback."""

    dispatch = DispatchStub(error=RuntimeError("timeout"))
    system = SystemLayerSpy()
    repo = RepositorySpy()
    reviewer = TaskReviewer(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(has_review_failover=True),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = reviewer.review(
        TaskRecord(id=10, job_id=0, description="", status="reviewing", assigned_to="codex", result="ok"),
        agent_name="pickle",
    )

    assert ok is False
    assert repo.transition_calls == [(10, TaskStatus.PENDING_REVIEW, "ok", "timeout", None)]
    assert repo.fail_calls == []
    assert system.messages[-1] == "[task 10] pickle: review falhou: timeout"


def test_reviewer_fails_when_no_operational_fallback():
    """Verifica que o revisor falha a tarefa quando não há fallback operacional."""
    dispatch = DispatchStub(error=RuntimeError("timeout"))
    system = SystemLayerSpy()
    repo = RepositorySpy()
    reviewer = TaskReviewer(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(has_review_failover=False),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = reviewer.review(
        TaskRecord(id=11, job_id=0, description="", status="reviewing", assigned_to="codex", result="ok"),
        agent_name="pickle",
    )

    assert ok is False
    assert repo.transition_calls == []
    assert repo.fail_calls == [(11, "review failed without operational fallback: timeout")]
    assert system.messages[-1] == "[task 11] pickle: review falhou: timeout"


def test_reviewer_cancelled_by_user():
    """Verifica que o revisor trata cancelamento pelo usuário."""

    dispatch = DispatchStub(response="ACEITE\nok")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    reviewer = TaskReviewer(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: True,
    )

    ok = reviewer.review(
        TaskRecord(id=12, job_id=0, description="test", status="reviewing", assigned_to="codex", result="ok"),
        agent_name="pickle",
    )

    assert ok is False
    assert repo.fail_calls == [(12, "cancelled by user")]
    assert system.messages[-1] == "[task 12] pickle: cancelado pelo usuário"


def test_reviewer_self_review_transition_fails():
    """Verifica que o revisor lida com falha na transição de auto-revisão."""

    dispatch = DispatchStub(response=None)
    system = SystemLayerSpy()
    repo = RepositorySpy()
    repo.transition_result = False
    reviewer = TaskReviewer(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = reviewer.review(
        TaskRecord(id=13, job_id=0, description="", status="reviewing", assigned_to="codex", result="ok", notes=None),
        agent_name="codex",
    )

    assert ok is False
    assert repo.transition_calls == [(13, TaskStatus.PENDING_REVIEW, "ok", None, None)]
    assert system.messages[-1] == "[task 13] codex: erro ao rejeitar review — transição inválida"


def test_reviewer_requeue_after_review_fails():
    """Verifica que o revisor lida com falha ao recolocar tarefa em fila após revisão."""
    dispatch = DispatchStub(response="RETENTATIVA\nFaltou teste")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    repo.requeue_result = False
    reviewer = TaskReviewer(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(),
        classify_task_review_result=lambda response: (False, "RETENTATIVA", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = reviewer.review(
        TaskRecord(id=14, job_id=0, description="ajuste", body="escopo", status="reviewing", assigned_to="codex", result="ok"),
        agent_name="pickle",
    )

    assert ok is False
    assert repo.requeue_calls == [(14, "codex", "ok", "RETENTATIVA\nFaltou teste")]
    assert system.messages[-1] == "[task 14] pickle: erro ao recolocar task em fila"


def test_reviewer_complete_task_fails():
    """Verifica que o revisor lida com falha ao concluir tarefa após revisão."""

    dispatch = DispatchStub(response="ACEITE\nEvidência ok")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    repo.complete_result = False
    reviewer = TaskReviewer(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = reviewer.review(
        TaskRecord(id=15, job_id=0, description="ajuste", body="escopo", status="reviewing", assigned_to="codex", result="ok"),
        agent_name="pickle",
    )

    assert ok is False
    assert repo.complete_calls == [(15, "ok", "pickle")]
    assert system.messages[-1] == "[task 15] pickle: erro ao concluir task após review"


def test_reviewer_exception_fallback_transition_fails():
    """Verifica que o revisor lida com falha na transição de fallback após exceção."""

    dispatch = DispatchStub(error=RuntimeError("timeout"))
    system = SystemLayerSpy()
    repo = RepositorySpy()
    repo.transition_result = False
    reviewer = TaskReviewer(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(has_review_failover=True),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = reviewer.review(
        TaskRecord(id=16, job_id=0, description="", status="reviewing", assigned_to="codex", result="ok"),
        agent_name="pickle",
    )

    assert ok is False
    assert repo.transition_calls == [(16, TaskStatus.PENDING_REVIEW, "ok", "timeout", None)]
    assert repo.fail_calls == [(16, "review failed and fallback transition failed: timeout")]
    assert system.messages[-1] == "[task 16] pickle: review falhou: timeout"

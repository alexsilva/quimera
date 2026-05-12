from quimera.constants import TaskStatus

from quimera.app.task_review_service import TaskReviewService


class DispatchStub:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def call_agent(self, agent_name, **kwargs):
        self.calls.append((agent_name, kwargs))
        if self.error:
            raise self.error
        return self.response


class SystemLayerSpy:
    def __init__(self):
        self.messages = []

    def show_system_message(self, message):
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
        _ = (executor_agent, failed_reviewer)
        return self.has_review_failover_value


def test_review_handler_rejects_self_review_and_returns_to_pending_review():
    dispatch = DispatchStub(response=None)
    system = SystemLayerSpy()
    repo = RepositorySpy()
    service = TaskReviewService(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = service.handler_for("codex")(
        {"id": 7, "assigned_to": "codex", "result": "ok", "notes": None}
    )

    assert ok is False
    assert repo.transition_calls == [(7, TaskStatus.PENDING_REVIEW, "ok", None, None)]
    assert repo.complete_calls == []
    assert system.messages[-1] == "[task 7] codex: review rejeitado, aguardando outro agente"


def test_review_handler_completes_task_when_verdict_is_aceite():
    dispatch = DispatchStub(response="ACEITE\nEvidência ok")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    service = TaskReviewService(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = service.handler_for("pickle")(
        {"id": 8, "assigned_to": "codex", "description": "ajuste", "body": "escopo", "result": "ok"}
    )

    assert ok is True
    assert repo.complete_calls == [(8, "ok", "pickle")]
    assert repo.requeue_calls == []
    assert "review concluído" in system.messages[-1]


def test_review_handler_requeues_task_when_verdict_is_not_accepted():
    dispatch = DispatchStub(response="RETENTATIVA\nFaltou teste")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    service = TaskReviewService(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(),
        classify_task_review_result=lambda response: (False, "RETENTATIVA", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = service.handler_for("pickle")(
        {"id": 9, "assigned_to": "codex", "description": "ajuste", "body": "escopo", "result": "ok"}
    )

    assert ok is False
    assert repo.requeue_calls == [(9, "codex", "ok", "RETENTATIVA\nFaltou teste")]
    assert repo.complete_calls == []
    assert system.messages[-1] == "[task 9] pickle: review pediu retentativa, task voltou para pending"


def test_review_handler_returns_to_pending_review_when_exception_has_fallback():
    dispatch = DispatchStub(error=RuntimeError("timeout"))
    system = SystemLayerSpy()
    repo = RepositorySpy()
    service = TaskReviewService(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(has_review_failover=True),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = service.handler_for("pickle")({"id": 10, "assigned_to": "codex", "result": "ok"})

    assert ok is False
    assert repo.transition_calls == [(10, TaskStatus.PENDING_REVIEW, "ok", "timeout", None)]
    assert repo.fail_calls == []
    assert system.messages[-1] == "[task 10] pickle: review falhou: timeout"


def test_review_handler_fails_when_exception_has_no_operational_fallback():
    dispatch = DispatchStub(error=RuntimeError("timeout"))
    system = SystemLayerSpy()
    repo = RepositorySpy()
    service = TaskReviewService(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(has_review_failover=False),
        classify_task_review_result=lambda response: (True, "ACEITE", response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = service.handler_for("pickle")({"id": 11, "assigned_to": "codex", "result": "ok"})

    assert ok is False
    assert repo.transition_calls == []
    assert repo.fail_calls == [(11, "review failed without operational fallback: timeout")]
    assert system.messages[-1] == "[task 11] pickle: review falhou: timeout"

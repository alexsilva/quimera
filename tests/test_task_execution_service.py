from quimera.app.task_execution_service import TaskExecutionService
from quimera.runtime.models import TaskRecord


class DispatchStub:
    def __init__(self, response=None):
        self.response = response
        self.calls = []

    def call_agent(self, agent_name, **kwargs):
        self.calls.append((agent_name, kwargs))
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
        self.fail_calls = []
        self.requeue_calls = []
        self.submit_calls = []
        self.complete_calls = []
        self.submit_result = True
        self.complete_result = True

    def fail_task(self, task_id, reason=None):
        self.fail_calls.append((task_id, reason))
        return True

    def requeue_task(self, task_id, failed_agent, reason=None):
        self.requeue_calls.append((task_id, failed_agent, reason))
        return True

    def submit_for_review(self, task_id, result=None):
        self.submit_calls.append((task_id, result))
        return self.submit_result

    def complete_task(self, task_id, result=None, reviewed_by=None):
        self.complete_calls.append((task_id, result, reviewed_by))
        return self.complete_result


class FailoverPolicyStub:
    def __init__(self, review_agents=None, can_failover=True):
        self.review_agents = list(review_agents or [])
        self.can_failover_value = can_failover

    def review_agents_for(self, executor_agent=None, exclude_agents=None):
        _ = (executor_agent, exclude_agents)
        return list(self.review_agents)

    def can_failover(self, task_id, failed_agent):
        _ = (task_id, failed_agent)
        return self.can_failover_value


def test_handler_completes_task_when_no_review_agent_available():
    dispatch = DispatchStub(response="resultado final")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[])
    service = TaskExecutionService(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response),
        was_user_cancelled=lambda: False,
    )

    ok = service.handler_for("codex")(TaskRecord(id=1, job_id=0, description="corrigir bug", status="in_progress"))

    assert ok is True
    assert repo.complete_calls == [(1, "resultado final", None)]
    assert repo.submit_calls == []
    assert system.messages == [
        "[task 1] codex: iniciando — corrigir bug",
        "[task 1] codex:\nresultado final",
    ]


def test_handler_submits_for_review_when_other_reviewer_exists():
    dispatch = DispatchStub(response="resultado final")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=["pickle"])
    service = TaskExecutionService(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response),
        was_user_cancelled=lambda: False,
    )

    ok = service.handler_for("codex")(TaskRecord(id=2, job_id=0, description="ajustar rota", status="in_progress"))

    assert ok is True
    assert repo.submit_calls == [(2, "resultado final")]
    assert repo.complete_calls == []
    assert system.messages[-1] == "[task 2] codex: aguardando review de outro agente"


def test_handler_requeues_when_agent_returns_no_response_and_failover_is_possible():
    dispatch = DispatchStub(response=None)
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[], can_failover=True)
    failures = []
    service = TaskExecutionService(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response or ""),
        was_user_cancelled=lambda: False,
        record_failure=lambda agent_name: failures.append(agent_name),
    )

    ok = service.handler_for("codex")(TaskRecord(id=3, job_id=0, description="rodar validação", status="in_progress"))

    assert ok is False
    assert failures == ["codex"]
    assert repo.requeue_calls == [(3, "codex", "communication failed")]
    assert repo.fail_calls == []


def test_handler_fails_when_execution_is_blocked_and_no_failover_exists():
    dispatch = DispatchStub(response="não consigo executar")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[], can_failover=False)
    service = TaskExecutionService(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (False, response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = service.handler_for("codex")(TaskRecord(id=4, job_id=0, description="aplicar patch", status="in_progress"))

    assert ok is False
    assert repo.fail_calls == [(4, "não consigo executar")]
    assert repo.requeue_calls == []
    assert system.messages[-1] == "[task 4] codex: bloqueada"


def test_handler_fails_when_user_cancels_execution():
    dispatch = DispatchStub(response="resultado parcial")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[])
    service = TaskExecutionService(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response or ""),
        was_user_cancelled=lambda: True,
    )

    ok = service.handler_for("codex")(TaskRecord(id=5, job_id=0, description="executar tarefa", status="in_progress"))

    assert ok is False
    assert repo.fail_calls == [(5, "cancelled by user")]
    assert repo.complete_calls == []
    assert system.messages[-1] == "[task 5] codex: cancelado pelo usuário"

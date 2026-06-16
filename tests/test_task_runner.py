"""Testes para TaskRunner — execução isolada de tasks sem subir o app."""

from quimera.prompt_kinds import PromptKind
from quimera.runtime.task_runner import TaskRunner
from quimera.runtime.models import TaskRecord
from quimera.app.task_utils import summarize_task_feedback


class DispatchStub:
    def __init__(self, response=None):
        self.response = response
        self.calls = []

    def delegate(self, agent_name, **kwargs):
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
        return list(self.review_agents)

    def can_failover(self, task_id, failed_agent):
        return self.can_failover_value


def test_runner_completes_task_when_no_review_agent():
    """Verifica que o runner conclui a tarefa quando não há agente de revisão."""
    dispatch = DispatchStub(response="resultado final")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[])
    runner = TaskRunner(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response),
        was_user_cancelled=lambda: False,
    )

    ok = runner.run(TaskRecord(id=1, job_id=0, description="corrigir bug", status="in_progress"), agent_name="codex")

    assert ok is True
    assert repo.complete_calls == [(1, "resultado final", None)]
    assert repo.submit_calls == []
    delegation = dispatch.calls[0][1]["delegation"]
    assert dispatch.calls[0][1]["prompt_kind"] is PromptKind.TASK_EXECUTOR
    assert delegation["delegation_id"] == "task-1"
    assert delegation["task"] == "corrigir bug"
    assert delegation["context"] == "corrigir bug"
    assert system.messages == [
        "[task 1] codex: iniciando — corrigir bug",
        "[task 1] codex:\nresultado final",
        "[task 1] codex: concluída",
    ]


def test_runner_submits_for_review_when_reviewer_exists():
    """Verifica que o runner submete para revisão quando há revisor disponível."""
    dispatch = DispatchStub(response="resultado final")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=["pickle"])
    runner = TaskRunner(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response),
        was_user_cancelled=lambda: False,
    )

    ok = runner.run(TaskRecord(id=2, job_id=0, description="ajustar rota", status="in_progress"), agent_name="codex")

    assert ok is True
    assert repo.submit_calls == [(2, "resultado final")]
    assert repo.complete_calls == []
    assert system.messages[-1] == "[task 2] codex: aguardando review de outro agente"


def test_runner_requeues_when_agent_returns_no_response_and_failover_possible():
    """Verifica que o runner recoloca em fila quando o agente não responde e há failover."""
    dispatch = DispatchStub(response=None)
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[], can_failover=True)
    failures = []
    runner = TaskRunner(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response or ""),
        was_user_cancelled=lambda: False,
        record_failure=lambda agent_name: failures.append(agent_name),
    )

    ok = runner.run(TaskRecord(id=3, job_id=0, description="rodar validação", status="in_progress"), agent_name="codex")

    assert ok is False
    assert failures == ["codex"]
    assert repo.requeue_calls == [(3, "codex", "communication failed")]
    assert repo.fail_calls == []


def test_runner_fails_when_execution_blocked_and_no_failover():
    """Verifica que o runner falha a tarefa quando a execução está bloqueada sem failover."""
    dispatch = DispatchStub(response="não consigo executar")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[], can_failover=False)
    runner = TaskRunner(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (False, response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = runner.run(TaskRecord(id=4, job_id=0, description="aplicar patch", status="in_progress"), agent_name="codex")

    assert ok is False
    assert repo.fail_calls == [(4, "não consigo executar")]
    assert repo.requeue_calls == []
    assert system.messages[-1] == "[task 4] codex: bloqueada"


def test_runner_fails_when_user_cancels_execution():
    """Verifica que o runner falha a tarefa quando o usuário cancela a execução."""
    dispatch = DispatchStub(response="resultado parcial")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[])
    runner = TaskRunner(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response or ""),
        was_user_cancelled=lambda: True,
    )

    ok = runner.run(TaskRecord(id=5, job_id=0, description="executar tarefa", status="in_progress"), agent_name="codex")

    assert ok is False
    assert repo.fail_calls == [(5, "cancelled by user")]
    assert repo.complete_calls == []
    assert system.messages[-1] == "[task 5] codex: cancelado pelo usuário"


def test_runner_fails_on_empty_body():
    """Verifica que o runner falha a tarefa quando o body está vazio."""
    system = SystemLayerSpy()
    repo = RepositorySpy()
    runner = TaskRunner(
        dispatch_services=DispatchStub(response="ok"),
        system_layer=system,
        repository=repo,
        failover_policy=FailoverPolicyStub(),
        classify_task_execution_result=lambda response: (True, response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = runner.run(TaskRecord(id=6, job_id=0, description="", status="in_progress"), agent_name="codex")

    assert ok is False
    assert repo.fail_calls == [(6, "empty body")]


def test_runner_requeues_on_exception_when_failover_possible():
    """Verifica que o runner recoloca em fila quando há exceção e failover disponível."""
    class FailingDispatch:
        def delegate(self, agent_name, **kwargs):
            raise RuntimeError("api timeout")

    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[], can_failover=True)
    runner = TaskRunner(
        dispatch_services=FailingDispatch(),
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = runner.run(TaskRecord(id=7, job_id=0, description="test", body="body", status="in_progress"), agent_name="codex")

    assert ok is False
    assert repo.requeue_calls == [(7, "codex", "api timeout")]
    assert repo.fail_calls == []
    assert system.messages[-1] == "[task 7] codex: erro: api timeout"


def test_runner_wraps_agent_call_with_hooks():
    """Verifica que o runner executa hooks before/after na chamada do agente."""

    dispatch = DispatchStub(response="resultado final")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[])
    events = []
    runner = TaskRunner(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response),
        was_user_cancelled=lambda: False,
        before_agent_call=lambda agent_name: events.append(("before", agent_name)),
        after_agent_call=lambda agent_name: events.append(("after", agent_name)),
    )

    ok = runner.run(TaskRecord(id=8, job_id=0, description="corrigir bug", status="in_progress"), agent_name="chatgpt-api")

    assert ok is True
    assert events == [("before", "chatgpt-api"), ("after", "chatgpt-api")]


def test_runner_uses_structured_delegation_with_real_task_id():
    """Verifica que o runner usa delegation estruturado com o ID real da tarefa."""

    dispatch = DispatchStub(response="resultado final")
    runner = TaskRunner(
        dispatch_services=dispatch,
        system_layer=SystemLayerSpy(),
        repository=RepositorySpy(),
        failover_policy=FailoverPolicyStub(review_agents=[]),
        classify_task_execution_result=lambda response: (True, response),
        was_user_cancelled=lambda: False,
    )

    ok = runner.run(
        TaskRecord(id=123, job_id=0, description="validar regressão", body="body da task", status="in_progress"),
        agent_name="codex",
    )

    assert ok is True
    delegation = dispatch.calls[0][1]["delegation"]
    assert delegation["delegation_id"] == "task-123"
    assert delegation["task"] == "validar regressão"
    assert delegation["context"] == "body da task"
    assert dispatch.calls[0][1]["prompt_kind"] is PromptKind.TASK_EXECUTOR


def test_runner_calls_after_hook_even_when_dispatch_raises():
    """Verifica que o hook after é chamado mesmo quando o dispatch lança exceção."""
    class FailingDispatch:
        def delegate(self, agent_name, **kwargs):
            raise RuntimeError("api timeout")

    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[], can_failover=False)
    events = []
    runner = TaskRunner(
        dispatch_services=FailingDispatch(),
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response or ""),
        was_user_cancelled=lambda: False,
        before_agent_call=lambda agent_name: events.append(("before", agent_name)),
        after_agent_call=lambda agent_name: events.append(("after", agent_name)),
    )

    ok = runner.run(TaskRecord(id=9, job_id=0, description="test", body="body", status="in_progress"), agent_name="chatgpt-api")

    assert ok is False
    assert events == [("before", "chatgpt-api"), ("after", "chatgpt-api")]


def test_runner_fails_on_exception_when_no_failover():
    """Verifica que o runner falha a tarefa quando há exceção e não há failover."""
    class FailingDispatch:
        def delegate(self, agent_name, **kwargs):
            raise RuntimeError("fatal error")

    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[], can_failover=False)
    runner = TaskRunner(
        dispatch_services=FailingDispatch(),
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = runner.run(TaskRecord(id=8, job_id=0, description="test", body="body", status="in_progress"), agent_name="codex")

    assert ok is False
    assert repo.requeue_calls == []
    assert repo.fail_calls == [(8, "fatal error")]


def test_runner_submit_for_review_fails():
    """Verifica que o runner lida com falha ao submeter para revisão."""

    dispatch = DispatchStub(response="resultado")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    repo.submit_result = False
    policy = FailoverPolicyStub(review_agents=["pickle"])
    runner = TaskRunner(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = runner.run(TaskRecord(id=9, job_id=0, description="submeter", status="in_progress"), agent_name="codex")

    assert ok is False
    assert repo.submit_calls == [(9, "resultado")]
    assert repo.complete_calls == []
    assert system.messages[-1] == "[task 9] codex: erro ao submeter para review"


def test_runner_complete_task_fails():
    """Verifica que o runner lida com falha ao concluir a tarefa."""

    dispatch = DispatchStub(response="finalizado")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    repo.complete_result = False
    policy = FailoverPolicyStub(review_agents=[])
    runner = TaskRunner(
        dispatch_services=dispatch,
        system_layer=system,
        repository=repo,
        failover_policy=policy,
        classify_task_execution_result=lambda response: (True, response or ""),
        was_user_cancelled=lambda: False,
    )

    ok = runner.run(TaskRecord(id=10, job_id=0, description="concluir", status="in_progress"), agent_name="codex")

    assert ok is False
    assert repo.complete_calls == [(10, "finalizado", None)]
    assert system.messages[-1] == "[task 10] codex: erro ao concluir task"


def test_summarize_task_feedback_truncates_lines():
    """Verifica que summarize_task_feedback trunca linhas que excedem o limite."""

    result = "ACEITE\nImplementação concluída com evidência concreta e detalhes extras para exceder o limite configurado."

    summary = summarize_task_feedback(result, max_chars=50, max_lines=6)

    assert "\n" in summary
    assert summary.endswith("…")
    assert len(summary) <= 50

def test_summarize_task_feedback_single_line_without_newlines():
    """Verifica que summarize_task_feedback mantém linha única sem newlines."""

    result = "Implementação concluída com sucesso."

    summary = summarize_task_feedback(result, max_chars=100)

    assert "\n" not in summary
    assert not summary.endswith("…")

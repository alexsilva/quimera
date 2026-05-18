from quimera.app.task_execution_service import TaskExecutionService
from quimera.app.task import AppTaskServices, _BACKGROUND_AGENT_TIMEOUT_SECONDS
from quimera.app.task_classifiers import classify_task_execution_result, classify_task_review_result
from quimera.runtime.models import TaskRecord


def build_task_services(app):
    if not hasattr(app, "task_executor_factory"):
        app.task_executor_factory = lambda *args, **kwargs: __import__(
            "quimera.app.core", fromlist=["create_executor"]
        ).create_executor(*args, **kwargs)
    if not hasattr(app, "current_job_id"):
        app.current_job_id = None
    if not hasattr(app, "agent_pool"):
        app.agent_pool = type("AgentPoolStub", (), {"agents": []})()
    if not hasattr(app, "task_executors"):
        app.task_executors = []
    if not hasattr(app, "renderer"):
        app.renderer = None
    if not hasattr(app, "input_services"):
        app.input_services = None
    if not hasattr(app, "input_gate"):
        app.input_gate = None
    if not hasattr(app, "tasks_db_path"):
        app.tasks_db_path = None
    if not hasattr(app, "event_sink"):
        app.event_sink = None
    if not hasattr(app, "agent_client"):
        app.agent_client = None
    if not hasattr(app, "workspace"):
        app.workspace = None
    if not hasattr(app, "dispatch_services"):
        app.dispatch_services = None
    if not hasattr(app, "tool_executor"):
        app.tool_executor = None
    if not hasattr(app, "auto_approve_mutations"):
        app.auto_approve_mutations = False
    if not hasattr(app, "_approval_handler"):
        app._approval_handler = None
    if not hasattr(app, "get_agent_plugin"):
        app.get_agent_plugin = lambda _agent_name: None
    if not hasattr(app, "get_available_plugins"):
        app.get_available_plugins = lambda: []
    if not hasattr(app, "session_state"):
        app.session_state = None
    if not hasattr(app, "history"):
        app.history = None
    if not hasattr(app, "shared_state"):
        app.shared_state = None
    if not hasattr(app, "system_layer"):
        app.system_layer = None
    if not hasattr(app, "task_classifier"):
        app.task_classifier = None
    if not hasattr(app, "user_name"):
        app.user_name = ""
    if not hasattr(app, "prompt_builder"):
        app.prompt_builder = None
    if not hasattr(app, "visibility"):
        app.visibility = None
    if not hasattr(app, "show_error_message"):
        app.show_error_message = None
    if not hasattr(app, "show_muted_message"):
        app.show_muted_message = None
    if not hasattr(app, "execution_mode"):
        app.execution_mode = None
    if not hasattr(app, "_record_tool_event"):
        app._record_tool_event = None
    if not hasattr(app, "record_failure"):
        app.record_failure = None
    if not hasattr(app, "session_metrics"):
        app.session_metrics = None
    if not hasattr(app, "round_index"):
        app.round_index = 0
    if not hasattr(app, "debug_prompt_metrics"):
        app.debug_prompt_metrics = False
    if not hasattr(app, "_clear_user_prompt_line_if_needed"):
        app._clear_user_prompt_line_if_needed = None
    if not hasattr(app, "_redisplay_user_prompt_if_needed"):
        app._redisplay_user_prompt_if_needed = None
    if not hasattr(app, "_output_lock"):
        app._output_lock = None
    if not hasattr(app, "_counter_lock"):
        app._counter_lock = None
    if not hasattr(app, "_shared_state_lock"):
        app._shared_state_lock = None
    if not hasattr(app, "session_services"):
        app.session_services = None
    if not hasattr(app, "MAX_RETRIES"):
        app.MAX_RETRIES = 2
    if not hasattr(app, "RETRY_BACKOFF_SECONDS"):
        app.RETRY_BACKOFF_SECONDS = 1
    if not hasattr(app, "RATE_LIMIT_BACKOFF_SECONDS"):
        app.RATE_LIMIT_BACKOFF_SECONDS = 30
    if not hasattr(app, "call_agent"):
        app.call_agent = lambda *args, **kwargs: None
    if not hasattr(app, "parse_response"):
        app.parse_response = lambda raw: (raw, None, None, False, False, None)

    return AppTaskServices(
        task_executor_factory=app.task_executor_factory,
        get_current_job_id=lambda: app.current_job_id,
        get_agent_pool_agents=lambda: list(app.agent_pool.agents),
        get_task_executors=lambda: list(app.task_executors),
        set_task_executors=lambda executors: setattr(app, "task_executors", list(executors)),
        get_renderer=lambda: app.renderer,
        get_input_services=lambda: app.input_services,
        get_input_gate=lambda: app.input_gate,
        get_tasks_db_path=lambda: app.tasks_db_path,
        get_event_sink=lambda: app.event_sink,
        get_agent_client=lambda: app.agent_client,
        get_workspace=lambda: app.workspace,
        get_dispatch_tool_executor=lambda: app.tool_executor,
        get_dispatch_services=lambda: app.dispatch_services,
        get_auto_approve_mutations=lambda: app.auto_approve_mutations,
        get_approval_handler=lambda: app._approval_handler,
        set_approval_handler=lambda handler: setattr(app, "_approval_handler", handler),
        get_agent_plugin=app.get_agent_plugin,
        get_available_plugins=app.get_available_plugins,
        get_session_state=lambda: app.session_state,
        get_history=lambda: app.history,
        get_shared_state=lambda: app.shared_state,
        get_system_layer=lambda: app.system_layer,
        get_task_classifier=lambda: app.task_classifier,
        get_user_name=lambda: app.user_name,
        get_prompt_builder=lambda: app.prompt_builder,
        get_visibility=lambda: app.visibility,
        get_show_error_message=lambda: app.show_error_message,
        get_show_muted_message=lambda: app.show_muted_message,
        get_execution_mode=lambda: app.execution_mode,
        get_record_tool_event=lambda: app._record_tool_event,
        get_record_failure=lambda: app.record_failure,
        get_session_metrics=lambda: app.session_metrics,
        get_round_index=lambda: app.round_index,
        get_debug_prompt_metrics=lambda: app.debug_prompt_metrics,
        get_clear_prompt_line=lambda: app._clear_user_prompt_line_if_needed,
        get_redisplay_prompt=lambda: app._redisplay_user_prompt_if_needed,
        get_output_lock=lambda: app._output_lock,
        get_counter_lock=lambda: app._counter_lock,
        get_shared_state_lock=lambda: app._shared_state_lock,
        get_session_services=lambda: app.session_services,
        get_max_retries=lambda: app.MAX_RETRIES,
        get_retry_backoff_seconds=lambda: app.RETRY_BACKOFF_SECONDS,
        get_rate_limit_backoff_seconds=lambda: app.RATE_LIMIT_BACKOFF_SECONDS,
        call_agent=app.call_agent,
        parse_response=app.parse_response,
        classify_task_execution_result=getattr(app, "classify_task_execution_result", classify_task_execution_result),
        classify_task_review_result=getattr(app, "classify_task_review_result", classify_task_review_result),
    )


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
    assert system.messages[:2] == [
        "[task 1] codex: iniciando — corrigir bug",
        "[task 1] codex:\nresultado final",
    ]
    assert system.messages[-1] == "[task 1] codex: concluída"


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


def test_app_task_services_execution_isolated_from_chat_cancel_state(monkeypatch, tmp_path):
    """Tasks em background não devem herdar o cancelamento do chat interativo."""
    dispatch = DispatchStub(response="resultado final")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[])

    class AppStub:
        pass

    app = AppStub()
    app.agent_client = type("ChatClient", (), {"_user_cancelled": True})()
    app.system_layer = system
    app.record_failure = lambda _agent_name: None
    app.workspace = type("WorkspaceStub", (), {"cwd": tmp_path})()
    app.tasks_db_path = str(tmp_path / "tasks.db")
    app.auto_approve_mutations = False
    app.get_agent_plugin = lambda _agent_name: None

    services = build_task_services(app)
    monkeypatch.setattr(services, "_build_task_repository", lambda: repo)
    monkeypatch.setattr(services, "_get_background_dispatch_services", lambda: dispatch)

    ok = services._build_task_execution_service(policy).handler_for("codex")(
        TaskRecord(id=6, job_id=0, description="executar tarefa", status="in_progress")
    )

    assert ok is True
    assert repo.fail_calls == []
    assert repo.complete_calls == [(6, "resultado final", None)]


def test_background_dispatch_uses_chat_timeout_when_present(tmp_path):
    class AppStub:
        pass

    app = AppStub()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"timeout": 45})()
    app.workspace = type("WorkspaceStub", (), {"cwd": tmp_path})()
    app.visibility = "summary"
    app.tasks_db_path = None
    app.auto_approve_mutations = False

    services = build_task_services(app)

    dispatch = services._get_background_dispatch_services()

    assert dispatch._get_agent_client().timeout == 45


def test_background_dispatch_uses_fallback_timeout_when_chat_timeout_is_missing(tmp_path):
    class AppStub:
        pass

    app = AppStub()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"timeout": None})()
    app.workspace = type("WorkspaceStub", (), {"cwd": tmp_path})()
    app.visibility = "summary"
    app.tasks_db_path = None
    app.auto_approve_mutations = False

    services = build_task_services(app)

    dispatch = services._get_background_dispatch_services()

    assert dispatch._get_agent_client().timeout == _BACKGROUND_AGENT_TIMEOUT_SECONDS


def test_parallel_calls_use_background_dispatch_when_available(tmp_path, monkeypatch):
    class AppStub:
        pass

    app = AppStub()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"timeout": 45})()
    app.workspace = type("WorkspaceStub", (), {"cwd": tmp_path})()
    app.visibility = "summary"
    app.tasks_db_path = None
    app.auto_approve_mutations = False
    app.call_agent_calls = []

    def _chat_call_agent(*args, **kwargs):
        app.call_agent_calls.append((args, kwargs))
        return "chat-response"

    app.call_agent = _chat_call_agent
    app.parse_response = lambda raw: (raw, None, None, False, False, None)

    services = build_task_services(app)
    dispatch = DispatchStub(response="background-response")
    monkeypatch.setattr(services, "_get_background_dispatch_services", lambda: dispatch)

    result = services.call_agent_for_parallel(
        "codex",
        None,
        "standard",
        tmp_path / "staging",
        0,
    )

    assert result == ("codex", "background-response", None, None, False, False)
    assert dispatch.calls == [
        (
            "codex",
            {
                "handoff": None,
                "primary": False,
                "protocol_mode": "standard",
                "silent": True,
                "show_output": False,
            },
        )
    ]
    assert app.call_agent_calls == []

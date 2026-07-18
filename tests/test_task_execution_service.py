import threading

from quimera.app.dispatch import AppDispatchServices
from quimera.tasks.runner import TaskRunner
from quimera.tasks.services import (
    AppTaskServices,
    _BACKGROUND_AGENT_TIMEOUT_SECONDS,
)
from quimera.tasks.classifiers import classify_task_execution_result, classify_task_review_result
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
    if not hasattr(app, "event_sink"):
        app.event_sink = None
    if not hasattr(app, "agent_run_sink"):
        app.agent_run_sink = None
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
    if not hasattr(app, "get_agent_profile"):
        app.get_agent_profile = lambda _agent_name: None
    if not hasattr(app, "get_available_profiles"):
        app.get_available_profiles = lambda: []
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
    if not hasattr(app, "delegate"):
        app.delegate = lambda *args, **kwargs: None
    if not hasattr(app, "parse_response"):
        app.parse_response = lambda raw: (raw, None, None, False, None)

    return AppTaskServices(
        task_executor_factory=app.task_executor_factory,
        get_current_job_id=lambda: app.current_job_id,
        get_agent_pool_agents=lambda: list(app.agent_pool.agents),
        get_task_executors=lambda: list(app.task_executors),
        set_task_executors=lambda executors: setattr(app, "task_executors", list(executors)),
        get_renderer=lambda: app.renderer,
        get_input_services=lambda: app.input_services,
        get_input_gate=lambda: app.input_gate,
        get_event_sink=lambda: app.event_sink,
        get_agent_run_sink=lambda: app.agent_run_sink,
        get_agent_client=lambda: app.agent_client,
        get_workspace=lambda: app.workspace,
        get_dispatch_tool_executor=lambda: app.tool_executor,
        get_dispatch_services=lambda: app.dispatch_services,
        get_auto_approve_mutations=lambda: app.auto_approve_mutations,
        get_approval_handler=lambda: app._approval_handler,
        set_approval_handler=lambda handler: setattr(app, "_approval_handler", handler),
        get_agent_profile=app.get_agent_profile,
        get_available_profiles=app.get_available_profiles,
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
        get_redisplay_prompt=lambda: app._redisplay_user_prompt_if_needed,
        get_output_lock=lambda: app._output_lock,
        get_counter_lock=lambda: app._counter_lock,
        get_shared_state_lock=lambda: app._shared_state_lock,
        get_session_services=lambda: app.session_services,
        max_retries=app.MAX_RETRIES,
        retry_backoff_seconds=app.RETRY_BACKOFF_SECONDS,
        get_rate_limit_backoff_seconds=lambda: app.RATE_LIMIT_BACKOFF_SECONDS,
        delegate=app.delegate,
        parse_response=app.parse_response,
        classify_task_execution_result=getattr(app, "classify_task_execution_result", classify_task_execution_result),
        classify_task_review_result=getattr(app, "classify_task_review_result", classify_task_review_result),
    )


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
        _ = (executor_agent, exclude_agents)
        return list(self.review_agents)

    def can_failover(self, task_id, failed_agent):
        _ = (task_id, failed_agent)
        return self.can_failover_value


def test_background_dispatch_exposes_agent_run_sink(tmp_path, monkeypatch):
    """Background dispatch deve usar o mesmo contrato de eventos do app."""
    class Sink:
        def emit(self, event):
            del event

    sink = Sink()
    app = type("App", (), {})()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"idle_timeout": 45})()
    app.workspace = type(
        "WorkspaceStub",
        (),
        {"cwd": tmp_path, "tasks_db": tmp_path / "tasks.db"},
    )()
    app.visibility = "summary"
    app.auto_approve_mutations = False
    app.agent_run_sink = sink
    services = build_task_services(app)

    class AgentClientStub:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.execution_mode = None
            self.tool_event_callback = None
            self.tool_executor = None

    monkeypatch.setattr("quimera.tasks.executor_pool.AgentClient", AgentClientStub)
    dispatch = services._create_background_dispatch_services()

    assert dispatch._call(dispatch._agent_run_sink) is sink


def test_background_dispatch_client_inherits_supervision_from_chat_client(tmp_path, monkeypatch):
    """O client de background herda pause_idle_if e process_supervisor do chat.

    Sem pause_idle_if, um delegado aguardando tool longa em silêncio morre por
    idle timeout ("returned no response"); sem process_supervisor, seus
    subprocessos escapam do terminate_all() e sobrevivem à sessão.
    """
    supervisor = object()

    def pause_if():
        return False

    app = type("App", (), {})()
    app.renderer = object()
    app.agent_client = type(
        "ChatClient",
        (),
        {"idle_timeout": 45, "process_supervisor": supervisor, "_pause_idle_if": staticmethod(pause_if)},
    )()
    app.workspace = type(
        "WorkspaceStub",
        (),
        {"cwd": tmp_path, "tasks_db": tmp_path / "tasks.db"},
    )()
    app.visibility = "summary"
    app.auto_approve_mutations = False
    services = build_task_services(app)

    captured = {}

    class AgentClientStub:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self.execution_mode = None
            self.tool_event_callback = None
            self.tool_executor = None

    monkeypatch.setattr("quimera.tasks.executor_pool.AgentClient", AgentClientStub)
    dispatch = services._create_background_dispatch_services()

    assert dispatch is not None
    assert captured["process_supervisor"] is supervisor
    assert captured["pause_idle_if"] is pause_if


def test_cancel_background_work_cancels_live_background_clients(tmp_path, monkeypatch):
    """cancel_background_work() propaga o cancel a todos os clients isolados vivos."""
    app = type("App", (), {})()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"idle_timeout": 45})()
    app.workspace = type(
        "WorkspaceStub",
        (),
        {"cwd": tmp_path, "tasks_db": tmp_path / "tasks.db"},
    )()
    app.visibility = "summary"
    app.auto_approve_mutations = False
    services = build_task_services(app)

    class AgentClientStub:
        instances = []

        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.execution_mode = None
            self.tool_event_callback = None
            self.tool_executor = None
            self.cancelled = False
            AgentClientStub.instances.append(self)

        def cancel_active_work(self):
            self.cancelled = True

    monkeypatch.setattr("quimera.tasks.executor_pool.AgentClient", AgentClientStub)
    dispatch_a = services._create_background_dispatch_services()
    dispatch_b = services._create_background_dispatch_services()
    assert dispatch_a is not None and dispatch_b is not None
    assert len(AgentClientStub.instances) == 2

    services.cancel_background_work()

    assert all(client.cancelled for client in AgentClientStub.instances)


def test_cancel_background_work_tolerates_client_failure(tmp_path, monkeypatch):
    """Falha ao cancelar um client não impede o cancel dos demais."""
    app = type("App", (), {})()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"idle_timeout": 45})()
    app.workspace = type(
        "WorkspaceStub",
        (),
        {"cwd": tmp_path, "tasks_db": tmp_path / "tasks.db"},
    )()
    app.visibility = "summary"
    app.auto_approve_mutations = False
    services = build_task_services(app)

    class AgentClientStub:
        instances = []

        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.execution_mode = None
            self.tool_event_callback = None
            self.tool_executor = None
            self.cancelled = False
            AgentClientStub.instances.append(self)

        def cancel_active_work(self):
            if self is AgentClientStub.instances[0]:
                raise RuntimeError("boom")
            self.cancelled = True

    monkeypatch.setattr("quimera.tasks.executor_pool.AgentClient", AgentClientStub)
    dispatch_a = services._create_background_dispatch_services()
    dispatch_b = services._create_background_dispatch_services()
    assert dispatch_a is not None and dispatch_b is not None

    services.cancel_background_work()

    assert AgentClientStub.instances[1].cancelled is True


def test_background_task_tool_executor_disables_ask_user(tmp_path):
    """Executores de /task não devem abrir perguntas interativas ao humano."""
    class AppStub:
        pass

    app = AppStub()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"idle_timeout": 45})()
    app.workspace = type(
        "WorkspaceStub",
        (),
        {"cwd": tmp_path, "tasks_db": tmp_path / "tasks.db"},
    )()
    app.visibility = "summary"
    app.auto_approve_mutations = False

    services = build_task_services(app)
    executor = services._get_background_tool_executor()

    assert executor.config.allow_ask_user is False
    assert executor.is_ask_user_available() is False


def test_task_tool_auto_approval_scope_applies_to_any_background_agent():
    """Tasks em background devem autorizar tools sem depender do driver do agente."""
    app = type("App", (), {})()
    services = build_task_services(app)

    class ApprovalHandler:
        def __init__(self):
            self.calls = []

        def set_thread_approve_all(self, enabled, scope_key=None, silent=False):
            self.calls.append((enabled, scope_key, silent))

    background_handler = ApprovalHandler()
    app_handler = ApprovalHandler()
    app._approval_handler = app_handler

    services._enable_task_tool_auto_approval("cli-agent", approval_handler=background_handler)
    services._disable_task_tool_auto_approval("cli-agent", approval_handler=background_handler)

    expected_calls = [
        (True, f"task:cli-agent:{id(services)}", True),
        (False, f"task:cli-agent:{id(services)}", False),
    ]
    assert background_handler.calls == expected_calls
    assert app_handler.calls == expected_calls


def test_handler_completes_task_when_no_review_agent_available():
    """Verifica que handler completes task when no review agent available."""
    dispatch = DispatchStub(response="resultado final")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[])
    service = TaskRunner(
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
    """Verifica que handler submits for review when other reviewer exists."""
    dispatch = DispatchStub(response="resultado final")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=["pickle"])
    service = TaskRunner(
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
    """Verifica que handler requeues when agent returns no response and failover is possible."""
    dispatch = DispatchStub(response=None)
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[], can_failover=True)
    failures = []
    service = TaskRunner(
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
    """Verifica que handler fails when execution is blocked and no failover exists."""
    dispatch = DispatchStub(response="não consigo executar")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[], can_failover=False)
    service = TaskRunner(
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
    """Verifica que handler fails when user cancels execution."""
    dispatch = DispatchStub(response="resultado parcial")
    system = SystemLayerSpy()
    repo = RepositorySpy()
    policy = FailoverPolicyStub(review_agents=[])
    service = TaskRunner(
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
    app.workspace = type(
        "WorkspaceStub",
        (),
        {"cwd": tmp_path, "tasks_db": tmp_path / "tasks.db"},
    )()
    app.auto_approve_mutations = False
    app.get_agent_profile = lambda _agent_name: None

    services = build_task_services(app)
    pool = services._executor_pool
    monkeypatch.setattr(pool, "_build_task_repository", lambda: repo)
    monkeypatch.setattr(
        pool,
        "_create_background_dispatch_services",
        lambda **kwargs: dispatch,
    )

    ok = pool._build_task_execution_service(policy).handler_for("codex")(
        TaskRecord(id=6, job_id=0, description="executar tarefa", status="in_progress")
    )

    assert ok is True
    assert repo.fail_calls == []
    assert repo.complete_calls == [(6, "resultado final", None)]


def test_background_dispatch_uses_chat_timeout_when_present(tmp_path):
    """Verifica que background dispatch uses chat timeout when present."""
    class AppStub:
        pass

    app = AppStub()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"idle_timeout": 45})()
    app.workspace = type(
        "WorkspaceStub",
        (),
        {"cwd": tmp_path, "tasks_db": tmp_path / "tasks.db"},
    )()
    app.visibility = "summary"
    app.auto_approve_mutations = False

    services = build_task_services(app)

    dispatch = services._get_background_dispatch_services()

    assert dispatch._get_agent_client().idle_timeout == 45


def test_background_dispatch_uses_fallback_timeout_when_chat_timeout_is_missing(tmp_path):
    """Verifica que background dispatch uses fallback timeout when chat timeout is missing."""
    class AppStub:
        pass

    app = AppStub()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"idle_timeout": None})()
    app.workspace = type(
        "WorkspaceStub",
        (),
        {"cwd": tmp_path, "tasks_db": tmp_path / "tasks.db"},
    )()
    app.visibility = "summary"
    app.auto_approve_mutations = False

    services = build_task_services(app)

    dispatch = services._get_background_dispatch_services()

    assert dispatch._get_agent_client().idle_timeout == _BACKGROUND_AGENT_TIMEOUT_SECONDS


def test_parallel_calls_use_background_dispatch_when_available(tmp_path, monkeypatch):
    """Verifica que parallel calls use background dispatch when available."""
    class AppStub:
        pass

    app = AppStub()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"idle_timeout": 45})()
    app.workspace = type(
        "WorkspaceStub",
        (),
        {"cwd": tmp_path, "tasks_db": tmp_path / "tasks.db"},
    )()
    app.visibility = "summary"
    app.auto_approve_mutations = False
    app.delegate_calls = []

    def _chat_delegate(*args, **kwargs):
        app.delegate_calls.append((args, kwargs))
        return "chat-response"

    app.delegate = _chat_delegate
    app.parse_response = lambda raw: (raw, None, None, False, None)

    services = build_task_services(app)
    dispatch = DispatchStub(response="background-response")
    monkeypatch.setattr(
        services,
        "_create_background_dispatch_services",
        lambda **kwargs: dispatch,
    )

    result = services.delegate_for_parallel(
        "codex",
        None,
        "standard",
        tmp_path / "staging",
        0,
    )

    assert result == ("codex", "background-response", False)
    assert dispatch.calls == [
        (
            "codex",
            {
                "delegation": None,
                "primary": False,
                "protocol_mode": "standard",
                "silent": True,
                "show_output": False,
            },
        )
    ]
    assert app.delegate_calls == []


def test_parallel_calls_create_dedicated_background_dispatch_and_close_it(tmp_path, monkeypatch):
    """Verifica que parallel calls create dedicated background dispatch and close it."""
    class AppStub:
        pass

    app = AppStub()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"idle_timeout": 45})()
    app.workspace = type(
        "WorkspaceStub",
        (),
        {"cwd": tmp_path, "tasks_db": tmp_path / "tasks.db"},
    )()
    app.visibility = "summary"
    app.auto_approve_mutations = False
    app.delegate = lambda *args, **kwargs: "chat-response"
    app.parse_response = lambda raw: (raw, None, None, False, None)

    services = build_task_services(app)
    created = []
    closed = []

    class DispatchPerCall(DispatchStub):
        def __init__(self):
            super().__init__(response="background-response")

        def close(self):
            closed.append(self)

    def _create_background_dispatch_services(**kwargs):
        dispatch = DispatchPerCall()
        dispatch.kwargs = kwargs
        created.append(dispatch)
        return dispatch

    monkeypatch.setattr(
        services,
        "_create_background_dispatch_services",
        _create_background_dispatch_services,
    )

    first_cancel = threading.Event()
    second_cancel = threading.Event()
    first = services.delegate_for_parallel(
        "codex",
        None,
        "standard",
        tmp_path / "staging",
        0,
        cancel_event=first_cancel,
    )
    second = services.delegate_for_parallel(
        "codex",
        None,
        "standard",
        tmp_path / "staging",
        1,
        cancel_event=second_cancel,
    )

    assert first == ("codex", "background-response", False)
    assert second == ("codex", "background-response", False)
    assert len(created) == 2
    assert len(closed) == 2
    assert created[0] is not created[1]
    assert created[0].kwargs["cancel_event"] is first_cancel
    assert created[1].kwargs["cancel_event"] is second_cancel


def test_background_dispatch_does_not_reuse_primary_delegate_override(tmp_path, monkeypatch):
    """Background dispatch deve usar seu próprio pipeline, não o delegate do dispatch principal."""

    class AppStub:
        pass

    app = AppStub()
    app.renderer = object()
    app.agent_client = type("ChatClient", (), {"idle_timeout": 45})()
    app.workspace = type(
        "WorkspaceStub",
        (),
        {"cwd": tmp_path, "tasks_db": tmp_path / "tasks.db", "tmp": None},
    )()
    app.visibility = "summary"
    app.auto_approve_mutations = False
    app.execution_mode = None
    app.show_muted_message = None
    app._record_tool_event = None
    app.session_state = {"session_id": "sess", "history_count": 0}
    app.dispatch_services = type("PrimaryDispatch", (), {})()
    app.dispatch_services.delegate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("background dispatch desviou para o delegate principal")
    )
    app.parse_response = lambda raw: (raw, None, None, False, None)

    services = build_task_services(app)
    background_dispatch = services._create_background_dispatch_services()

    class FakeCallService:
        def __init__(self):
            self.calls = []

        def call(self, **kwargs):
            self.calls.append(kwargs)
            return "through-service"

    fake_service = FakeCallService()
    monkeypatch.setattr(background_dispatch, "_get_agent_call_service", lambda: fake_service)

    result = background_dispatch.delegate("codex", silent=True, show_output=False)

    assert result == "through-service"
    assert len(fake_service.calls) == 1
    assert fake_service.calls[0]["agent"] == "codex"

"""Adapters de compatibilidade para testes que constroem serviços a partir de um app fake.

Produção monta ``AppDispatchServices`` e ``AppSystemLayer`` com dependências
explícitas (ver ``quimera/app/bootstrap/wiring.py`` e
``quimera/tasks/executor_pool.py``). Os testes históricos usam objetos
app-like (SimpleNamespace/stubs) com os atributos legados; estes adapters
preservam o mapeamento "app-like → kwargs" apenas para esses fakes.
"""
from contextlib import nullcontext

from quimera.app.dispatch import AppDispatchServices
from quimera.app.display_service import DisplayService
from quimera.app.system_layer import AppSystemLayer


def dispatch_services_from_app(app, **kwargs):
    """Constrói AppDispatchServices a partir de um objeto app-like."""
    from quimera.domain.session_state import SessionRuntimeState

    _system_layer = getattr(app, 'system_layer', None)
    if 'session_state' not in kwargs:
        existing = getattr(app, '_chat_state', None) or getattr(app, 'session_state', None)
        if isinstance(existing, SessionRuntimeState):
            kwargs['session_state'] = existing
        elif isinstance(existing, dict):
            rt = SessionRuntimeState(
                history=getattr(app, 'history', []),
                shared_state=getattr(app, 'shared_state', {}),
            )
            rt.session_state.update(existing)
            app._chat_state = rt
            app.session_state = rt.session_state
            kwargs['session_state'] = rt
        elif existing is None:
            rt = SessionRuntimeState(
                history=getattr(app, 'history', []),
                shared_state=getattr(app, 'shared_state', {}),
            )
            app._chat_state = rt
            app.session_state = rt.session_state
            kwargs['session_state'] = rt
    return AppDispatchServices(
        prompt_builder=lambda: getattr(app, 'prompt_builder', None),
        renderer=lambda: getattr(app, 'renderer', None),
        get_agent_profile=lambda agent_name: (
            getattr(app, 'get_agent_profile', lambda n: None)(agent_name)
        ),
        get_execution_mode=lambda: getattr(app, 'execution_mode', None),
        refresh_task_state=lambda: getattr(
            getattr(app, 'task_services', None), 'refresh_task_shared_state', lambda: None
        )(),
        agent_run_sink=getattr(app, 'agent_run_sink', None),
        debug_prompt_metrics=lambda: getattr(app, 'debug_prompt_metrics', False),
        redisplay_prompt=lambda **kw: getattr(app, '_redisplay_user_prompt_if_needed', lambda **kw_: None)(**kw),
        output_lock=lambda: getattr(app, '_output_lock', None),
        counter_lock=lambda: getattr(app, '_counter_lock', None),
        session_metrics=lambda: getattr(app, 'session_metrics', None),
        print_response_fn=lambda agent, text: getattr(app, 'print_response', lambda a, t: None)(agent, text),
        persist_message_fn=lambda agent, text: getattr(
            getattr(app, 'session_services', None), 'persist_message', lambda a, t: None
        )(agent, text),
        record_session_metric=lambda agent, metric, elapsed: (
            getattr(getattr(app, 'session_metrics', None), 'record_agent_metric', lambda *a: None)(
                app, agent, metric, elapsed
            )
        ),
        record_tool_event_fn=lambda agent, **kw: (
            getattr(getattr(app, 'session_metrics', None), 'record_tool_event', lambda *a, **kw_: None)(
                app, agent, **kw
            )
        ),
        notify_warning=getattr(_system_layer, 'show_warning_message', lambda m: None),
        notify_retry=getattr(_system_layer, 'notify_agent_retry', None),
        notify_error=getattr(_system_layer, 'show_error_message', lambda m: None),
        max_retries=lambda: getattr(app, 'MAX_RETRIES', 2),
        retry_backoff=lambda: getattr(app, 'RETRY_BACKOFF_SECONDS', 1),
        rate_limit_backoff=lambda: getattr(app, 'RATE_LIMIT_BACKOFF_SECONDS', 1),
        record_failure=getattr(app, 'record_failure', None),
        record_success=getattr(app, 'record_success', None),
        get_agent_client=lambda: getattr(app, 'agent_client', None),
        get_tool_executor=lambda: getattr(app, 'tool_executor', None),
        get_delegate_fn_override=lambda: getattr(app, '_delegate', None),
        **kwargs,
    )


class LegacyProfileResolver:
    def __init__(self, app):
        self._app = app

    def get(self, name: str):
        getter = getattr(self._app, "get_agent_profile", None)
        if callable(getter):
            return getter(name)
        return None

    @property
    def profiles(self) -> list:
        getter = getattr(self._app, "get_available_profiles", None)
        if callable(getter):
            return list(getter())
        return []


class LegacyAgentPoolAdapter:
    """Adapter mínimo do contrato de agent pool sobre ``app.active_agents``."""

    def __init__(self, app):
        self._app = app

    @property
    def agents(self) -> list[str]:
        return list(getattr(self._app, "active_agents", []) or [])

    def add(self, name: str) -> None:
        agents = self.agents
        if name not in agents:
            agents.append(name)
            setattr(self._app, "active_agents", agents)

    def set(self, agents: list[str]) -> None:
        setattr(self._app, "active_agents", list(agents))

    def __contains__(self, name: str) -> bool:
        return name in self.agents


def _get_runtime_or_legacy_attr(app, runtime_name: str, legacy_name: str, default=None):
    runtime_state = getattr(app, "runtime_state", None)
    if runtime_state is not None:
        return getattr(runtime_state, runtime_name, default)
    return getattr(app, legacy_name, default)


def _get_input_gate_status(app) -> bool | None:
    input_gate = getattr(app, "input_gate", None)
    if input_gate is None:
        return None
    is_active = getattr(input_gate, "is_active", None)
    if not callable(is_active):
        return None
    try:
        status = is_active()
    except Exception:
        return None
    if isinstance(status, bool):
        return status
    return None


def _get_input_gate_owner_thread_id(app):
    input_gate = getattr(app, "input_gate", None)
    if input_gate is None:
        return None
    getter = getattr(input_gate, "get_owner_thread_id", None)
    if not callable(getter):
        return None
    try:
        owner = getter()
    except Exception:
        return None
    if isinstance(owner, int):
        return owner
    return None


def resolve_input_status_from_app(app):
    gate_status = _get_input_gate_status(app)
    if gate_status is not None:
        return gate_status
    return _get_runtime_or_legacy_attr(
        app,
        runtime_name="nonblocking_input_status",
        legacy_name="_nonblocking_input_status",
        default="idle",
    )


def resolve_prompt_owner_thread_id_from_app(app):
    gate_owner_thread_id = _get_input_gate_owner_thread_id(app)
    if gate_owner_thread_id is not None:
        return gate_owner_thread_id
    return _get_runtime_or_legacy_attr(
        app,
        runtime_name="prompt_owning_thread_id",
        legacy_name="_prompt_owning_thread_id",
        default=None,
    )


def _handler_prompt_active_from_app(app):
    input_gate = getattr(app, "input_gate", None)
    if input_gate is not None:
        is_active = getattr(input_gate, "is_active", None)
        if callable(is_active):
            try:
                status = is_active()
                if isinstance(status, bool):
                    return status
            except Exception:
                pass
    runtime_state = getattr(app, "runtime_state", None)
    if runtime_state is not None:
        return getattr(runtime_state, "nonblocking_input_status", None)
    return getattr(app, "_nonblocking_input_status", None)


def bind_handler_app(handler, app):
    """Materializa os AppCallbacks de um PromptAwareStderrHandler a partir de um app fake."""
    handler._app = app
    if app is None:
        handler._callbacks = None
        return

    def _noop(*_args, **_kwargs):
        return None

    handler.bind_callbacks(
        output_lock=getattr(app, "_output_lock", None),
        redisplay_prompt=getattr(app, "_redisplay_user_prompt_if_needed", _noop),
        show_error=getattr(app.system_layer, "show_error_message", _noop),
        show_warning=getattr(app.system_layer, "show_warning_message", _noop),
        show_system=getattr(app.system_layer, "show_system_message", _noop),
        show_muted=getattr(app.system_layer, "show_muted_message", _noop),
        is_reading=lambda: _handler_prompt_active_from_app(app),
        debug_enabled=lambda: bool(getattr(app, "debug_prompt_metrics", False)),
    )


def system_layer_from_app(app, **overrides):
    """Constrói AppSystemLayer a partir de um objeto app-like."""
    input_gate = getattr(app, "input_gate", None)
    task_services = getattr(app, "task_services", None)
    display_service = DisplayService(
        renderer=lambda: getattr(app, "renderer", None),
        input_status_getter=lambda: resolve_input_status_from_app(app),
        redisplay_prompt=getattr(app, "_redisplay_user_prompt_if_needed", None),
        output_lock=lambda: getattr(app, "_output_lock", nullcontext()),
        prompt_owner_thread_id_getter=lambda: resolve_prompt_owner_thread_id_from_app(app),
        run_above_active_prompt=getattr(input_gate, "run_in_terminal_message", None),
        deferred_messages_getter=lambda: getattr(app, "_deferred_system_messages", []),
        max_deferred_messages_getter=lambda: getattr(app, "_MAX_DEFERRED_SYSTEM_MESSAGES", 20),
    )
    agent_pool = getattr(app, "agent_pool", None)
    if agent_pool is None or not hasattr(agent_pool, "agents"):
        agent_pool = LegacyAgentPoolAdapter(app)
    kwargs = dict(
        display_service=display_service,
        agent_pool=agent_pool,
        profile_resolver=LegacyProfileResolver(app),
        prompt_builder=getattr(app, "prompt_builder", None),
        history_getter=lambda: list(getattr(app, "history", []) or []),
        shared_state_getter=lambda: getattr(app, "shared_state", None),
        execution_mode_getter=lambda: getattr(app, "execution_mode", None),
        get_selected_agents=lambda: list(getattr(app, "selected_agents", []) or []),
        set_selected_agents=lambda agents: setattr(app, "selected_agents", list(agents)),
        clear_screen=lambda: getattr(app, "clear_terminal_screen", lambda: None)(),
        redisplay_prompt=getattr(app, "_redisplay_user_prompt_if_needed", None),
        prompt_owner_thread_id_getter=lambda: resolve_prompt_owner_thread_id_from_app(app),
        run_above_active_prompt=getattr(input_gate, "run_in_terminal_message", None),
        read_user_input=getattr(app, "read_user_input", None),
        task_command_handler=getattr(task_services, "handle_task_command", None),
        bugs_command_handler=getattr(app, "_handle_bugs_command", None),
        session_state_manager=getattr(app, "session_state_mgr", None),
        approval_handler_getter=lambda: getattr(app, "_approval_handler", None),
        context_manager=getattr(app, "context_manager", None),
        profile_registry=getattr(app, "_profile_registry", None),
        workspace_policy_getter=getattr(app, "get_workspace_policy_name", None),
        workspace_policy_setter=getattr(app, "set_workspace_policy_name", None),
        deferred_messages_getter=lambda: getattr(app, "_deferred_system_messages", []),
        max_deferred_messages_getter=lambda: getattr(app, "_MAX_DEFERRED_SYSTEM_MESSAGES", 20),
    )
    kwargs.update(overrides)
    return AppSystemLayer(**kwargs)


def chat_round_orchestrator_from_app(app, **overrides):
    """Constrói ChatRoundOrchestrator a partir de um objeto app-like."""
    from quimera.app.agent_pool import AgentPool
    from quimera.app.chat_round import ChatRoundOrchestrator
    from quimera.domain.session_state import SessionRuntimeState

    agent_pool = getattr(app, "agent_pool", None)
    if agent_pool is None:
        agent_pool = AgentPool(getattr(app, "active_agents", []) or [])
    session_state = getattr(app, "_chat_state", None)
    if session_state is None:
        app_session_state = getattr(app, "session_state", None)
        if isinstance(app_session_state, dict):
            session_state = SessionRuntimeState.from_legacy(
                shared_state=getattr(app, "shared_state", None),
                session_meta=app_session_state,
                history=getattr(app, "history", None),
            )
        else:
            session_state = app_session_state
    kwargs = dict(
        dispatch_services=getattr(app, "dispatch_services", None),
        parse_routing=lambda user: app.parse_routing(user),
        agent_pool=agent_pool,
        session_services=getattr(app, "session_services", None),
        parse_response=lambda response: app.parse_response(response),
        agent_client=getattr(app, "agent_client", None),
        turn_manager=getattr(app, "turn_manager", None),
        task_services=getattr(app, "task_services", None),
        get_agent_profile=getattr(app, "get_agent_profile", None),
        behavior_metrics=getattr(app, "behavior_metrics", None),
        threads=getattr(app, "threads", 1),
        session_state=session_state,
        show_system_message=getattr(
            getattr(app, "system_layer", None), "show_system_message", None
        ),
        renderer=getattr(app, "renderer", None),
        set_parallel_toolbar_state=getattr(app, "_set_parallel_toolbar_state", None),
        ui_queue=getattr(app, "_ui_event_queue", None),
    )
    kwargs.update(overrides)
    return ChatRoundOrchestrator(**kwargs)

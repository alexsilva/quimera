import io
import importlib
import re
import tempfile
import threading
import time
import unittest
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import quimera.app as app_module
import quimera.cli as cli_module
import quimera.profiles as profiles
from quimera.agents import AgentClient
from quimera.app import QuimeraApp
from quimera.app.chat_round import ChatRoundOrchestrator
from quimera.app.agent_pool import AgentPool
from quimera.app.core import TurnManager, normalize_agent_name
from quimera.app.staging import merge_staging_to_workspace
from quimera.app.dispatch import AppDispatchServices
from tests.legacy_app_adapters import (
    bind_handler_app,
    chat_round_orchestrator_from_app,
    dispatch_services_from_app,
    system_layer_from_app,
)
from quimera.app.inputs import AppInputServices, read_from_editor, read_user_input_with_timeout
from quimera.app.session import AppSessionServices
from quimera.app.system_layer import AppSystemLayer
from quimera.tasks.services import AppTaskServices, delegate_for_parallel
from quimera.tasks.classifiers import classify_task_execution_result, classify_task_review_result
from quimera.app.protocol import AppProtocol
from quimera.app.session_metrics import SessionMetricsService
from quimera.tasks.events import TaskCompleted
from quimera.app.event_sink import EventSink
from quimera.app.session_bootstrap import (
    resolve_render_debug_log_path,
    resolve_session_log_path,
)
from quimera.cli import main as cli_main
from quimera.config import DEFAULT_HISTORY_WINDOW
from quimera.constants import CMD_AGENTS, CMD_CLEAR, CMD_CONNECT, CMD_DISCONNECT, CMD_HELP, CMD_POLICY, CMD_PROMPT, EXTEND_MARKER, MSG_SHUTDOWN, TaskStatus, TaskType, Visibility, build_agents_help, build_help
from quimera.profiles import ExecutionProfile
from quimera.prompt_templates import PromptText
from quimera.profiles.base import ProfileRegistry
from quimera.runtime.models import TaskRecord, ToolCall
from quimera.domain.session_state import SessionRuntimeState
from quimera.profiles.base import OpenAIConnection
from quimera.delegate_presenter import DelegatePresenter
from quimera.prompt import PromptBuilder
from quimera.shared_state import AGENT_STATE_KEYS
from quimera.prompt_templates import prompt_template
from quimera.runtime.approval import ApprovalHandler
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.tasks.planning import TaskClassification
from quimera.tasks.api import add_job, complete_task, create_task, init_db, list_tasks
from quimera.session_summary import SessionSummarizer, build_chain_summarizer
from quimera.ui import _agent_style
from quimera.ui.base import RendererBase

AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"
AGENT_GEMINI = "antigravity"


def _extract_block(prompt: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*>\n?(.*?)\n?</{tag}>", prompt, re.DOTALL)
    if not match:
        raise AssertionError(f"Bloco <{tag}> não encontrado no prompt")
    return match.group(1)


def _make_protocol(app, **_kwargs):
    return AppProtocol(
        lock=getattr(app, '_shared_state_lock', None) or getattr(app, '_lock', threading.Lock()),
        shared_state=getattr(app, 'shared_state', {}),
        workspace=getattr(app, 'workspace', None),
        turn_stamps=getattr(app, '_turn_stamps', None),
    )


class DummyRenderer(RendererBase):
    def __init__(self):
        self.warnings = []
        self.system_messages = []
        self.plain_messages = []
        self.delegations = []
        self.prompt_previews = []
        self._output_lock = threading.Lock()
        self.task_services = None

    def show_warning(self, message):
        self.warnings.append(message)

    def show_system(self, message):
        self.system_messages.append(message)

    def show_plain(self, message):
        self.plain_messages.append(message)

    def show_delegation(self, from_agent, to_agent, task=None, **_kwargs):
        self.delegations.append((from_agent, to_agent, task))

    def show_prompt_preview(self, agent, content):
        self.prompt_previews.append((agent, content))

    @contextmanager
    def external_window(self, window_id, title="", metadata=None):
        """Provide explicit external-window ownership for editor tests."""
        yield None

    def reset_visual_state(self, *a, **kw): pass


class DummyContextManager:
    SUMMARY_MARKER = "<SUMMARY>"

    def __init__(self, *args, **kwargs):
        pass

    def load(self):
        return ""

    def load_session(self):
        return ""


class DummyConfigManager:
    def __init__(self, _config_path=None):
        self.user_name = "Você"
        self.history_window = DEFAULT_HISTORY_WINDOW
        self.auto_summarize_threshold = 30
        self.idle_timeout_seconds = 300
        self.theme = None
        self.density = "normal"


class DummyStorage:
    def __init__(self, *args, **kwargs):
        self.session_id = "sessao-2026-03-27-123456"

    def append_log(self, role, content):
        self.last_log = (role, content)

    def get_log_file(self):
        return "/tmp/quimera.log"

    def get_history_file(self):
        return Path("/tmp/sessao-2026-03-27-123456.json")

    def save_history(self, history, shared_state=None):
        self.saved_history = history
        self.saved_shared_state = shared_state

    def load_last_session(self):
        return {"messages": [], "shared_state": {}}


class DummyAgentClient:
    def __init__(self):
        self._user_cancelled = False
        self._cancel_event = threading.Event()
        self.rate_limit_detected = False

    def close(self):
        return None

    def reset_cancel_state(self):
        self._user_cancelled = False
        self._cancel_event.clear()


def build_task_services(app):
    if not hasattr(app, "task_executor_factory"):
        app.task_executor_factory = lambda *args, **kwargs: __import__(
            "quimera.app.bootstrap.wiring", fromlist=["create_executor"]
        ).create_executor(*args, **kwargs)
    if not hasattr(app, "current_job_id"):
        app.current_job_id = None
    if not hasattr(app, "agent_pool"):
        app.agent_pool = AgentPool([])
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
        class _DynamicWorkspace:
            def __init__(self, app_ref):
                self._app_ref = app_ref
                self.cwd = Path("/tmp")
                self.tmp = None

            @property
            def tasks_db(self):
                path = getattr(self._app_ref, "tasks_db_path", None)
                return Path(path) if path else None
        app.workspace = _DynamicWorkspace(app)
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
    if hasattr(app, "session_state") and isinstance(app.session_state, dict):
        app._chat_state = SessionRuntimeState.from_legacy(
            shared_state=getattr(app, "shared_state", None),
            session_meta=app.session_state,
            history=getattr(app, "history", None),
        )
    elif not hasattr(app, "session_state"):
        app.session_state = None
        app._chat_state = SessionRuntimeState()
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


def _make_session_services(app):
    """Constrói AppSessionServices com dependências explícitas a partir de app de teste."""
    agent_pool = getattr(app, "agent_pool", None)
    if agent_pool is None:
        active = getattr(app, "active_agents", None)
        if active is not None and len(active) > 0:
            agent_pool = SimpleNamespace(primary=active[0])
        else:
            agent_pool = SimpleNamespace(primary=None)

    prompt_builder = getattr(app, "prompt_builder", None)
    if prompt_builder is None and not hasattr(app, "prompt_builder"):
        prompt_builder = SimpleNamespace(history_window=None)

    session_state = getattr(app, "_chat_state", None)
    if session_state is None:
        session_state = SessionRuntimeState.from_legacy(
            history=app.history,
            shared_state=getattr(app, "shared_state", {}),
            session_meta=getattr(app, "session_state", {}),
            shared_state_lock=getattr(app, "_shared_state_lock", threading.Lock()),
        )

    return AppSessionServices(
        session_state=session_state,
        storage=getattr(app, "storage", DummyStorage()),
        renderer=getattr(app, "renderer", DummyRenderer()),
        agent_pool=agent_pool,
        context_manager=getattr(app, "context_manager", Mock()),
        session_summarizer=getattr(app, "session_summarizer", Mock()),
        task_services=getattr(app, "task_services", Mock()),
        prompt_builder=prompt_builder,
        auto_summarize_threshold=getattr(app, "auto_summarize_threshold", None),
        summary_agent_preference=getattr(app, "summary_agent_preference", None),
        agent_client=getattr(app, "agent_client", None),
    )


def _materialize_chat_lifecycle(app):
    """Cria ChatLifecycle com bridge para métodos mock já setados no app."""
    from quimera.app.chat_lifecycle import ChatLifecycle
    from unittest.mock import Mock

    # Usa o orquestrador real se existir e _process_chat_message não foi sobrescrito como mock
    real_orchestrator = getattr(app, 'chat_round_orchestrator', None)
    mock_overridden = '_process_chat_message' in app.__dict__

    if real_orchestrator is not None and not mock_overridden:
        stub_orchestrator = real_orchestrator
    else:
        class _StubOrchestrator:
            def process(self, user, ctx):
                fn = getattr(app, '_process_chat_message', None)
                if callable(fn) and '_process_chat_message' in app.__dict__:
                    fn(user)
        stub_orchestrator = _StubOrchestrator()

    class _AppBridgeLifecycle(ChatLifecycle):
        def _do_process_message(self, user):
            do_fn = app.__dict__.get('_do_process_chat_message')
            if callable(do_fn):
                do_fn(user)
                return
            for attr in ('session_services', 'task_services', 'dispatch_services', 'agent_client', 'parse_routing', 'parse_response'):
                val = getattr(app, attr, None)
                if val is not None:
                    setattr(self, f'_{attr}', val)
            chat_state = getattr(app, '_chat_state', None)
            if chat_state is not None:
                self._session_state = chat_state
            super()._do_process_message(user)

        def process_sync_message_with_slot(self, user):
            fn = app.__dict__.get('_process_sync_chat_message_with_slot')
            if callable(fn):
                fn(user)
            else:
                super().process_sync_message_with_slot(user)

        def handle_local_interrupt(self):
            fn = app.__dict__.get('_handle_local_processing_interrupt')
            if callable(fn):
                fn()
            else:
                super().handle_local_interrupt()

        def submit_async_message(self, user):
            fn = app.__dict__.get('_submit_async_chat_message')
            if callable(fn):
                fn(user)
            else:
                super().submit_async_message(user)

        def drain_ui_events(self, ui_queue):
            fn = app.__dict__.get('_drain_ui_events')
            if callable(fn):
                fn(ui_queue)
            else:
                super().drain_ui_events(ui_queue)

    app.chat_lifecycle = _AppBridgeLifecycle(
        chat_round_orchestrator=stub_orchestrator,
        system_layer=getattr(app, 'system_layer', Mock()),
        renderer=getattr(app, 'renderer', None),
        runtime_state=getattr(app, 'runtime_state', None),
        turn_manager=getattr(app, 'turn_manager', None),
        agent_client=getattr(app, 'agent_client', None),
        ui_event_handler=getattr(app, '_ui_event_handler', None),
        session_services=getattr(app, 'session_services', None),
        task_services=getattr(app, 'task_services', None),
        session_state=getattr(app, '_chat_state', None),
        dispatch_services=getattr(app, 'dispatch_services', None),
        parse_routing=getattr(app, 'parse_routing', None),
        parse_response=getattr(app, 'parse_response', None),
        refresh_parallel_toolbar=getattr(app, '_refresh_parallel_toolbar', lambda: None),
    )


def materialize_internal_services(app):
    import threading
    from contextlib import nullcontext
    from unittest.mock import Mock
    if getattr(app, "_output_lock", None) is None:
        app._output_lock = threading.Lock()
    if getattr(app, "session_state_mgr", None) is None:
        app.session_state_mgr = Mock()
    if getattr(app, "runtime_state", None) is None:
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
    if getattr(app, "agent_pool", None) is None:
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([])
    if getattr(app, "storage", None) is None:
        app.storage = DummyStorage()
    if getattr(app, "renderer", None) is None:
        app.renderer = DummyRenderer()
    if getattr(app, "protocol", None) is None:
        app.protocol = _make_protocol(app)
    if getattr(app, "task_executor_factory", None) is None:
        app.task_executor_factory = lambda *args, **kwargs: __import__(
            "quimera.app.bootstrap.wiring", fromlist=["create_executor"]
        ).create_executor(*args, **kwargs)
    if getattr(app, "task_services", None) is None:
        app.task_services = build_task_services(app)
    if getattr(app, "dispatch_services", None) is None:
        app.dispatch_services = dispatch_services_from_app(app)
    if getattr(app, "input_services", None) is None:
        app.input_services = AppInputServices(
            app.renderer,
            input_resolver=lambda: input,
            get_input_status=lambda: getattr(app.runtime_state, "nonblocking_input_status", "idle"),
            set_input_status=lambda v: setattr(app.runtime_state, "nonblocking_input_status", v),
            set_prompt_text=lambda v: setattr(app.runtime_state, "nonblocking_prompt_text", v),
            set_prompt_owner=lambda v: setattr(app.runtime_state, "prompt_owning_thread_id", v),
            set_prompt_visible=lambda v: setattr(app.runtime_state, "nonblocking_prompt_visible", v),
            flush_deferred_messages=lambda: app.system_layer.flush_deferred_messages(),
            output_lock=getattr(app, "_output_lock", None),
        )
    if getattr(app, "system_layer", None) is None:
        app.system_layer = system_layer_from_app(app)
    if getattr(app, "chat_round_orchestrator", None) is None:
        _agent_pool = getattr(app, "agent_pool", None)
        if _agent_pool is None:
            from quimera.app.agent_pool import AgentPool
            _agent_pool = AgentPool(getattr(app, "active_agents", []) or [])
            app.agent_pool = _agent_pool
        _session_state = getattr(app, "_chat_state", None) or SessionRuntimeState()
        app.chat_round_orchestrator = ChatRoundOrchestrator(
            dispatch_services=getattr(app, "dispatch_services", None),
            parse_routing=lambda u: app.parse_routing(u),
            agent_pool=_agent_pool,
            session_services=getattr(app, "session_services", None),
            parse_response=lambda r: app.parse_response(r),
            agent_client=getattr(app, "agent_client", None),
            turn_manager=getattr(app, "turn_manager", None),
            task_services=getattr(app, "task_services", None),
            get_agent_profile=getattr(app, "get_agent_profile", None),
            behavior_metrics=getattr(app, "behavior_metrics", None),
            threads=getattr(app, "threads", 1),
            session_state=_session_state,
            renderer=getattr(app, "renderer", None),
            show_system_message=getattr(getattr(app, "system_layer", None), "show_system_message", None),
            merge_staging_to_workspace=merge_staging_to_workspace,
        )
    if not hasattr(app, "execution_mode"):
        app.execution_mode = None
    if getattr(app, "command_router", None) is None:
        from quimera.app.command_router import CommandRouter
        from quimera.app.agent_pool import AgentPool
        _existing = getattr(app, "agent_pool", None)
        _agent_pool = _existing if _existing is not None else AgentPool([])
        app.command_router = CommandRouter(
            agent_pool=_agent_pool,
            renderer=getattr(app, "renderer", None),
            get_active_agent_profiles=getattr(app, "get_active_agent_profiles", lambda: []),
            set_execution_mode=getattr(app, "_set_execution_mode", lambda mode: None),
            normalize_agent_name=getattr(app, "_normalize_agent_name", lambda n: n),
            selected_agents=getattr(app, "selected_agents", []),
            get_available_profiles=getattr(app, "get_available_profiles", lambda: []),
        )
    if getattr(app, "bug_services", None) is None:
        from unittest.mock import Mock
        app.bug_services = Mock()
    if getattr(app, "_ui_event_handler", None) is None:
        from quimera.app.ui_event_handler import UiEventHandler
        app._ui_event_handler = UiEventHandler(
            renderer=getattr(app, "renderer", None),
            input_gate=getattr(app, "input_gate", None),
            runtime_state=getattr(app, "runtime_state", None),
            system_layer=getattr(app, "system_layer", None),
            event_sink=getattr(app, "event_sink", None),
            show_muted_message=(
                getattr(app, "show_muted_message", None)
                or getattr(getattr(app, "system_layer", None), "show_muted_message", lambda msg: None)
            ),
            show_system_message=(
                getattr(app, "show_system_message", None)
                or getattr(getattr(app, "system_layer", None), "show_system_message", lambda msg: None)
            ),
            show_warning_message=(
                getattr(app, "show_warning_message", None)
                or getattr(getattr(app, "system_layer", None), "show_warning_message", lambda msg: None)
            ),
            show_error_message=(
                getattr(app, "show_error_message", None)
                or getattr(getattr(app, "system_layer", None), "show_error_message", lambda msg: None)
            ),
            redisplay_user_prompt=getattr(app, "_redisplay_user_prompt_if_needed", lambda: None),
            output_lock=getattr(app, "_output_lock", nullcontext()),
        )
    if getattr(app, "chat_lifecycle", None) is None:
        _materialize_chat_lifecycle(app)
    return app


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        importlib.reload(profiles)

    @unittest.skipUnless(
        hasattr(cli_module, "TerminalRenderer") and hasattr(cli_module, "AgentClient"),
        "interactive-test CLI não está disponível nesta versão",
    )
    def test_cli_runs_interactive_test_with_default_prompt(self):
        """Verifica que cli runs interactive test with default prompt."""
        class FakeRenderer(RendererBase):
            instances = []

            def __init__(self):
                self.system_messages = []
                self.plain_messages = []
                FakeRenderer.instances.append(self)

            def show_system(self, message):
                self.system_messages.append(message)

            def show_plain(self, message):
                self.plain_messages.append(message)

        calls = []

        class FakeAgentClient:
            def __init__(self, renderer, metrics_file=None):
                self.renderer = renderer

            def call(self, agent, prompt):
                calls.append((agent, prompt))
                return "saida limpa"

            def close(self):
                pass

        with patch("quimera.cli.TerminalRenderer", FakeRenderer), patch(
                "quimera.cli.AgentClient", FakeAgentClient
        ), patch("sys.argv", ["quimera", "--interactive-test"]):
            cli_main()

        self.assertEqual(len(FakeRenderer.instances), 1)
        self.assertEqual(calls, [(AGENT_CLAUDE,
                                  "Use uma ferramenta de shell para executar o comando `pwd` e me diga o diretório atual. Se a ferramenta pedir aprovação, mostre o prompt normalmente.")])
        self.assertTrue(FakeRenderer.instances[0].system_messages)
        self.assertEqual(FakeRenderer.instances[0].plain_messages, ["\n--- RESULTADO LIMPO ---\n", "saida limpa"])

    @unittest.skipUnless(
        hasattr(cli_module, "TerminalRenderer") and hasattr(cli_module, "AgentClient"),
        "interactive-test CLI não está disponível nesta versão",
    )
    def test_cli_runs_interactive_test_with_custom_prompt(self):
        """Verifica que cli runs interactive test with custom prompt."""
        calls = []

        class FakeRenderer(RendererBase):
            instances = []

            def __init__(self):
                self.system_messages = []
                self.plain_messages = []
                FakeRenderer.instances.append(self)

            def show_system(self, message):
                self.system_messages.append(message)

            def show_plain(self, message):
                self.plain_messages.append(message)

        class FakeAgentClient:
            def __init__(self, renderer, metrics_file=None):
                self.renderer = renderer

            def call(self, agent, prompt):
                calls.append((agent, prompt))
                return None

            def close(self):
                pass

        with patch("quimera.cli.ConfigManager", DummyConfigManager), patch(
                "quimera.cli.TerminalRenderer", FakeRenderer
        ), patch("quimera.cli.AgentClient", FakeAgentClient), patch(
            "sys.argv", ["quimera", "--interactive-test", "codex", "--test-prompt", "rode", "pwd"]
        ):
            cli_main()

        self.assertEqual(calls, [(AGENT_CODEX, "rode pwd")])
        self.assertEqual(len(FakeRenderer.instances), 1)
        self.assertEqual(FakeRenderer.instances[0].system_messages, ["rode pwd"])

    def test_cli_passes_visibility_to_app(self):
        """Verifica que cli passes visibility to app."""
        captured = {}

        class FakeApp:
            def __init__(self, cwd, **kwargs):
                captured["cwd"] = cwd
                captured.update(kwargs)
                self.tool_executor = object()

            def run(self):
                captured["ran"] = True

            def configure_mcp_socket(self, socket_path: str | None, token: str | None = None) -> None:
                pass

        with patch("quimera.cli.QuimeraApp", FakeApp), patch("sys.argv", ["quimera", "--visibility", "full"]):
            cli_main()

        self.assertEqual(captured["visibility"], Visibility.FULL)
        self.assertTrue(captured["ran"])

    def test_cli_rejects_legacy_spy_flag(self):
        """Verifica que cli rejects legacy spy flag."""
        with patch("sys.argv", ["quimera", "--spy"]):
            with self.assertRaises(SystemExit) as exc:
                cli_main()

        self.assertEqual(exc.exception.code, 2)

    def test_parse_response_detects_extend_marker_at_end(self):
        """Verifica que parse response detects extend marker at end."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = _make_protocol(app)
        app.shared_state = {}

        response, _, _, extend, _ = app.parse_response(f"Resposta objetiva {EXTEND_MARKER}")

        self.assertEqual(response, "Resposta objetiva")
        self.assertTrue(extend)

    def test_parse_response_keeps_plain_response(self):
        """Verifica que parse response keeps plain response."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = _make_protocol(app)
        app.shared_state = {}

        response, target, delegation, extend, _ = app.parse_response("Resposta objetiva")

        self.assertEqual(response, "Resposta objetiva")
        self.assertIsNone(target)
        self.assertIsNone(delegation)
        self.assertFalse(extend)

    def test_parse_routing_rejects_double_prefix(self):
        """Verifica que parse routing rejects double prefix."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]

        materialize_internal_services(app)
        decision = app.parse_routing("/claude /codex revisar isso")
        agent, message, explicit = decision

        self.assertIsNone(agent)
        self.assertIsNone(message)
        self.assertTrue(app.renderer.warnings)
        self.assertEqual(decision.source, "double_prefix")

    def test_parse_routing_treats_unknown_prefix_as_plain_message(self):
        """Verifica que parse routing treats unknown prefix as plain message."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]

        materialize_internal_services(app)
        decision = app.parse_routing("/code revise isso")
        agent, message, explicit = decision

        self.assertEqual(agent, AGENT_CLAUDE)
        self.assertEqual(message, "/code revise isso")
        self.assertFalse(explicit)
        self.assertEqual(decision.source, "primary")

    def test_parse_routing_non_prefixed_input_under_orchestrator_is_not_explicit(self):
        """o/agente com texto residual deve entrar como input normal do orquestrador."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.agent_pool.set_orchestrator(AGENT_CLAUDE)
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]

        materialize_internal_services(app)
        decision = app.parse_routing("revise isso")
        agent, message, explicit = decision

        self.assertEqual(agent, AGENT_CLAUDE)
        self.assertEqual(message, "revise isso")
        self.assertFalse(explicit)
        self.assertEqual(decision.source, "orchestrator")

    def test_handle_command_shows_help(self):
        """Verifica que handle command shows help."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.system_layer = system_layer_from_app(app)

        materialize_internal_services(app)
        handled = app.system_layer.handle_command(CMD_HELP)

        self.assertTrue(handled)
        expected_help = build_help([AGENT_CLAUDE, AGENT_CODEX])
        self.assertEqual(app.renderer.system_messages, [expected_help])

    def test_handle_command_shows_agents(self):
        """Verifica que handle command shows agents."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.system_layer = system_layer_from_app(app)

        materialize_internal_services(app)
        handled = app.system_layer.handle_command(CMD_AGENTS)

        self.assertTrue(handled)
        expected_agents = build_agents_help([AGENT_CLAUDE, AGENT_CODEX])
        self.assertEqual(app.renderer.system_messages, [expected_agents])

    def test_handle_command_clears_terminal(self):
        """Verifica que handle command clears terminal."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.clear_terminal_screen = Mock()
        app.system_layer = system_layer_from_app(app)

        materialize_internal_services(app)
        handled = app.system_layer.handle_command(CMD_CLEAR)

        self.assertTrue(handled)
        app.clear_terminal_screen.assert_called_once_with()

    def test_handle_command_shows_prompt_preview_for_default_agent(self):
        """Verifica que handle command shows prompt preview for default agent."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._redisplay_user_prompt_if_needed = Mock()
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "idle"
        app._deferred_system_messages = []
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.history = [{"role": "human", "content": "Pedido atual"}]
        app.shared_state = {"goal": "corrigir prompt"}
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = (
            PromptText("PROMPT GERADO", strict=False),
            {
                "rules_chars": 10,
                "session_state_chars": 20,
                "persistent_chars": 30,
                "request_chars": 40,
                "execution_state_chars": 50,
                "shared_state_chars": 60,
                "history_chars": 70,
                "delegation_chars": 0,
                "history_messages": 1,
                "total_chars": 280,
                "primary": True,
            },
        )

        app.system_layer = system_layer_from_app(app)
        handled = app.system_layer.handle_command(CMD_PROMPT)

        self.assertTrue(handled)
        app.prompt_builder.build.assert_called_once_with(
            AGENT_CLAUDE,
            app.history,
            is_first_speaker=True,
            debug=True,
            primary=True,
            shared_state=app.shared_state,
            skip_tool_prompt=True,
            execution_mode=None,
        )
        self.assertEqual(len(app.renderer.prompt_previews), 1)
        agent, content = app.renderer.prompt_previews[0]
        self.assertEqual(agent, AGENT_CLAUDE)
        self.assertIn("PROMPT PREVIEW: claude", content)
        self.assertIn("ANÁLISE DOS BLOCOS:", content)
        self.assertIn("- total_chars: 280", content)
        self.assertIn("PROMPT FINAL:\nPROMPT GERADO", content)

    def test_handle_command_shows_prompt_preview_for_exact_agent_prefix(self):
        """Verifica que handle command shows prompt preview for exact agent prefix."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._redisplay_user_prompt_if_needed = Mock()
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "idle"
        app._deferred_system_messages = []
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.history = []
        app.shared_state = {}
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = (
            PromptText("PROMPT CODex", strict=False),
            {
                "rules_chars": 1,
                "session_state_chars": 2,
                "persistent_chars": 3,
                "request_chars": 4,
                "execution_state_chars": 5,
                "shared_state_chars": 6,
                "history_chars": 7,
                "delegation_chars": 0,
                "history_messages": 0,
                "total_chars": 28,
                "primary": True,
            },
        )

        app.system_layer = system_layer_from_app(app)
        materialize_internal_services(app)
        handled = app.system_layer.handle_command("/prompt /codex")

        self.assertTrue(handled)
        app.prompt_builder.build.assert_called_once()
        self.assertEqual(app.prompt_builder.build.call_args.args[0], AGENT_CODEX)
        self.assertEqual(len(app.renderer.prompt_previews), 1)
        self.assertEqual(app.renderer.prompt_previews[0][0], AGENT_CODEX)
        self.assertIn("PROMPT PREVIEW: codex", app.renderer.prompt_previews[0][1])

    def test_prompt_preview_omits_tool_prompt_for_cli_agent_without_builtin_tools(self):
        """Verifica que prompt preview omits tool prompt for cli agent without builtin tools."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.shared_state = {}
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = (PromptText("PROMPT", strict=False), {
            "rules_chars": 1,
            "session_state_chars": 1,
            "persistent_chars": 1,
            "request_chars": 1,
            "execution_state_chars": 1,
            "shared_state_chars": 1,
            "history_chars": 1,
            "delegation_chars": 0,
            "history_messages": 0,
            "total_chars": 7,
            "primary": True,
        })
        app.get_agent_profile = Mock(return_value=ExecutionProfile(
            name="opencode",
            prefix="/opencode",
            style=("blue", "OpenCode"),
            cmd=["opencode"],
            driver="cli",
            supports_tools=True,
            has_builtin_tools=False,
        ))

        message = system_layer_from_app(app)._build_prompt_preview_message("opencode")

        self.assertIn("TOOLS NO TEXTO: não", message)
        self.assertTrue(app.prompt_builder.build.call_args.kwargs["skip_tool_prompt"])

    def test_prompt_preview_skips_tool_prompt_for_cli_agent_with_builtin_tools(self):
        """Verifica que prompt preview skips tool prompt for cli agent with builtin tools."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.shared_state = {}
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = (PromptText("PROMPT", strict=False), {
            "rules_chars": 1,
            "session_state_chars": 1,
            "persistent_chars": 1,
            "request_chars": 1,
            "execution_state_chars": 1,
            "shared_state_chars": 1,
            "history_chars": 1,
            "delegation_chars": 0,
            "history_messages": 0,
            "total_chars": 7,
            "primary": True,
        })
        app.get_agent_profile = Mock(return_value=ExecutionProfile(
            name="codex-cli",
            prefix="/codex-cli",
            style=("blue", "Codex CLI"),
            cmd=["codex"],
            driver="cli",
            supports_tools=True,
            has_builtin_tools=True,
        ))

        message = system_layer_from_app(app)._build_prompt_preview_message("codex-cli")

        self.assertIn("TOOLS NO TEXTO: não", message)
        self.assertTrue(app.prompt_builder.build.call_args.kwargs["skip_tool_prompt"])

    def test_prompt_preview_omits_tool_prompt_for_openai_compat_even_with_builtin_tools(self):
        """Verifica que prompt preview omits tool prompt for openai compat even with builtin tools."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.shared_state = {}
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = (PromptText("PROMPT", strict=False), {
            "rules_chars": 1,
            "session_state_chars": 1,
            "persistent_chars": 1,
            "request_chars": 1,
            "execution_state_chars": 1,
            "shared_state_chars": 1,
            "history_chars": 1,
            "delegation_chars": 0,
            "history_messages": 0,
            "total_chars": 7,
            "primary": True,
        })
        app.get_agent_profile = Mock(return_value=ExecutionProfile(
            name="chatgpt-api",
            prefix="/chatgpt-api",
            style=("yellow", "ChatGPT API"),
            driver="openai_compat",
            model="gpt-4o",
            base_url="http://localhost:5532/v1",
            api_key_env="OPENAI_API_KEY",
            supports_tools=True,
            has_builtin_tools=True,
        ))

        message = system_layer_from_app(app)._build_prompt_preview_message("chatgpt-api")

        self.assertIn("DRIVER: openai_compat", message)
        self.assertIn("TOOLS NO TEXTO: não", message)
        self.assertTrue(app.prompt_builder.build.call_args.kwargs["skip_tool_prompt"])

    def test_handle_command_warns_on_unknown_prompt_agent(self):
        """Verifica que handle command warns on unknown prompt agent."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.system_layer = system_layer_from_app(app)

        materialize_internal_services(app)
        handled = app.system_layer.handle_command("/prompt inexistente")

        self.assertTrue(handled)
        self.assertEqual(app.renderer.warnings, ["Uso: /prompt [agente] [follower]"])

    def test_available_internal_commands_include_prompt(self):
        """Verifica que available internal commands include prompt."""
        self.assertIn(CMD_PROMPT, QuimeraApp._available_internal_commands())

    def test_available_internal_commands_include_connect(self):
        """Verifica que available internal commands include connect."""
        self.assertIn(CMD_CONNECT, QuimeraApp._available_internal_commands())

    def test_available_internal_commands_include_disconnect(self):
        """Verifica que available internal commands include disconnect."""
        self.assertIn(CMD_DISCONNECT, QuimeraApp._available_internal_commands())

    def test_available_internal_commands_include_policy(self):
        """Verifica que available internal commands include policy."""
        self.assertIn(CMD_POLICY, QuimeraApp._available_internal_commands())

    def test_policy_command_argument_resolver_suggests_presets(self):
        """Verifica que policy command argument resolver suggests presets."""
        app = QuimeraApp.__new__(QuimeraApp)
        self.assertEqual(
            app._command_argument_resolver(CMD_POLICY, ""),
            ["status", "strict", "developer", "autonomous"],
        )

    def test_list_connected_agents_returns_sorted_names(self):
        """Verifica que list connected agents returns sorted names."""
        app = QuimeraApp.__new__(QuimeraApp)
        layer = system_layer_from_app(app)

        with patch("quimera.app.system_layer.get_connections", return_value={"codex": {}, "chatgpt": {}}) as get_overrides:
            result = layer.list_connected_agents()

        self.assertEqual(result, ["chatgpt", "codex"])
        get_overrides.assert_called_once_with()

    def test_handle_command_warns_when_connect_target_is_missing(self):
        """Verifica que handle command warns when connect target is missing."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.system_layer = system_layer_from_app(app)

        materialize_internal_services(app)
        handled = app.system_layer.handle_command(CMD_CONNECT)

        self.assertTrue(handled)
        self.assertEqual(app.renderer.warnings, ["Uso: /connect <agente> [--advanced]"])

    def test_handle_command_connects_agent_interactively(self):
        """Verifica que handle command connects agent interactively."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._redisplay_user_prompt_if_needed = Mock()
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "idle"
        app._deferred_system_messages = []
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([])
        app.active_agents = []
        profile = ExecutionProfile(
            name="chatgpt",
            prefix="/chatgpt",
            style=("green", "ChatGPT"),
            driver="openai_compat",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            supports_tools=True,
        )
        app.get_available_profiles = Mock(return_value=[profile])
        app.get_agent_profile = Mock(return_value=profile)
        answers = iter(["", "openai", "", "gpt-5.1", "http://localhost:1234/v1", "LM_STUDIO_KEY", "", "", ""])
        app.read_user_input = Mock(side_effect=lambda prompt, timeout=-1: next(answers))
        app.system_layer = system_layer_from_app(app)

        with patch("quimera.app.system_layer.set_connection") as set_override:
            materialize_internal_services(app)
            handled = app.system_layer.handle_command("/connect chatgpt")

        self.assertTrue(handled)
        set_override.assert_called_once()
        target, connection = set_override.call_args.args[:2]
        self.assertEqual(target, "chatgpt")
        self.assertIsInstance(connection, OpenAIConnection)
        self.assertEqual(connection.model, "gpt-5.1")
        self.assertEqual(connection.base_url, "http://localhost:1234/v1")
        self.assertEqual(connection.api_key_env, "LM_STUDIO_KEY")
        self.assertIn("Configurando conexão para chatgpt", app.renderer.system_messages[0])
        self.assertIn("Conexão ativa para chatgpt", app.renderer.system_messages[-1])

    def test_handle_command_warns_when_disconnect_target_is_missing(self):
        """Verifica que handle command warns when disconnect target is missing."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.system_layer = system_layer_from_app(app)

        materialize_internal_services(app)
        handled = app.system_layer.handle_command(CMD_DISCONNECT)

        self.assertTrue(handled)
        self.assertEqual(app.renderer.warnings, ["Uso: /disconnect <agente>"])

    def test_handle_command_disconnects_persisted_connection(self):
        """Verifica que handle command disconnects persisted connection."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.system_layer = system_layer_from_app(app)

        with patch("quimera.app.system_layer.remove_connection", return_value=True) as remove_conn:
            materialize_internal_services(app)
            handled = app.system_layer.handle_command("/disconnect chatgpt")

        self.assertTrue(handled)
        remove_conn.assert_called_once_with("chatgpt", registry=None)
        self.assertIn("Conexão removida para chatgpt.", app.renderer.system_messages)

    def test_handle_command_disconnect_warns_when_not_found(self):
        """Verifica que handle command disconnect warns when not found."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.system_layer = system_layer_from_app(app)

        with patch("quimera.app.system_layer.remove_connection", return_value=False) as remove_conn:
            materialize_internal_services(app)
            handled = app.system_layer.handle_command("/disconnect chatgpt")

        self.assertTrue(handled)
        remove_conn.assert_called_once_with("chatgpt", registry=None)
        self.assertEqual(
            app.renderer.warnings,
            ["Nenhuma conexão persistida encontrada para chatgpt."],
        )

    def test_configure_connection_interactively_openai_returns_dataclass_connection(self):
        """Verifica que configure connection interactively openai returns dataclass connection."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        profile = ExecutionProfile(
            name="qwen2-5",
            prefix="/qwen2-5",
            style=("cyan", "Qwen"),
            driver="openai_compat",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            supports_tools=True,
        )
        answers = iter(["", "openai", "", "qwen2.5:14b-instruct-q4_K_M", "http://localhost:11434/v1", "", "", "", ""])
        app.read_user_input = Mock(side_effect=lambda prompt, timeout=-1: next(answers))
        layer = system_layer_from_app(app)

        connection, base_name = layer._configure_connection_interactively(profile)

        self.assertIsNone(base_name)
        self.assertIsInstance(connection, OpenAIConnection)
        self.assertEqual(connection.model, "qwen2.5:14b-instruct-q4_K_M")
        self.assertEqual(connection.base_url, "http://localhost:11434/v1")
        self.assertEqual(connection.api_key_env, "OPENAI_API_KEY")
        self.assertEqual(connection.provider, "openai_compat")

    def test_clear_terminal_screen_clears_scrollback_and_repositions_cursor(self):
        """Verifica que clear terminal screen clears scrollback and repositions cursor."""
        app = QuimeraApp.__new__(QuimeraApp)

        stdout = Mock()
        stdout.isatty.return_value = True

        with patch("sys.stdout", stdout):
            QuimeraApp.clear_terminal_screen(app)

        stdout.write.assert_called_once_with("\x1b[3J\x1b[2J\x1b[H")
        stdout.flush.assert_called_once_with()

    def test_handle_task_command_creates_task_and_assigns_best_agent(self):
        """Verifica que handle task command creates task and assigns best agent."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.user_name = "Alex"
        app.shared_state = {"goal": "corrigir task runner"}
        app.current_job_id = 1
        app.history = [
            {"role": "human", "content": "o teste de task perdeu contexto"},
            {"role": "claude", "content": "precisamos serializar o chat recente"},
        ]
        app.prompt_builder = type("PromptBuilderStub", (), {"history_window": 4})()
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)
        app.task_services = build_task_services(app)
        app.system_layer = system_layer_from_app(app)
        materialize_internal_services(app)
        handled = app.system_layer.handle_command('/task "execute os testes"')

        self.assertTrue(handled)
        tasks = list_tasks({"job_id": 1}, db_path=str(db_path))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "pending")
        self.assertEqual(tasks[0]["task_type"], "test_execution")
        self.assertEqual(tasks[0]["origin"], "human_command")
        self.assertEqual(tasks[0]["assigned_to"], AGENT_CODEX)
        self.assertIn("TAREFA:\nexecute os testes", tasks[0]["body"])
        self.assertIn("CONTEXTO DA TASK (sanitizado):", tasks[0]["body"])
        self.assertIn("ALEX]: o teste de task perdeu contexto", tasks[0]["body"])
        self.assertIn("CLAUDE]: precisamos serializar o chat recente", tasks[0]["body"])
        self.assertIn('"goal": "corrigir task runner"', tasks[0]["body"])
        self.assertIn("task criada com id", app.renderer.system_messages[-1])
        self.assertIn("atribuída para codex", app.renderer.system_messages[-1])

    def test_handle_task_command_assigns_ollama_when_it_supports_task_execution(self):
        """Verifica que handle task command assigns ollama when it supports task execution."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool(["opencode"])
        app.active_agents = ["opencode"]
        app.user_name = "Alex"
        app.shared_state = {}
        app.current_job_id = 1
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)
        app.task_services = build_task_services(app)
        app.system_layer = system_layer_from_app(app)
        materialize_internal_services(app)
        handled = app.system_layer.handle_command('/task "revise o arquivo quimera/app.py"')

        self.assertTrue(handled)
        tasks = list_tasks({"job_id": 1}, db_path=str(db_path))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["assigned_to"], "opencode")
        self.assertIn("atribuída para opencode", app.renderer.system_messages[-1])

    def test_handle_task_command_uses_injected_task_classifier(self):
        """Verifica que handle task command uses injected task classifier."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.user_name = "Alex"
        app.shared_state = {}
        app.current_job_id = 1
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)

        class CustomClassifier:
            def classify(self, _description: str) -> TaskClassification:
                return TaskClassification(
                    task_type=TaskType.DOCUMENTATION,
                    complexity="low",
                    requires_tools=False,
                    requires_code_editing=False,
                    risk_level="low",
                )

        app.task_classifier = CustomClassifier()
        app.task_services = build_task_services(app)
        app.system_layer = system_layer_from_app(app)

        handled = app.system_layer.handle_command('/task "corrija o bug"')

        self.assertTrue(handled)
        tasks = list_tasks({"job_id": 1}, db_path=str(db_path))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_type"], TaskType.DOCUMENTATION)

    def test_handle_task_command_warns_and_falls_back_for_invalid_task_classifier(self):
        """Verifica que handle task command warns and falls back for invalid task classifier."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.user_name = "Alex"
        app.shared_state = {}
        app.current_job_id = 1
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)
        app.task_classifier = object()
        app.task_services = build_task_services(app)
        app.system_layer = system_layer_from_app(app)

        with patch("quimera.tasks.protocol.logger.debug") as debug:
            materialize_internal_services(app)
            handled = app.system_layer.handle_command('/task "execute os testes"')

        self.assertTrue(handled)
        debug.assert_called_once()
        tasks = list_tasks({"job_id": 1}, db_path=str(db_path))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_type"], TaskType.TEST_EXECUTION)

    def test_classify_task_execution_result_rejects_inability_text(self):
        """Verifica que classify task execution result rejects inability text."""
        ok, reason = QuimeraApp.classify_task_execution_result(
            "Não consigo executar isso sem acesso ao ambiente."
        )

        self.assertFalse(ok)
        self.assertIn("Não consigo", reason)

    def test_choose_agent_with_load_balance_penalizes_busy_higher_tier_agent(self):
        """Verifica que choose agent with load balance penalizes busy higher tier agent."""
        from quimera.tasks.api import create_task

        app = QuimeraApp.__new__(QuimeraApp)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)
        for idx in range(3):
            create_task(
                1,
                f"Tarefa {idx}",
                task_type="general",
                assigned_to=AGENT_CLAUDE,
                status="pending",
                db_path=str(db_path),
            )

        selected = build_task_services(app).choose_agent_with_load_balance("general")

        self.assertEqual(selected, AGENT_CODEX)

    def test_handle_task_command_rejects_empty_description(self):
        """Verifica que handle task command rejects empty description."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.user_name = "Alex"
        app.shared_state = {}
        app.current_job_id = 1
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)
        app.task_services = build_task_services(app)
        app.system_layer = system_layer_from_app(app)

        materialize_internal_services(app)
        handled = app.system_layer.handle_command('/task ""')

        self.assertTrue(handled)
        self.assertEqual(app.renderer.warnings, ["Uso: /task <descrição>"])
        self.assertEqual(list_tasks({"job_id": 1}, db_path=str(db_path)), [])

    def test_prompt_marks_only_first_speaker(self):
        """Verifica que prompt marks only first speaker."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        first_prompt = builder.build(AGENT_CLAUDE, history, is_first_speaker=True)
        second_prompt = builder.build(AGENT_CODEX, history, is_first_speaker=False)

        self.assertIn(EXTEND_MARKER, first_prompt)
        self.assertIn("validador", second_prompt)
        self.assertNotIn("inclua [DEBATE] ao final da sua resposta", second_prompt)

    def test_prompt_delegation_only_allows_route_for_multi_hop(self):
        """Verifica que prompt delegation only allows route for multi hop."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(
            AGENT_CODEX,
            history,
            delegation={
                "task": "Revisar parser",
                "context": "Há dúvida sobre validação",
                "expected": "1 parágrafo curto",
            },
            delegation_only=True,
        )

        self.assertIn("Você recebeu uma subtarefa delegada", prompt)
        self.assertIn("REQUEST:\nRevisar parser", prompt)
        self.assertIn("EXPECTED:\n1 parágrafo curto", prompt)
        self.assertNotIn("segundo agente nesta rodada", prompt)
        self.assertIn("tool estruturada `delegate`", prompt)
        self.assertIn("target_agent", prompt)
        self.assertNotIn("Não delegue de volta", prompt)

    def test_prompt_includes_delegation_when_present(self):
        """Verifica que prompt includes delegation when present."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CODEX, history, delegation="Revise este ponto.")

        self.assertIn('<delegation title="Mensagem direta do outro agente">', prompt)
        self.assertIn("</delegation>", prompt)
        self.assertIn("Revise este ponto.", prompt)

    def test_prompt_includes_current_human_request_block(self):
        """Verifica que prompt includes current human request block."""
        builder = PromptBuilder(DummyContextManager(), history_window=4)
        history = [
            {"role": "human", "content": "Primeiro pedido"},
            {"role": "claude", "content": "Resposta anterior"},
            {"role": "human", "content": "Pedido atual"},
        ]

        prompt = builder.build(AGENT_CODEX, history)

        self.assertIn('<current_turn title="Pedido atual de >>>">', prompt)
        self.assertIn("</current_turn>", prompt)
        self.assertIn("Pedido atual", prompt)

    def test_prompt_does_not_repeat_current_human_request_in_conversation(self):
        """Verifica que prompt does not repeat current human request in conversation."""
        builder = PromptBuilder(DummyContextManager(), history_window=4)
        history = [
            {"role": "human", "content": "Primeiro pedido"},
            {"role": "claude", "content": "Resposta anterior"},
            {"role": "human", "content": "Pedido atual"},
        ]

        prompt = builder.build(AGENT_CODEX, history)

        conversation = prompt.split('<recent_conversation title="Conversa recente">\n', 1)[1]
        self.assertNotIn("[>>>]: Pedido atual", conversation)
        self.assertIn("[>>>]: Primeiro pedido", conversation)
        self.assertIn("</recent_conversation>", conversation)

    def test_prompt_keeps_agent_messages_in_recent_conversation(self):
        """Verifica que prompt keeps agent messages in recent conversation."""
        # Mensagens de outros agentes aparecem em recent_conversation (ordem
        # canônica). O bloco auxiliar não deve renderizar para não duplicar.
        builder = PromptBuilder(DummyContextManager(), history_window=5)
        history = [
            {"role": "human", "content": "Investigue"},
            {"role": "claude", "content": "Arquivo alterado: app.py"},
            {"role": "codex", "content": "Teste falhou em test_x"},
        ]

        prompt = builder.build(AGENT_CLAUDE, history)

        # Mensagem de outro agente deve estar na conversa recente canônica
        self.assertIn("[CODEX]: Teste falhou em test_x", prompt)
        self.assertIn("<recent_conversation", prompt)
        # Bloco redundante não deve aparecer (mensagem já está em recent_conversation)
        self.assertNotIn("<recent_agent_messages", prompt)

    def test_prompt_skips_meta_lock_messages_from_recent_conversation(self):
        """Verifica que prompt skips meta lock messages from recent conversation."""
        builder = PromptBuilder(DummyContextManager(), history_window=5)
        history = [
            {"role": "human", "content": "Mude o foco"},
            {"role": "codex", "content": "goal_canonical continua ativo e não redefina o objetivo"},
            {"role": "claude", "content": "Arquivo alterado: app.py"},
        ]

        prompt = builder.build(AGENT_CLAUDE, history)

        self.assertNotIn('<recent_agent_messages title=', prompt)
        self.assertNotIn("[CLAUDE]: Arquivo alterado: app.py", prompt)
        self.assertNotIn("goal_canonical continua ativo", prompt)
        self.assertNotIn("não redefina o objetivo", prompt)
        conversation = prompt.split('<recent_conversation title="Conversa recente">\n', 1)[1]
        self.assertIn("[sem itens residuais na conversa recente]", conversation)

    def test_prompt_keeps_same_agent_history_in_conversation_not_other_agents_block(self):
        """Verifica que prompt keeps same agent history in conversation not other agents block."""
        builder = PromptBuilder(DummyContextManager(), history_window=5)
        history = [
            {"role": "claude", "content": "Eu estava investigando o parser"},
            {"role": "human", "content": "Continue dessa linha"},
        ]

        prompt = builder.build(AGENT_CLAUDE, history)

        self.assertNotIn('<recent_agent_messages title=', prompt)
        conversation = prompt.split('<recent_conversation title="Conversa recente">\n', 1)[1]
        self.assertIn("[CLAUDE]: Eu estava investigando o parser", conversation)

    def test_prompt_keeps_latest_same_agent_message_even_if_just_outside_window(self):
        """Verifica que prompt keeps latest same agent message even if just outside window."""
        builder = PromptBuilder(DummyContextManager(), history_window=4)
        history = [
            {"role": "claude", "content": "Eu já tinha isolado a causa no parser."},
            {"role": "human", "content": "Pedido antigo"},
            {"role": "codex", "content": "Fato 1"},
            {"role": "chatgpt", "content": "Fato 2"},
            {"role": "human", "content": "Continue dessa linha"},
        ]

        prompt = builder.build(AGENT_CLAUDE, history)

        conversation = prompt.split('<recent_conversation title="Conversa recente">\n', 1)[1]
        self.assertIn("[CLAUDE]: Eu já tinha isolado a causa no parser.", conversation)

    def test_prompt_keeps_recent_messages_in_conversation_for_continuity(self):
        """Verifica que prompt keeps recent messages in conversation for continuity."""
        builder = PromptBuilder(DummyContextManager(), history_window=5)
        history = [
            {"role": "human", "content": "Investigue"},
            {"role": "claude", "content": "Arquivo alterado: app.py"},
            {"role": "codex", "content": "Teste falhou em test_x"},
        ]

        prompt = builder.build(AGENT_CLAUDE, history)

        conversation = prompt.split('<recent_conversation title="Conversa recente">\n', 1)[1]
        self.assertIn("[CODEX]: Teste falhou em test_x", conversation)
        self.assertNotIn("[sem itens residuais na conversa recente]", conversation)

    def test_prompt_lists_only_active_agents(self):
        """Verifica que prompt lists only active agents."""
        builder = PromptBuilder(
            DummyContextManager(),
            history_window=3,
            active_agents=[AGENT_CLAUDE, AGENT_CODEX],
        )
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CODEX, history)

        self.assertIn("CLAUDE", prompt)
        # CODEX é o agente falante — não aparece na lista de outros agentes
        self.assertNotIn("QWEN", prompt)

    def test_prompt_includes_session_state_when_present(self):
        """Verifica que prompt includes session state when present."""
        builder = PromptBuilder(
            DummyContextManager(),
            history_window=3,
            session_state={
                "session_id": "sessao-2026-03-27-123456",
                "current_job_id": 1,
                "is_new_session": "não",
                "history_restored": "sim",
                "summary_loaded": "não",
                "workspace_root": "/tmp/quimera",
                "current_dir": ".",
                "os_info": "Linux 6.17.0-22-generic",
            },
        )
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CLAUDE, history)

        self.assertIn('<session_state title="Estado da sessão">', prompt)
        self.assertIn("</session_state>", prompt)
        self.assertIn("SESSÃO ATUAL: sessao-2026-03-27-123456", prompt)
        self.assertIn("JOB_ID ATUAL: 1", prompt)
        self.assertIn("WORKSPACE RAIZ: /tmp/quimera", prompt)
        self.assertIn("SISTEMA OPERACIONAL: Linux 6.17.0-22-generic", prompt)
        self.assertNotIn("NOVA SESSÃO", prompt)
        self.assertNotIn("HISTÓRICO RESTAURADO", prompt)
        self.assertNotIn("RESUMO CARREGADO", prompt)

    def test_prompt_includes_shared_state_as_json(self):
        """Verifica que prompt includes shared state as json."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        # Chaves legadas (goal, decisions) sem goal_canonical não devem aparecer no prompt
        prompt = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={"goal": "corrigir", "decisions": ["usar json"]},
        )

        self.assertNotIn('<shared_state title="Estado compartilhado">', prompt)
        self.assertNotIn('"goal": "corrigir"', prompt)
        self.assertNotIn('"decisions": [', prompt)

        # Chaves de execução (next_step, etc.) sem goal_canonical também não devem aparecer
        prompt2 = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={"next_step": "continuar", "goal": "ignorado"},
        )
        self.assertNotIn('<shared_state title="Estado compartilhado">', prompt2)

        # task_overview é campo de infra (não execução) e deve aparecer normalmente
        prompt3 = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={"task_overview": {"job_id": 1}, "goal": "ignorado"},
        )
        self.assertIn('<shared_state title="Estado compartilhado">', prompt3)
        self.assertIn("</shared_state>", prompt3)
        self.assertIn('"task_overview"', prompt3)
        self.assertNotIn('"goal":', prompt3)

    def test_prompt_truncates_shared_state_to_last_five_decisions(self):
        """Verifica que prompt truncates shared state to last five decisions."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]
        big_state = {
            "goal": "objetivo",
            "next_step": "próximo passo",
            "decisions": [f"d{i}" for i in range(10)],
            "open_disagreements": ["x" * 200],
        }

        prompt = builder.build(AGENT_CLAUDE, history, shared_state=big_state)

        # Sem goal_canonical, todos os campos de execução (goal, decisions, next_step) são filtrados
        self.assertNotIn('<shared_state title="Estado compartilhado">', prompt)
        self.assertNotIn('"goal":', prompt)
        self.assertNotIn('"decisions":', prompt)
        self.assertNotIn('"next_step":', prompt)
        self.assertNotIn('"open_disagreements"', prompt)

        # Com task_overview (campo de infra), o bloco aparece normalmente
        state_with_overview = {**big_state, "task_overview": {"job_id": 42}}
        prompt2 = builder.build(AGENT_CLAUDE, history, shared_state=state_with_overview)
        state_start = prompt2.index('<shared_state title="Estado compartilhado">')
        state_block = prompt2[state_start:]
        self.assertIn("</shared_state>", state_block)
        self.assertIn('"task_overview"', state_block)
        self.assertNotIn('"goal":', state_block)
        self.assertNotIn('"next_step":', state_block)

    def test_prompt_includes_task_overview_in_shared_state(self):
        """Verifica que prompt includes task overview in shared state."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={
                "goal": "coordenar tarefas",
                "task_overview": {
                    "job_id": 23,
                    "open_task_counts": {"approved": 1, "proposed": 0, "in_progress": 0},
                    "recommended_action": "Execute approved antes de criar novas.",
                },
            },
        )

        self.assertIn('"task_overview": {', prompt)
        self.assertIn('"job_id": 23', prompt)
        self.assertIn('Execute approved antes de criar novas.', prompt)

    def test_prompt_omits_state_update_block_documented_by_mcp_tool(self):
        """Verifica que prompt omits state update block documented by MCP tool."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        # next_step é campo de execução legado e não deve acionar o bloco visual
        prompt = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={"next_step": "continuar"},
        )
        self.assertNotIn('<shared_state title="Estado compartilhado">', prompt)

        # task_overview (campo de infra) deve acionar o bloco visível de shared_state
        prompt2 = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={"task_overview": {"job_id": 1}},
        )
        self.assertIn('<shared_state title="Estado compartilhado">', prompt2)
        # O bloco explicativo de update_shared_state foi removido do prompt:
        # a própria descrição da ferramenta MCP documenta uso e campos suportados.
        self.assertNotIn("Você pode atualizar o estado compartilhado usando a tool `update_shared_state`", prompt2)
        self.assertNotIn("update_shared_state", prompt2)

    def test_prompt_keeps_internal_shared_state_keys_out_of_visible_blocks(self):
        """Verifica que prompt keeps internal shared state keys out of visible blocks."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={
                "goal_canonical": "não deve aparecer no bloco visível",
                "task_overview": {"job_id": 33},
                "working_dir": "/tmp/worktree",
                "workspace_root": "/tmp/worktree",
                "completed_task_results": "[task 1] ok",
                "spy_last_turn_detail": {"agent": "codex"},
            },
        )

        shared_state_block = _extract_block(prompt, "shared_state")
        completed_block = _extract_block(prompt, "completed_tasks")

        self.assertIn('"task_overview"', shared_state_block)
        self.assertIn('"working_dir": "/tmp/worktree"', shared_state_block)
        self.assertIn('"workspace_root": "/tmp/worktree"', shared_state_block)
        self.assertNotIn("goal_canonical", shared_state_block)
        self.assertNotIn("spy_last_turn_detail", shared_state_block)
        self.assertNotIn("completed_task_results", shared_state_block)
        self.assertEqual(completed_block.strip(), "[task 1] ok")

    def test_app_builds_explicit_session_state_for_prompt(self):
        """Verifica que app builds explicit session state for prompt."""
        temp_root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        class FakeTmp:
            root = temp_root
            logs_dir = temp_root / "logs"

            def render_log_path_for(self, session_id):
                return temp_root / f"render-{session_id}.jsonl"

            def render_ansi_path_for(self, session_id):
                return temp_root / f"render-{session_id}.ansi"

            def metrics_path_for(self, session_id):
                return temp_root / f"metrics-{session_id}.jsonl"

        class FakeWorkspace:
            def __init__(self, cwd):
                self.root = temp_root
                self.cwd = cwd
                self.config_file = temp_root / "config.json"
                self.context_persistent = temp_root / "quimera_context.md"
                self.context_session = temp_root / "quimera_session_context.md"
                self.logs_dir = temp_root / "quimera_logs"
                self.state_dir = temp_root / "quimera_state"
                self.tasks_db = temp_root / "quimera_tasks.db"
                self.decisions_log = temp_root / "decisions.jsonl"
                self.env_file = temp_root / ".env"
                self.tmp = FakeTmp()

            def history_file_for(self, session_id):
                return temp_root / f"quimera_history-{session_id}.jsonl"

            def migrate_from_legacy(self, cwd):
                return []

        class FakeContextManager:
            SUMMARY_MARKER = "## Resumo da última sessão"

            def __init__(self, *_args, **_kwargs):
                pass

            def load_session(self):
                return "## Resumo da última sessão\n\nResumo anterior"

        class FakeSessionStorage:
            session_id = "sessao-2026-03-27-123456"

            def __init__(self, *_args, **_kwargs):
                pass

            def load_last_session(self):
                return {
                    "messages": [{"role": "human", "content": "oi"}],
                    "shared_state": {"goal": "continuar"},
                }

            def get_history_file(self):
                return Path("/tmp/sessao-2026-03-27-123456.json")

        with patch("quimera.app.bootstrap.wiring.ConfigManager", DummyConfigManager), patch("quimera.app.bootstrap.wiring.Workspace",
                                                                                FakeWorkspace), patch(
                "quimera.app.bootstrap.wiring.ContextManager", FakeContextManager
        ), patch("quimera.app.bootstrap.wiring.SessionStorage", FakeSessionStorage):
            app = QuimeraApp(Path("/tmp/projeto"), input_gate_factory=lambda **kw: MagicMock())

        try:
            session_state = app.prompt_builder.session_state
            self.assertEqual(session_state.get("session_id"), "sessao-2026-03-27-123456")
            self.assertEqual(session_state.get("is_new_session"), "não")
            self.assertEqual(session_state.get("history_restored"), "sim")
            self.assertEqual(session_state.get("summary_loaded"), "sim")
            self.assertIn("current_job_id", session_state)
            self.assertRegex(session_state.get("os_info", ""), r"^\S.+\S$")
            self.assertIs(session_state.get("mcp_enabled"), False)
            self.assertEqual(session_state.get("mcp_socket_path"), "")
            self.assertIs(app.input_services._output_lock, app._output_lock)
        finally:
            app._stop_task_executors()
        self.assertIsInstance(session_state["current_job_id"], int)
        self.assertEqual(app.shared_state, {})

    def test_app_uses_default_history_window_from_config(self):
        """Verifica que app uses default history window from config."""
        temp_root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        class FakeTmp:
            root = temp_root
            logs_dir = temp_root / "logs"

            def render_log_path_for(self, session_id):
                return temp_root / f"render-{session_id}.jsonl"

            def render_ansi_path_for(self, session_id):
                return temp_root / f"render-{session_id}.ansi"

            def metrics_path_for(self, session_id):
                return temp_root / f"metrics-{session_id}.jsonl"

        class FakeWorkspace:
            def __init__(self, cwd):
                self.root = temp_root
                self.cwd = cwd
                self.config_file = temp_root / "config.json"
                self.context_persistent = temp_root / "quimera_context.md"
                self.context_session = temp_root / "quimera_session_context.md"
                self.logs_dir = temp_root / "quimera_logs"
                self.state_dir = temp_root / "quimera_state"
                self.tasks_db = temp_root / "quimera_tasks.db"
                self.decisions_log = temp_root / "decisions.jsonl"
                self.env_file = temp_root / ".env"
                self.tmp = FakeTmp()

            def history_file_for(self, session_id):
                return temp_root / f"quimera_history-{session_id}.jsonl"

            def migrate_from_legacy(self, cwd):
                return []

        class FakeContextManager:
            SUMMARY_MARKER = "## Resumo da última sessão"

            def __init__(self, *_args, **_kwargs):
                pass

            def load_session(self):
                return ""

        class FakeSessionStorage:
            session_id = "sessao-2026-03-27-123456"

            def __init__(self, *_args, **_kwargs):
                pass

            def load_last_session(self):
                return {"messages": [], "shared_state": {}}

            def get_history_file(self):
                return Path("/tmp/sessao-2026-03-27-123456.json")

        with patch("quimera.app.bootstrap.wiring.ConfigManager", DummyConfigManager), patch("quimera.app.bootstrap.wiring.Workspace",
                                                                                FakeWorkspace), patch(
                "quimera.app.bootstrap.wiring.ContextManager", FakeContextManager
        ), patch("quimera.app.bootstrap.wiring.SessionStorage", FakeSessionStorage):
            app = QuimeraApp(Path("/tmp/projeto"), input_gate_factory=lambda **kw: MagicMock())

        try:
            self.assertEqual(app.prompt_builder.history_window, DEFAULT_HISTORY_WINDOW)
        finally:
            app._stop_task_executors()

    def test_app_allows_history_window_override(self):
        """Verifica que app allows history window override."""
        temp_root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        class FakeTmp:
            root = temp_root
            logs_dir = temp_root / "logs"

            def render_log_path_for(self, session_id):
                return temp_root / f"render-{session_id}.jsonl"

            def render_ansi_path_for(self, session_id):
                return temp_root / f"render-{session_id}.ansi"

            def metrics_path_for(self, session_id):
                return temp_root / f"metrics-{session_id}.jsonl"

        class FakeWorkspace:
            def __init__(self, cwd):
                self.root = temp_root
                self.cwd = cwd
                self.config_file = temp_root / "config.json"
                self.context_persistent = temp_root / "quimera_context.md"
                self.context_session = temp_root / "quimera_session_context.md"
                self.logs_dir = temp_root / "quimera_logs"
                self.state_dir = temp_root / "quimera_state"
                self.tasks_db = temp_root / "quimera_tasks.db"
                self.decisions_log = temp_root / "decisions.jsonl"
                self.env_file = temp_root / ".env"
                self.tmp = FakeTmp()

            def history_file_for(self, session_id):
                return temp_root / f"quimera_history-{session_id}.jsonl"

            def migrate_from_legacy(self, cwd):
                return []

        class FakeContextManager:
            SUMMARY_MARKER = "## Resumo da última sessão"

            def __init__(self, *_args, **_kwargs):
                pass

            def load_session(self):
                return ""

        class FakeSessionStorage:
            session_id = "sessao-2026-03-27-123456"

            def __init__(self, *_args, **_kwargs):
                pass

            def load_last_session(self):
                return {"messages": [], "shared_state": {}}

            def get_history_file(self):
                return Path("/tmp/sessao-2026-03-27-123456.json")

        with patch("quimera.app.bootstrap.wiring.ConfigManager", DummyConfigManager), patch("quimera.app.bootstrap.wiring.Workspace",
                                                                                FakeWorkspace), patch(
                "quimera.app.bootstrap.wiring.ContextManager", FakeContextManager
        ), patch("quimera.app.bootstrap.wiring.SessionStorage", FakeSessionStorage):
            app = QuimeraApp(Path("/tmp/projeto"), history_window=5, input_gate_factory=lambda **kw: MagicMock())

        try:
            self.assertEqual(app.prompt_builder.history_window, 5)
        finally:
            app._stop_task_executors()

    def test_app_truncates_restored_history_to_hard_limit(self):
        """Verifica que app truncates restored history to hard limit."""
        temp_root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        class FakeTmp:
            root = temp_root
            logs_dir = temp_root / "logs"

            def render_log_path_for(self, session_id):
                return temp_root / f"render-{session_id}.jsonl"

            def render_ansi_path_for(self, session_id):
                return temp_root / f"render-{session_id}.ansi"

            def metrics_path_for(self, session_id):
                return temp_root / f"metrics-{session_id}.jsonl"

        class FakeWorkspace:
            def __init__(self, cwd):
                self.root = temp_root
                self.cwd = cwd
                self.config_file = temp_root / "config.json"
                self.context_persistent = temp_root / "quimera_context.md"
                self.context_session = temp_root / "quimera_session_context.md"
                self.logs_dir = temp_root / "quimera_logs"
                self.state_dir = temp_root / "quimera_state"
                self.tasks_db = temp_root / "quimera_tasks.db"
                self.decisions_log = temp_root / "decisions.jsonl"
                self.env_file = temp_root / ".env"
                self.tmp = FakeTmp()

            def history_file_for(self, session_id):
                return temp_root / f"quimera_history-{session_id}.jsonl"

            def migrate_from_legacy(self, cwd):
                return []

        class FakeContextManager:
            SUMMARY_MARKER = "## Resumo da última sessão"

            def __init__(self, *_args, **_kwargs):
                pass

            def load_session(self):
                return ""

        class FakeSessionStorage:
            session_id = "sessao-2026-03-27-123456"

            def __init__(self, *_args, **_kwargs):
                pass

            def load_last_session(self):
                return {
                    "messages": [{"role": "human", "content": f"m{i}"} for i in range(80)],
                    "shared_state": {},
                }

            def get_history_file(self):
                return Path("/tmp/sessao-2026-03-27-123456.json")

        with patch("quimera.app.bootstrap.wiring.ConfigManager", DummyConfigManager), patch("quimera.app.bootstrap.wiring.Workspace",
                                                                                FakeWorkspace), patch(
                "quimera.app.bootstrap.wiring.ContextManager", FakeContextManager
        ), patch("quimera.app.bootstrap.wiring.SessionStorage", FakeSessionStorage):
            app = QuimeraApp(Path("/tmp/projeto"), input_gate_factory=lambda **kw: MagicMock())

        try:
            self.assertEqual(len(app.history), 60)
            self.assertEqual(app.history[0]["content"], "m20")
            self.assertEqual(app.history[-1]["content"], "m79")
            self.assertEqual(app.session_state["history_count"], 60)
        finally:
            app._stop_task_executors()

    def test_run_uses_single_turn_by_default(self):
        """No fluxo padrão (sem prefixo explícito, sem EXTEND), apenas um agente responde."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.user_name = "Você"
        app.round_index = 0
        app.session_call_index = 0
        app.debug_prompt_metrics = False
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = DummyAgentClient()
        app.prompt_builder = None
        app.session_state = {
            "session_id": "sessao-2026-03-27-123456",
            "history_count": 0,
            "summary_loaded": False,
        }
        app.session_state_mgr = Mock()
        persisted = []
        printed = []

        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE])
        app.active_agents = [AGENT_CLAUDE]
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.bug_services = Mock()
        app._ui_event_handler = Mock()
        app.threads = 1
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.shared_state = {}
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content, **kwargs: persisted.append((role, content))
        app.session_services.maybe_auto_summarize = Mock()
        app.system_layer = Mock()
        app.system_layer.handle_command = Mock(return_value=False)
        app.protocol = _make_protocol(app)
        app.dispatch_services = Mock(spec=AppDispatchServices)
        app.dispatch_services.delegate = Mock(return_value="claude responde")
        app.dispatch_services.print_response = lambda agent, response: printed.append((agent, response))
        app.input_services = Mock()
        app.input_services.read_user_input = Mock(side_effect=["mensagem", "/exit"])
        app.turn_manager = TurnManager()
        app.chat_round_orchestrator = chat_round_orchestrator_from_app(app)
        _materialize_chat_lifecycle(app)

        app.run()

        # Apenas o agente roteado responde — outros agentes não são acionados
        self.assertEqual(printed, [(AGENT_CLAUDE, "claude responde")])
        self.assertEqual(
            persisted,
            [("human", "oi"), (AGENT_CLAUDE, "claude responde")],
        )
        app.dispatch_services.delegate.assert_called_once()

    def test_run_flushes_startup_system_messages_before_first_prompt(self):
        """Verifica que run flushes startup system messages before first prompt."""
        class RecordingRenderer(DummyRenderer):
            def __init__(self):
                super().__init__()
                self.events = []

            def show_system(self, message):
                self.events.append(("show_system", message))

            def flush(self):
                self.events.append(("flush", None))

        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.user_name = "Alex"
        app.execution_mode = None
        app.renderer = RecordingRenderer()
        app.storage = DummyStorage()
        app.session_state = {
            "session_id": "sessao-2026-03-27-123456",
            "history_count": 0,
            "summary_loaded": False,
        }
        app.agent_client = DummyAgentClient()
        app.threads = 1
        app.read_user_input = Mock(side_effect=["/exit"])
        app.session_services = Mock()

        materialize_internal_services(app)
        app.run()

        self.assertEqual(app.renderer.events[0][0], "show_system")
        self.assertEqual(app.renderer.events[1][0], "show_system")
        self.assertEqual(app.renderer.events[2][0], "show_system")
        self.assertEqual(app.renderer.events[3][0], "show_system")
        self.assertEqual(app.renderer.events[4], ("flush", None))

    def test_run_shows_render_audit_path_only_in_debug_mode(self):
        """Verifica que run shows render audit path only in debug mode."""
        class RecordingRenderer(DummyRenderer):
            def __init__(self):
                super().__init__()
                self.events = []

            def show_system(self, message):
                self.events.append(message)

        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.user_name = "Alex"
        app.execution_mode = None
        app.renderer = RecordingRenderer()
        app.storage = SimpleNamespace(
            session_id="sessao-2026-03-27-123456",
            get_log_file=lambda: Path("/home/alex/.local/share/quimera/workspaces/abc/data/logs/sessions/sessao-2026-03-27-123456.jsonl"),
            pop_restore_notice=lambda: None,
        )
        app.workspace = SimpleNamespace(
            tmp=SimpleNamespace(
                render_log_path_for=lambda _session_id: Path("/tmp/quimera/render-sessao-2026-03-27-123456.jsonl")
            )
        )
        app.session_state = {
            "session_id": "sessao-2026-03-27-123456",
            "history_count": 0,
            "summary_loaded": False,
        }
        app.agent_client = DummyAgentClient()
        app.threads = 1
        app.read_user_input = Mock(side_effect=["/exit"])
        app.session_services = Mock()
        app.debug_prompt_metrics = False

        materialize_internal_services(app)
        app.run()

        self.assertFalse(any("Log da sessão:" in msg for msg in app.renderer.events))
        self.assertFalse(any("/tmp/quimera/" in msg for msg in app.renderer.events))

        app.renderer.events.clear()
        app.read_user_input = Mock(side_effect=["/exit"])
        app.debug_prompt_metrics = True

        app.run()

        self.assertTrue(any("Log da sessão:" in msg for msg in app.renderer.events))
        self.assertTrue(any("sessao-2026-03-27-123456.jsonl" in msg for msg in app.renderer.events))
        self.assertTrue(any("/tmp/quimera/" in msg for msg in app.renderer.events))

    def test_run_keyboard_interrupt_renders_shutdown_with_muted_style(self):
        """Verifica que run keyboard interrupt renders shutdown with muted style."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.user_name = "Alex"
        app.execution_mode = None
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.session_state = {
            "session_id": "sessao-2026-03-27-123456",
            "history_count": 0,
            "summary_loaded": False,
        }
        app.agent_client = DummyAgentClient()
        app.threads = 1
        app.read_user_input = Mock(side_effect=KeyboardInterrupt)
        app.session_services = Mock()

        materialize_internal_services(app)
        app.run()

        self.assertEqual(app.renderer.system_messages[-1], MSG_SHUTDOWN)
        self.assertTrue(app.agent_client._user_cancelled)
        self.assertTrue(app.agent_client._cancel_event.is_set())

    def test_run_forwards_full_multiline_editor_content_without_truncation(self):
        """Verifica que run forwards full multiline editor content without truncation."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.user_name = "Alex"
        app.execution_mode = None
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.session_state = {
            "session_id": "sessao-2026-03-27-123456",
            "history_count": 0,
            "summary_loaded": False,
        }
        app.session_state_mgr = Mock()
        app.agent_client = DummyAgentClient()
        app.threads = 1
        app.read_user_input = Mock(side_effect=["/edit", "/exit"])
        app.input_services = Mock()
        edited_message = (
            "\"\"\"Serviços de sessão, persistência e sumarização.\"\"\"\n"
            "import sys\n"
            "import threading\n"
            "import time"
        )
        app.input_services.read_from_editor = Mock(return_value=edited_message)
        app.session_services = Mock()
        app.handle_command = Mock(return_value=False)
        captured_messages = []
        app._process_chat_message = lambda message: captured_messages.append(message)

        materialize_internal_services(app)
        app.run()

        self.assertEqual(captured_messages, [edited_message])

    def test_format_session_log_message_compacts_home_path(self):
        """Verifica que format session log message compacts home path."""
        app = QuimeraApp.__new__(QuimeraApp)
        long_path = Path.home() / "um" / "caminho" / ("muito-longo-" * 12) / "sessao-2026-04-30.txt"

        message = app._format_session_log_message(long_path)
        lines = message.splitlines()

        self.assertGreaterEqual(len(lines), 2)
        self.assertEqual(lines[0], "Log da sessão:")
        self.assertTrue(lines[1].startswith("  ~"))
        self.assertIn("...", lines[1])
        self.assertLessEqual(len(lines[1].strip()), app._SESSION_LOG_DISPLAY_MAX_CHARS)

    def test_resolve_session_log_path_does_not_fallback_to_render_tmp(self):
        """Verifica que resolve session log path does not fallback to render tmp."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.storage = SimpleNamespace(get_log_file=lambda: "", session_id="sessao-2026-03-27-123456")
        app.workspace = SimpleNamespace(
            logs_dir=Path("/home/alex/.local/share/quimera/workspaces/abc/data/logs/sessions"),
            tmp=SimpleNamespace(render_log_path_for=lambda _session_id: Path("/tmp/quimera/render.jsonl")),
        )

        log_path = resolve_session_log_path(app.storage, app.workspace)

        self.assertEqual(
            log_path,
            Path("/home/alex/.local/share/quimera/workspaces/abc/data/logs/sessions/sessao-2026-03-27-123456.jsonl"),
        )

    def test_resolve_render_debug_log_path_only_when_debug_active(self):
        """Verifica que resolve render debug log path only when debug active."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.storage = SimpleNamespace(session_id="sessao-2026-03-27-123456")
        app.workspace = SimpleNamespace(
            tmp=SimpleNamespace(
                render_log_path_for=lambda session_id: Path(f"/tmp/quimera/render-{session_id}.jsonl")
            )
        )
        app.debug_prompt_metrics = False

        self.assertEqual(resolve_render_debug_log_path(app.storage, app.workspace, app.debug_prompt_metrics), "")

        app.debug_prompt_metrics = True
        self.assertEqual(
            resolve_render_debug_log_path(app.storage, app.workspace, app.debug_prompt_metrics),
            Path("/tmp/quimera/render-sessao-2026-03-27-123456.jsonl"),
        )

    def test_resolve_render_debug_log_path_prefers_workspace_tmp_path(self):
        """Verifica que resolve render debug log path prefers workspace tmp path."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.storage = SimpleNamespace(session_id="sessao-2026-03-27-123456")
        app.workspace = SimpleNamespace(
            render_log_path_for=lambda session_id: Path(
                f"/home/alex/.local/share/quimera/workspaces/abc/data/logs/render/render-{session_id}.jsonl"
            ),
            tmp=SimpleNamespace(
                render_log_path_for=lambda session_id: Path(f"/tmp/quimera/render-{session_id}.jsonl")
            ),
        )
        app.debug_prompt_metrics = True

        self.assertEqual(
            resolve_render_debug_log_path(app.storage, app.workspace, app.debug_prompt_metrics),
            Path("/tmp/quimera/render-sessao-2026-03-27-123456.jsonl"),
        )

    def test_resolve_render_debug_log_path_returns_empty_when_getters_missing(self):
        """Verifica que resolve render debug log path returns empty when getters missing."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.storage = SimpleNamespace(session_id="sessao-2026-03-27-123456")
        app.workspace = SimpleNamespace(
            tmp=SimpleNamespace(),
        )
        app.debug_prompt_metrics = True

        self.assertEqual(resolve_render_debug_log_path(app.storage, app.workspace, app.debug_prompt_metrics), "")

    def test_resolve_render_debug_log_path_ignores_dot_path(self):
        """Verifica que resolve render debug log path ignores dot path."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.storage = SimpleNamespace(session_id="sessao-2026-03-27-123456")
        app.workspace = SimpleNamespace(
            render_log_path_for=lambda _session_id: Path("."),
            tmp=SimpleNamespace(
                render_log_path_for=lambda session_id: Path(f"/tmp/quimera/render-{session_id}.jsonl")
            ),
        )
        app.debug_prompt_metrics = True

        self.assertEqual(
            resolve_render_debug_log_path(app.storage, app.workspace, app.debug_prompt_metrics),
            Path("/tmp/quimera/render-sessao-2026-03-27-123456.jsonl"),
        )

    def test_run_uses_four_turns_when_extended(self):
        """Verifica que run uses four turns when extended."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.user_name = "Você"
        app.round_index = 0
        app.session_call_index = 0
        app.debug_prompt_metrics = False
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = DummyAgentClient()
        app.prompt_builder = None
        app.session_state = {
            "session_id": "sessao-2026-03-27-123456",
            "history_count": 0,
            "summary_loaded": False,
        }
        persisted = []
        printed = []

        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.threads = 1
        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.shared_state = {}
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content, **kwargs: persisted.append((role, content))

        materialize_internal_services(app)

        app.read_user_input = Mock(side_effect=["mensagem", "/exit"])
        app.dispatch_services.print_response = lambda agent, response: printed.append((agent, response))
        responses = iter(
            [
                "claude abre [DEBATE]",
                "codex comenta",
                "claude aprofunda",
                "codex fecha",
            ]
        )
        app.dispatch_services.delegate = lambda agent, is_first_speaker=False, delegation=None, primary=True, protocol_mode="standard", **kwargs: next(responses)

        app.run()

        self.assertEqual(
            printed,
            [
                ("claude", "claude abre"),
                ("codex", "codex comenta"),
                ("claude", "claude aprofunda"),
                ("codex", "codex fecha"),
            ],
        )
        self.assertEqual(
            persisted,
            [
                ("human", "oi"),
                (AGENT_CLAUDE, "claude abre"),
                (AGENT_CODEX, "codex comenta"),
                (AGENT_CLAUDE, "claude aprofunda"),
                (AGENT_CODEX, "codex fecha"),
            ],
        )

    def test_run_blocks_agent_task_creation_via_tool_in_normal_flow(self):
        """Verifica que run blocks agent task creation via tool in normal flow."""
        class AutoApprove(ApprovalHandler):
            def approve(self, tool_name, summary):
                return True

        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.history = []
        app.user_name = "Você"
        app.round_index = 0
        app.session_call_index = 0
        app.debug_prompt_metrics = False
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = DummyAgentClient()
        app.prompt_builder = None
        app.shared_state = {}
        app._lock = threading.Lock()
        app.session_state_mgr = Mock()
        app.session_state = {
            "session_id": "sessao-2026-04-02-183323",
            "history_count": 0,
            "summary_loaded": True,
            "delegations_sent": 0,
            "delegations_received": 0,
            "delegations_succeeded": 0,
            "delegations_failed": 0,
            "total_latency": 0.0,
            "agent_metrics": {},
        }

        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        job_id = add_job("Session sessao-2026-04-02-183323", db_path=str(db_path))
        app.current_job_id = job_id
        app.tasks_db_path = str(db_path)
        app.tool_executor = ToolExecutor(
            config=ToolRuntimeConfig(
                workspace_root=tmp_dir,
                db_path=str(db_path),
                require_approval_for_mutations=False,
            ),
            approval_handler=AutoApprove(),
        )

        persisted = []
        printed = []
        calls = []

        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, "opencode-qwen"])
        app.active_agents = [AGENT_CLAUDE, "opencode-qwen"]
        app.threads = 1
        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "faça o teste pelo chat", True)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.resolve_agent_response = QuimeraApp.resolve_agent_response.__get__(app, QuimeraApp)
        app.task_services = Mock()
        app.task_services.refresh_task_shared_state = Mock()
        app.task_services.truncate_payload = lambda payload: payload
        app.dispatch_services.print_response = lambda agent, response: printed.append((agent, response))
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content, **kwargs: persisted.append((role, content))
        app.read_user_input = Mock(side_effect=["mensagem", "/exit"])

        responses = iter(
            [
                "vou tentar abrir task sozinho\n"
                '<tool function="propose_task" description="rode os testes" />',
                "sem task criada",
            ]
        )

        def fake_delegate(
                agent,
                is_first_speaker=False,
                delegation=None,
                primary=True,
                protocol_mode="standard",
                delegation_only=False,
                silent=False,
                from_agent=None,
                prompt_kind=None,
                **_kwargs,
        ):
            calls.append((agent, protocol_mode, delegation_only, from_agent, delegation))
            return next(responses)

        app._delegate = fake_delegate

        app.run()

        tasks = list_tasks({"job_id": job_id}, db_path=str(db_path))
        self.assertEqual(tasks, [])
        self.assertEqual(
            printed,
            [
                (
                    AGENT_CLAUDE,
                    'vou tentar abrir task sozinho\n'
                    '<tool function="propose_task" description="rode os testes" />',
                ),
            ],
        )
        self.assertEqual(
            persisted,
            [
                ("human", "faça o teste pelo chat"),
                (
                    AGENT_CLAUDE,
                    'vou tentar abrir task sozinho\n'
                    '<tool function="propose_task" description="rode os testes" />',
                ),
            ],
        )
        self.assertEqual(app.renderer.delegations, [])

    def test_persist_message_saves_shared_state(self):
        """Verifica que persist message saves shared state."""
        import threading
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.shared_state = {"goal": "corrigir protocolo"}
        app.storage = DummyStorage()
        app._lock = threading.Lock()

        app.session_services = _make_session_services(app)
        app.session_services.persist_message("human", "oi")

        self.assertEqual(app.storage.saved_shared_state, {"goal": "corrigir protocolo"})

    def test_persist_message_caps_history_when_auto_summarize_is_disabled(self):
        """Verifica que persist message caps history when auto summarize is disabled."""
        import threading
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = [{"role": "human", "content": f"m{i}"} for i in range(24)]
        app.shared_state = {}
        app.storage = DummyStorage()
        app._lock = threading.Lock()
        app.prompt_builder = type("PromptBuilderStub", (), {"history_window": 2})()
        app.auto_summarize_threshold = 0

        app.session_services = _make_session_services(app)
        app.session_services.persist_message("human", "m24")

        self.assertEqual(len(app.history), 4)
        self.assertEqual(app.history[0]["content"], "m21")
        self.assertEqual(app.history[-1]["content"], "m24")

    def test_auto_summarize_merges_with_existing_session_summary(self):
        """Verifica que auto summarize merges with existing session summary."""
        class FakeContextManager:
            def __init__(self):
                self.saved_summary = None

            def load_session_summary(self):
                return "## Resumo da Conversa\n\n- Contexto anterior"

            def update_with_summary(self, summary):
                self.saved_summary = summary

        class FakeSessionSummarizer:
            def __init__(self):
                self.calls = []

            def summarize(self, history, existing_summary=None, preferred_agent=None, fallback=True):
                self.calls.append((history, existing_summary, preferred_agent))
                return "## Resumo da Conversa\n\n- Consolidado"

        # 12 mensagens com janela=2: surplus=10 >= _MIN_SUMMARIZE_SURPLUS, então resume as 10 primeiras.
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = [{"role": "human", "content": f"m{i}"} for i in range(1, 13)]
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.auto_summarize_threshold = 4
        app.prompt_builder = type("PromptBuilderStub", (), {"history_window": 2})()
        app.context_manager = FakeContextManager()
        app.session_summarizer = FakeSessionSummarizer()
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.shared_state = {"goal": "manter memória"}

        app.session_services = _make_session_services(app)
        app.session_services.maybe_auto_summarize()
        app.session_services.join_summarization(timeout=3)

        self.assertEqual(
            app.session_summarizer.calls,
            [
                (
                    [{"role": "human", "content": f"m{i}"} for i in range(1, 11)],
                    "## Resumo da Conversa\n\n- Contexto anterior",
                    "claude",
                )
            ],
        )
        self.assertEqual(app.context_manager.saved_summary, "## Resumo da Conversa\n\n- Consolidado")
        self.assertEqual(
            app.history,
            [
                {"role": "human", "content": "m11"},
                {"role": "human", "content": "m12"},
            ],
        )
        self.assertEqual(app.storage.saved_shared_state, {"goal": "manter memória"})

    def test_auto_summarize_skips_when_threshold_is_hit_but_history_window_is_larger(self):
        """Não resume nem emite UI quando o threshold dispara, mas nada sairia da janela de histórico."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = [{"role": "human", "content": f"m{i}"} for i in range(48)]
        app.auto_summarize_threshold = 30
        app.prompt_builder = type("PromptBuilderStub", (), {"history_window": 96})()
        app.context_manager = Mock()
        app.session_summarizer = Mock()
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.shared_state = {"goal": "manter memória"}

        app.session_services = _make_session_services(app)
        app.session_services.maybe_auto_summarize()

        app.context_manager.load_session_summary.assert_not_called()
        app.session_summarizer.summarize.assert_not_called()
        self.assertEqual(app.renderer.system_messages, [])
        self.assertEqual(len(app.history), 48)
        self.assertFalse(hasattr(app.storage, "saved_history"))

    def test_auto_summarize_skips_when_surplus_below_minimum(self):
        """Não resume quando o excedente acima da janela é menor que _MIN_SUMMARIZE_SURPLUS."""
        app = QuimeraApp.__new__(QuimeraApp)
        # surplus = 66 - 64 = 2, abaixo do mínimo de 10
        app.history = [{"role": "human", "content": f"m{i}"} for i in range(66)]
        app.auto_summarize_threshold = 30
        app.prompt_builder = type("PromptBuilderStub", (), {"history_window": 64})()
        app.context_manager = Mock()
        app.session_summarizer = Mock()
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.shared_state = {}

        app.session_services = _make_session_services(app)
        app.session_services.maybe_auto_summarize()

        app.session_summarizer.summarize.assert_not_called()
        self.assertEqual(app.renderer.system_messages, [])
        self.assertEqual(len(app.history), 66)

    def test_auto_summarize_runs_when_surplus_meets_minimum(self):
        """Resume quando o excedente atinge o mínimo de 10 mensagens acima da janela."""
        app = QuimeraApp.__new__(QuimeraApp)
        # surplus = 74 - 64 = 10, exatamente no limite mínimo
        app.history = [{"role": "human", "content": f"m{i}"} for i in range(74)]
        app.auto_summarize_threshold = 30
        app.prompt_builder = type("PromptBuilderStub", (), {"history_window": 64})()
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.context_manager = Mock()
        app.context_manager.load_session_summary.return_value = None
        app.session_summarizer = Mock()
        app.session_summarizer.summarize.return_value = "resumo"
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.shared_state = {}

        app.session_services = _make_session_services(app)
        app.session_services.maybe_auto_summarize()
        app.session_services.join_summarization(timeout=3)

        app.session_summarizer.summarize.assert_called_once()
        summarized_history = app.session_summarizer.summarize.call_args[0][0]
        self.assertEqual(len(summarized_history), 10)  # os 10 excedentes
        self.assertEqual(len(app.history), 64)  # janela preservada

    def test_shutdown_merges_existing_session_summary(self):
        """Verifica que shutdown merges existing session summary."""
        class FakeContextManager:
            def __init__(self):
                self.saved_summary = None

            def load_session_summary(self):
                return "## Resumo da Conversa\n\n- Memória acumulada"

            def update_with_summary(self, summary):
                self.saved_summary = summary

        class FakeSessionSummarizer:
            def __init__(self):
                self.calls = []

            def summarize(self, history, existing_summary=None, preferred_agent=None, fallback=True):
                self.calls.append((history, existing_summary, preferred_agent))
                return "## Resumo da Conversa\n\n- Memória consolidada"

        app = QuimeraApp.__new__(QuimeraApp)
        app.history = [{"role": "human", "content": "mensagem final"}]
        app.context_manager = FakeContextManager()
        app.session_summarizer = FakeSessionSummarizer()
        app.renderer = DummyRenderer()
        app.summary_agent_preference = "codex"
        app.task_services = Mock()
        app.task_services.stop_task_executors = Mock()

        _make_session_services(app).shutdown()

        self.assertEqual(
            app.session_summarizer.calls,
            [
                (
                    [{"role": "human", "content": "mensagem final"}],
                    "## Resumo da Conversa\n\n- Memória acumulada",
                    "codex",
                )
            ],
        )
        self.assertEqual(app.context_manager.saved_summary, "## Resumo da Conversa\n\n- Memória consolidada")
        # Mensagens de resumo agora vão apenas para o log, não mais para a UI.
        self.assertEqual(app.renderer.system_messages, [])

    def test_shutdown_skips_summary_when_interrupted(self):
        """Verifica que shutdown skips summary when interrupted."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = [{"role": "human", "content": "mensagem final"}]
        app.context_manager = DummyContextManager()
        app.session_summarizer = Mock()
        app.renderer = DummyRenderer()
        app.summary_agent_preference = "codex"
        app.task_services = Mock()
        app.task_services.stop_task_executors = Mock()

        _make_session_services(app).shutdown(interrupted=True)

        app.session_summarizer.summarize.assert_not_called()
        self.assertEqual(app.renderer.system_messages, [])

    def test_shutdown_cancels_agent_summary_when_join_is_interrupted(self):
        """Verifica que shutdown cancels agent summary when join is interrupted."""
        class FakeThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon
                self.started = False
                self.join_calls = 0

            def start(self):
                self.started = True

            def join(self, timeout=None):
                self.join_calls += 1
                if self.join_calls == 1:
                    raise KeyboardInterrupt()

        app = QuimeraApp.__new__(QuimeraApp)
        app.history = [{"role": "human", "content": "mensagem final"}]
        app.context_manager = DummyContextManager()
        app.session_summarizer = Mock()
        app.renderer = DummyRenderer()
        app.summary_agent_preference = "ollama-qwen"
        app.agent_client = SimpleNamespace(_user_cancelled=False, _cancel_event=threading.Event())
        app.task_services = Mock()
        app.task_services.stop_task_executors = Mock()

        with patch("quimera.app.session.threading.Thread", FakeThread):
            _make_session_services(app).shutdown()

        self.assertTrue(app.agent_client._user_cancelled)
        self.assertTrue(app.agent_client._cancel_event.is_set())
        # Mensagem de falha agora vai para o log, não mais para a UI.
        self.assertEqual(app.renderer.system_messages, [])

    def test_summarize_session_returns_none_when_all_backends_unavailable(self):
        """Verifica que summarize session returns none when all backends unavailable."""
        class DummyRendererWithSystem(DummyRenderer):
            def __init__(self):
                super().__init__()
                self.system_messages = []

            def show_system(self, message):
                self.system_messages.append(message)

            def show_error(self, message):
                self.system_messages.append(message)

        renderer = DummyRendererWithSystem()
        summarizer = SessionSummarizer(renderer, summarizer_call=Mock(return_value=None))

        summary = summarizer.summarize(
            [{"role": "human", "content": "Precisamos validar o formato /caminho/absoluto/arquivo:linha."}],
            existing_summary="## Resumo anterior",
        )

        self.assertIsNone(summary)
        self.assertIn("[memória] resumidores indisponíveis", renderer.system_messages)

    def test_summarize_session_returns_none_when_backend_raises(self):
        """Verifica que summarize session returns none when backend raises."""
        class DummyRendererWithSystem(DummyRenderer):
            def __init__(self):
                super().__init__()
                self.system_messages = []

            def show_system(self, message):
                self.system_messages.append(message)

            def show_error(self, message):
                self.system_messages.append(message)

        def broken_summarizer(_prompt, preferred_agent=None):
            raise TypeError("backend bug")

        renderer = DummyRendererWithSystem()
        summarizer = SessionSummarizer(renderer, summarizer_call=broken_summarizer)

        summary = summarizer.summarize(
            [{"role": "human", "content": "Vamos fechar o contrato do resumidor."}],
            preferred_agent="codex",
        )

        self.assertIsNone(summary)
        self.assertEqual(renderer.system_messages, ["[memória] resumidores indisponíveis"])

    def test_chain_summarizer_stops_fallback_when_user_cancels(self):
        """Verifica que chain summarizer stops fallback when user cancels."""
        class DummyAgentClient:
            def __init__(self, renderer):
                self.renderer = renderer
                self._user_cancelled = False
                self.calls = []

            def call(self, agent, prompt, silent=False, allow_tools=True):
                self.calls.append((agent, prompt, silent, allow_tools))
                self._user_cancelled = True
                return None

        renderer = DummyRenderer()
        agent_client = DummyAgentClient(renderer)
        summarizer_call = build_chain_summarizer(agent_client, ["chatgpt", "codex"])

        result = summarizer_call("resuma", preferred_agent="chatgpt")

        self.assertIsNone(result)
        self.assertEqual(agent_client.calls, [("chatgpt", "resuma", True, False)])
        self.assertEqual(renderer.system_messages, [])
        self.assertEqual(summarizer_call.last_outcome, "cancelled")

    def test_summarize_session_suppresses_unavailable_message_on_user_cancel(self):
        """Verifica que summarize session suppresses unavailable message on user cancel."""
        class DummyAgentClient:
            def __init__(self, renderer):
                self.renderer = renderer
                self._user_cancelled = False

            def call(self, agent, prompt, silent=False, allow_tools=True):
                self._user_cancelled = True
                return None

        renderer = DummyRenderer()
        summarizer_call = build_chain_summarizer(DummyAgentClient(renderer), ["chatgpt", "codex"])
        summarizer = SessionSummarizer(renderer, summarizer_call=summarizer_call)

        summary = summarizer.summarize(
            [{"role": "human", "content": "gerar resumo"}],
            preferred_agent="chatgpt",
        )

        self.assertIsNone(summary)
        self.assertEqual(renderer.system_messages, [])

    def test_chain_summarizer_stops_fallback_when_cancel_event_is_already_set(self):
        """Verifica que chain summarizer stops fallback when cancel event is already set."""
        class DummyAgentClient:
            def __init__(self, renderer):
                self.renderer = renderer
                self._user_cancelled = False
                self._cancel_event = threading.Event()
                self.calls = []

            def call(self, agent, prompt, silent=False, allow_tools=True):
                self.calls.append((agent, prompt, silent, allow_tools))
                self._cancel_event.set()
                return None

        renderer = DummyRenderer()
        agent_client = DummyAgentClient(renderer)
        summarizer_call = build_chain_summarizer(agent_client, ["chatgpt", "codex"])

        result = summarizer_call("resuma", preferred_agent="chatgpt")

        self.assertIsNone(result)
        self.assertEqual(agent_client.calls, [("chatgpt", "resuma", True, False)])
        self.assertEqual(renderer.system_messages, [])
        self.assertEqual(summarizer_call.last_outcome, "cancelled")

    def test_chain_summarizer_can_disable_fallback_for_summary(self):
        class DummyAgentClient:
            def __init__(self, renderer):
                self.renderer = renderer
                self._user_cancelled = False
                self._cancel_event = threading.Event()
                self.calls = []

            def call(self, agent, prompt, silent=False, allow_tools=True):
                self.calls.append((agent, prompt, silent, allow_tools))
                return None

        renderer = DummyRenderer()
        agent_client = DummyAgentClient(renderer)
        summarizer_call = build_chain_summarizer(agent_client, ["chatgpt", "codex"])

        result = summarizer_call("resuma", preferred_agent="chatgpt", fallback=False)

        self.assertIsNone(result)
        self.assertEqual(agent_client.calls, [("chatgpt", "resuma", True, False)])
        self.assertEqual(summarizer_call.last_outcome, "unavailable")


    def test_chain_summarizer_does_not_emit_per_agent_unavailable_messages(self):
        """Verifica que chain summarizer does not emit per agent unavailable messages."""
        class DummyAgentClient:
            def __init__(self, renderer):
                self.renderer = renderer
                self._user_cancelled = False
                self._cancel_event = threading.Event()
                self.calls = []

            def call(self, agent, prompt, silent=False, allow_tools=True):
                self.calls.append((agent, prompt, silent, allow_tools))
                return None

        renderer = DummyRenderer()
        agent_client = DummyAgentClient(renderer)
        summarizer_call = build_chain_summarizer(agent_client, ["chatgpt", "codex"])
        summarizer = SessionSummarizer(renderer, summarizer_call=summarizer_call)

        summary = summarizer.summarize(
            [{"role": "human", "content": "gerar resumo"}],
            preferred_agent="chatgpt",
        )

        self.assertIsNone(summary)
        self.assertEqual(
            agent_client.calls,
            [("chatgpt", unittest.mock.ANY, True, False), ("codex", unittest.mock.ANY, True, False)],
        )
        self.assertEqual(renderer.system_messages, ["[memória] resumidores indisponíveis"])

    def test_session_summary_prompt_explicitly_restricts_scope_to_provided_messages(self):
        """Verifica que session summary prompt explicitly restricts scope to provided messages."""
        prompt = SessionSummarizer._build_prompt(
            [{"role": "human", "content": "Mensagem relevante"}],
            existing_summary="## Resumo anterior",
        )

        self.assertIn("Baseie-se exclusivamente no material abaixo", prompt)
        self.assertIn("Não use ferramentas, arquivos, shell, web ou memória externa", prompt)

    def test_shutdown_summary_thread_does_not_mark_agents_unavailable_due_to_signal_registration(self):
        """Verifica que shutdown summary thread does not mark agents unavailable due to signal registration."""
        class DummyStatusContext:
            def __init__(self, status):
                self._status = status

            def __enter__(self):
                return self._status

            def __exit__(self, exc_type, exc, tb):
                return False

        renderer = Mock()
        status = Mock()
        renderer.running_status.return_value = DummyStatusContext(status)
        agent_client = AgentClient(renderer)

        with patch("quimera.profiles.get") as mock_get:
            mock_profile = SimpleNamespace(
                driver="openai_compat",
                model="qwen3-coder:30b",
                base_url="http://localhost:11434/v1",
                api_key_env=None,
                tool_use_reliability="medium",
                supports_tools=True,
            )
            mock_get.return_value = mock_profile

            with patch.object(agent_client, "_api_drivers", {"ollama-qwen": Mock()}):
                agent_client._api_drivers["ollama-qwen"].run.return_value = "Resumo final"
                summarizer_call = build_chain_summarizer(agent_client, ["ollama-qwen"])
                summarizer = SessionSummarizer(renderer, summarizer_call=summarizer_call)
                result = {}

                def worker():
                    result["summary"] = summarizer.summarize(
                        [{"role": "human", "content": "encerrar sessão"}],
                        preferred_agent="ollama-qwen",
                    )

                thread = threading.Thread(target=worker)
                thread.start()
                thread.join()

        self.assertEqual(result["summary"], "Resumo final")
        renderer.show_system.assert_not_called()

    def test_call_api_marks_user_cancelled_when_cancel_event_finishes_driver_without_result(self):
        """Verifica que call api marks user cancelled when cancel event finishes driver without result."""
        class DummyStatusContext:
            def __init__(self, status):
                self._status = status

            def __enter__(self):
                return self._status

            def __exit__(self, exc_type, exc, tb):
                return False

        renderer = Mock()
        status = Mock()
        renderer.running_status.return_value = DummyStatusContext(status)
        agent_client = AgentClient(renderer)

        profile = SimpleNamespace(
            driver="openai_compat",
            model="qwen3-coder:30b",
            base_url="http://localhost:11434/v1",
            api_key_env=None,
            tool_use_reliability="medium",
            supports_tools=True,
            cmd=None,
        )

        def driver_run(*, cancel_event=None, **kwargs):
            if cancel_event is not None:
                cancel_event.set()
            return None

        with patch.object(agent_client, "_api_drivers", {"ollama-qwen": Mock()}):
            agent_client._api_drivers["ollama-qwen"].run.side_effect = driver_run
            result = agent_client._call_api("ollama-qwen", profile, "resuma", quiet=True)

        self.assertIsNone(result)
        self.assertTrue(agent_client._user_cancelled)
        renderer.show_error.assert_not_called()

    def test_call_api_disables_tool_executor_when_allow_tools_is_false(self):
        """Verifica que call api disables tool executor when allow tools is false."""
        class DummyStatusContext:
            def __init__(self, status):
                self._status = status

            def __enter__(self):
                return self._status

            def __exit__(self, exc_type, exc, tb):
                return False

        renderer = Mock()
        status = Mock()
        renderer.running_status.return_value = DummyStatusContext(status)
        agent_client = AgentClient(renderer)
        agent_client.tool_executor = Mock()

        profile = SimpleNamespace(
            driver="openai_compat",
            model="qwen3-coder:30b",
            base_url="http://localhost:11434/v1",
            api_key_env=None,
            tool_use_reliability="medium",
            supports_tools=True,
            cmd=None,
        )

        driver_mock = Mock()
        with patch.object(agent_client, "_api_drivers", {"ollama-qwen": driver_mock}):
            driver_mock.run.return_value = "Resumo final"
            result = agent_client._call_api(
                "ollama-qwen",
                profile,
                "resuma",
                quiet=True,
                allow_tools=False,
            )

        self.assertEqual(result, "Resumo final")
        self.assertIsNone(driver_mock.run.call_args.kwargs["tool_executor"])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        importlib.reload(profiles)

    def test_agent_profile_fields(self):
        """Verifica que agent profile fields."""
        p = ExecutionProfile(name="test", prefix="/test", cmd=["test", "-p"], style=("red", "Test"))

        self.assertEqual(p.name, "test")
        self.assertEqual(p.prefix, "/test")
        self.assertEqual(p.cmd, ["test", "-p"])
        self.assertEqual(p.style, ("red", "Test"))

    def test_register_and_get(self):
        """Verifica que register and get."""
        p = ExecutionProfile(name="dummy", prefix="/dummy", cmd=["dummy"], style=("yellow", "Dummy"))

        with patch("quimera.profiles.base._registry", ProfileRegistry()):
            profiles.register(p)
            self.assertIs(profiles.get("dummy"), p)

    def test_get_returns_none_for_unknown(self):
        """Verifica que get returns none for unknown."""
        with patch("quimera.profiles.base._registry", ProfileRegistry()):
            self.assertIsNone(profiles.get("naoexiste"))

    def test_default_profiles_loaded(self):
        """Verifica que default profiles loaded."""
        self.assertIn("claude", profiles.all_names())
        self.assertIn("codex", profiles.all_names())

    def test_all_profiles_returns_agent_profile_instances(self):
        """Verifica que all profiles returns agent profile instances."""
        for p in profiles.all_profiles():
            self.assertIsInstance(p, ExecutionProfile)

    def test_all_names_matches_all_profiles(self):
        """Verifica que all names matches all profiles."""
        names = profiles.all_names()
        self.assertEqual(len(names), len(profiles.all_profiles()))
        for p in profiles.all_profiles():
            self.assertIn(p.name, names)

    def test_agent_style_returns_profile_style(self):
        """Verifica que agent style returns profile style."""
        def get_style(agent):
            return ("magenta", "🤖  Stub") if agent == "stub" else None

        self.assertEqual(_agent_style("stub", get_profile_style=get_style), ("magenta", "🤖  Stub"))

    def test_agent_style_fallback_for_unknown(self):
        """Verifica que agent style fallback for unknown."""
        with patch("quimera.profiles.base._registry", ProfileRegistry()):
            color, label = _agent_style("unknown")
            self.assertEqual(color, "white")
            self.assertEqual(label, "🤖  Unknown")

    def test_agent_client_call_uses_profile_cmd(self):
        """Verifica que agent client call uses profile cmd."""
        stub = ExecutionProfile(name="stub", prefix="/stub", cmd=["stub", "-x"], style=("white", "Stub"))
        renderer = Mock()

        reg = ProfileRegistry()
        reg.register(stub)
        with patch("quimera.profiles.base._registry", reg):
            client = AgentClient(renderer)
            with patch.object(client, "run", return_value="ok") as mock_run:
                result = client.call("stub", "hello")

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        self.assertEqual(call_args[0][0], ["stub", "-x"])
        self.assertEqual(call_args[1]["input_text"], "hello")
        self.assertEqual(result, "ok")

    def test_agent_client_call_error_on_unknown_agent(self):
        """Verifica que agent client call error on unknown agent."""
        renderer = Mock()
        with patch("quimera.profiles.base._registry", ProfileRegistry()):
            client = AgentClient(renderer)
            result = client.call("fantasma", "msg")

        self.assertIsNone(result)
        renderer.show_error.assert_called_once()
        self.assertIn("fantasma", renderer.show_error.call_args[0][0])

    def test_new_profile_registration_visible_via_all_names(self):
        """Verifica que new profile registration visible via all names."""
        novo = ExecutionProfile(name="novo", prefix="/novo", cmd=["novo"], style=("cyan", "Novo"))

        with patch("quimera.profiles.base._registry", ProfileRegistry()):
            profiles.register(novo)
            self.assertEqual(profiles.all_names(), ["novo"])
            self.assertEqual(profiles.all_profiles(), [novo])

    def test_parallel_threads_initializes_correctly(self):
        """Verifica que parallel threads initializes correctly."""
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = QuimeraApp(tmp, debug=False, history_window=10, agents=["agent1", "agent2"], threads=3, input_gate_factory=lambda **kw: MagicMock())
        self.assertEqual(app.threads, 3)
        self.assertIn("agent1", app.active_agents)
        self.assertIn("agent2", app.active_agents)
        self.assertTrue(hasattr(app.task_services, "delegate_for_parallel"))
        app._stop_task_executors()

    def test_parallel_threads_calls_agents_concurrently(self):
        """Verifica que parallel threads calls agents concurrently."""
        # Testa que o método _delegate_for_parallel retorna tupla correta
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.threads = 2
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool(["agent1", "agent2"])
        app.active_agents = ["agent1", "agent2"]
        app.debug_prompt_metrics = False
        app.session_call_index = 0
        app._lock = threading.Lock()
        app._output_lock = threading.Lock()
        app._counter_lock = threading.Lock()
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = "dummy prompt"
        app.agent_client = Mock()
        app.agent_client.call.return_value = "Resposta mock"
        app.tool_executor = Mock()
        app.history = []
        app.shared_state = {}
        app.renderer = Mock()
        app.storage = Mock()
        app.context_manager = Mock()
        app.session_state = {"session_id": "test"}
        app.round_index = 0
        app.summary_agent_preference = None
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.task_services = Mock()
        app.task_services.refresh_task_shared_state = Mock()
        app._record_agent_metric = Mock()

        from pathlib import Path
        import tempfile
        staging_root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        agent, response, extend = delegate_for_parallel(
            app, "agent1", None, "standard", staging_root, 0
        )
        self.assertEqual(agent, "agent1")
        self.assertEqual(response, "Resposta mock")
        self.assertFalse(extend)

    def test_run_thread_mode_accepts_new_human_input_while_agent_is_running(self):
        """Verifica que run thread mode accepts new human input while agent is running."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.history = []
        app.user_name = "Você"
        app.round_index = 0
        app.session_call_index = 0
        app.debug_prompt_metrics = False
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = DummyAgentClient()
        app.prompt_builder = None
        app.session_state = {
            "session_id": "sessao-2026-03-27-123456",
            "history_count": 0,
            "summary_loaded": False,
        }
        app.shared_state = {}
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.threads = 2
        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        persisted = []
        printed = []
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content, **kwargs: persisted.append((role, content))
        app.dispatch_services.print_response = lambda agent, response: printed.append((agent, response))

        call_started = threading.Event()
        second_prompt_seen = threading.Event()
        allow_finish = threading.Event()

        def fake_read_user_input(prompt, timeout=0):
            if not call_started.is_set():
                return "mensagem"
            second_prompt_seen.set()
            return "/exit"

        def fake_delegate(agent, **kwargs):
            call_started.set()
            allow_finish.wait(timeout=2)
            return "claude responde"

        app.read_user_input = Mock(side_effect=fake_read_user_input)
        app.dispatch_services.delegate = fake_delegate

        run_thread = threading.Thread(target=app.run)
        run_thread.start()

        self.assertTrue(second_prompt_seen.wait(timeout=1),
                        "run() não voltou ao prompt enquanto o agente ainda executava")
        allow_finish.set()
        run_thread.join(timeout=2)

        self.assertFalse(run_thread.is_alive(), "run() deveria encerrar após drenar a fila")
        self.assertEqual(persisted[0], ("human", "oi"))
        self.assertIn((AGENT_CLAUDE, "claude responde"), printed)

    def test_turn_manager_wait_for_human_turn_unblocks_immediately_after_agent_response(self):
        """Verifica que turn manager wait for human turn unblocks immediately after agent response."""
        turn_manager = TurnManager()
        turn_manager.next_turn()

        started = threading.Event()
        released = []

        def _waiter():
            started.set()
            released.append(turn_manager.wait_for_human_turn(timeout=1))

        waiter = threading.Thread(target=_waiter, daemon=True)
        waiter.start()

        self.assertTrue(started.wait(timeout=1), "thread de espera não iniciou")
        time.sleep(0.02)
        turn_manager.next_turn()

        waiter.join(timeout=0.2)
        self.assertFalse(waiter.is_alive(), "espera pelo turno humano não deveria depender de polling lento")
        self.assertEqual(released, [True])

    def test_read_user_input_zero_timeout_tty_uses_blocking_input_path(self):
        """Verifica que read user input zero timeout tty uses blocking input path."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.renderer = DummyRenderer()
        app._deferred_system_messages = []
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_prompt_visible = False
        app.runtime_state.nonblocking_input_queue = None
        app.runtime_state.nonblocking_input_thread = None
        app.runtime_state.nonblocking_input_status = "idle"
        app.runtime_state.nonblocking_prompt_text = ""

        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        with patch("sys.stdin", stdin), patch("builtins.input", return_value="mensagem") as mock_input:
            result = app.read_user_input("Você: ", timeout=0)

        self.assertEqual(result, "mensagem")
        mock_input.assert_called_once_with("Você: ")

    def test_read_user_input_zero_timeout_tty_marks_prompt_as_reading_during_blocking_input(self):
        """Verifica que read user input zero timeout tty marks prompt as reading during blocking input."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.renderer = DummyRenderer()
        app._deferred_system_messages = []
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_prompt_visible = False
        app.runtime_state.nonblocking_input_queue = None
        app.runtime_state.nonblocking_input_thread = None
        app.runtime_state.nonblocking_input_status = "idle"
        app.runtime_state.nonblocking_prompt_text = ""

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        def _input_with_assertions(prompt):
            self.assertEqual(prompt, "Alex: ")
            self.assertEqual(app.runtime_state.nonblocking_input_status, "reading")
            self.assertEqual(app.runtime_state.nonblocking_prompt_text, "Alex: ")
            return "mensagem"

        with patch("sys.stdin", stdin), patch("builtins.input", side_effect=_input_with_assertions):
            result = app.read_user_input("Alex: ", timeout=0)

        self.assertEqual(result, "mensagem")
        self.assertEqual(app.runtime_state.nonblocking_input_status, "idle")
        self.assertEqual(app.runtime_state.nonblocking_prompt_text, "")

    def test_read_user_input_with_timeout_polls_stdin_without_spawning_thread(self):
        """Verifica que read user input with timeout polls stdin without spawning thread."""
        stdin = Mock()
        stdin.isatty.return_value = False
        stdin.readline.return_value = "mensagem\n"

        with patch("quimera.app.inputs._tty._stdin", return_value=stdin), patch(
            "quimera.app.inputs._tty.select.select",
            return_value=([stdin], [], []),
        ), patch("quimera.app.inputs._tty.threading.Thread") as mock_thread:
            result = read_user_input_with_timeout("Você: ", timeout=1)

        self.assertEqual(result, "mensagem")
        mock_thread.assert_not_called()

    def test_read_user_input_with_timeout_returns_none_without_spawning_thread_when_idle(self):
        """Verifica que read user input with timeout returns none without spawning thread when idle."""
        stdin = Mock()
        stdin.isatty.return_value = False

        with patch("quimera.app.inputs._tty._stdin", return_value=stdin), patch(
            "quimera.app.inputs._tty.select.select",
            return_value=([], [], []),
        ), patch("quimera.app.inputs._tty.threading.Thread") as mock_thread:
            result = read_user_input_with_timeout("Você: ", timeout=1)

        self.assertIsNone(result)
        mock_thread.assert_not_called()

    def test_read_user_input_zero_timeout_tty_flushes_deferred_messages_before_prompt(self):
        """Verifica que read user input zero timeout tty flushes deferred messages before prompt."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.renderer = DummyRenderer()
        app._deferred_system_messages = ["[task 7] claude:\nresultado final"]
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_prompt_visible = False
        app.runtime_state.nonblocking_input_queue = None
        app.runtime_state.nonblocking_input_thread = None
        app.runtime_state.nonblocking_input_status = "reading"
        app.runtime_state.nonblocking_prompt_text = "Você: "
        app._output_lock = threading.Lock()

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("builtins.input", return_value="oi"):
            value = app.read_user_input("Você: ", timeout=0)

        self.assertEqual(value, "oi")
        self.assertEqual(app.runtime_state.nonblocking_input_status, "idle")
        self.assertEqual(app.runtime_state.nonblocking_prompt_text, "")
        self.assertEqual(app._deferred_system_messages, [])
        self.assertEqual(app.renderer.system_messages, ["[task 7] claude:\nresultado final"])

    def test_read_user_input_zero_timeout_tty_raises_keyboard_interrupt(self):
        """Verifica que read user input zero timeout tty raises keyboard interrupt."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.renderer = DummyRenderer()
        app._deferred_system_messages = []
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_prompt_visible = False
        app.runtime_state.nonblocking_input_queue = None
        app.runtime_state.nonblocking_input_thread = None
        app.runtime_state.nonblocking_input_status = "idle"
        app.runtime_state.nonblocking_prompt_text = ""

        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        with patch("sys.stdin", stdin), patch("builtins.input", side_effect=KeyboardInterrupt), patch(
            "builtins.print"
        ) as mock_print:
            with self.assertRaises(KeyboardInterrupt):
                app.read_user_input("Você: ", timeout=0)

        mock_print.assert_called_once_with()

    def test_read_from_editor_holds_output_lock_during_editor_session(self):
        """Verifica que read from editor holds output lock during editor session."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()

        def _fake_editor_run(cmd, check):
            self.assertTrue(app._output_lock.locked())
            Path(cmd[-1]).write_text("texto do editor", encoding="utf-8")
            return None

        with patch.dict("os.environ", {"EDITOR": "fake-editor"}), patch(
            "quimera.process_factory.run",
            side_effect=_fake_editor_run,
        ):
            content = read_from_editor(app.renderer, output_lock=app._output_lock)

        self.assertEqual(content, "texto do editor")
        self.assertFalse(app._output_lock.locked())

    def test_read_from_editor_preserves_multiline_content(self):
        """Verifica que read from editor preserves multiline content."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        expected = (
            "\"\"\"Serviços de sessão, persistência e sumarização.\"\"\"\n"
            "import sys\n"
            "import threading\n"
            "import time"
        )

        def _fake_editor_run(cmd, check):
            Path(cmd[-1]).write_text(expected + "\n", encoding="utf-8")
            return None

        with patch.dict("os.environ", {"EDITOR": "fake-editor"}), patch(
            "quimera.process_factory.run",
            side_effect=_fake_editor_run,
        ):
            content = read_from_editor(app.renderer, output_lock=app._output_lock)

        self.assertEqual(content, expected)

    def test_read_from_editor_writes_newline_to_stdout_after_editor_exits(self):
        """Verifica que read from editor writes newline to stdout after editor exits."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()

        def _fake_editor_run(cmd, check):
            Path(cmd[-1]).write_text("mensagem", encoding="utf-8")
            return None

        with patch.dict("os.environ", {"EDITOR": "fake-editor"}), patch(
            "quimera.process_factory.run",
            side_effect=_fake_editor_run,
        ), patch("quimera.editor.sys.stdout") as mock_stdout:
            read_from_editor(app.renderer, output_lock=app._output_lock)

        writes = [c.args[0] for c in mock_stdout.write.call_args_list]
        self.assertIn("\n", writes)

    def test_read_from_editor_preserves_content_with_special_chars(self):
        """Conteúdo com triple-quotes, chaves e caracteres especiais não é truncado."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        expected = (
            '"""Serviços de sessão, persistência e sumarização."""\n'
            "import sys\n"
            "import threading\n"
            "import time\n"
            "\n"
            "class AppSessionServices:\n"
            '    def __init__(self, app):\n'
            "        self.app = app\n"
            '        app.history.append({"role": role, "content": content})\n'
            "        app.storage.save_history(app.history, shared_state=app.shared_state)"
        )

        def _fake_editor_run(cmd, check):
            Path(cmd[-1]).write_text(expected + "\n", encoding="utf-8")
            return None

        with patch.dict("os.environ", {"EDITOR": "fake-editor"}), patch(
            "quimera.process_factory.run",
            side_effect=_fake_editor_run,
        ), patch("quimera.editor.sys.stdout"):
            content = read_from_editor(app.renderer, output_lock=app._output_lock)

        self.assertEqual(content, expected)

    def test_show_system_message_suppresses_transient_task_status_while_tty_reader_is_active(self):
        """Verifica que show system message suppresses transient task status while tty reader is active."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "reading"
        app.runtime_state.nonblocking_prompt_text = "Alex: "
        app.input_gate = Mock()
        app.input_gate.get_line_buffer.return_value = ""

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin):
            app.system_layer.show_system_message("[task 7] claude: iniciando")

        self.assertEqual(app.renderer.system_messages, [])
        app.input_gate.redisplay.assert_not_called()

    def test_show_system_message_redraws_human_prompt_with_user_name_for_task_error_text(self):
        """Verifica que show system message redraws human prompt with user name for task error text."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "reading"
        app.runtime_state.nonblocking_prompt_text = "Alex: "
        app.input_gate = Mock()
        app.input_gate.get_line_buffer.return_value = ""

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        msg = "[task 7] claude: erro: falha de rede"
        with patch("sys.stdin", stdin), patch("sys.stdout.write"), patch("sys.stdout.flush"):
            app.system_layer.show_system_message(msg)

        self.assertEqual(app._deferred_system_messages, [("system", msg)])

    def test_redisplay_user_prompt_does_not_sleep_while_redrawing_after_agent_output(self):
        """Verifica que redisplay user prompt does not sleep while redrawing after agent output."""
        app = QuimeraApp.__new__(QuimeraApp)
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "reading"
        app.runtime_state.nonblocking_prompt_text = "Alex: "
        app.input_gate = Mock()
        app.input_gate.get_line_buffer.return_value = "digitando"
        app.input_gate.is_active.return_value = True

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("sys.stdout.write"), patch("sys.stdout.flush"), patch(
            "time.sleep"
        ) as mock_sleep:
            for _ in range(5):
                app._redisplay_user_prompt_if_needed()

        mock_sleep.assert_not_called()
        self.assertEqual(app.input_gate.redisplay.call_count, 5)

    def test_show_system_message_defers_multiline_review_message_while_tty_reader_is_active(self):
        """Verifica que show system message defers multiline review message while tty reader is active."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "reading"
        app.runtime_state.nonblocking_prompt_text = "Alex: "

        renderer = Mock()
        app.renderer = renderer
        app.input_gate = Mock()
        app.input_gate.get_line_buffer.return_value = ""

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        msg = "[task 7] gemini:\nACEITE\nResultado validado com evidência concreta."
        with patch("sys.stdin", stdin), patch("sys.stdout.write"), patch("sys.stdout.flush"):
            app.system_layer.show_system_message(msg)

        self.assertEqual(app._deferred_system_messages, [])
        renderer.show_system.assert_called_once_with(msg)

    def test_staging_logger_does_not_touch_prompt_for_info_logs_while_tty_reader_is_active(self):
        """Verifica que staging logger does not touch prompt for info logs while tty reader is active."""
        app = QuimeraApp.__new__(QuimeraApp)
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "reading"
        app.runtime_state.nonblocking_prompt_text = "Alex: "
        app.input_gate = Mock()
        app.input_gate.get_line_buffer.return_value = ""
        app.system_layer = Mock()

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        prompt_handler = next(handler for handler in app_module.logger.handlers if
                              isinstance(handler, app_module.PromptAwareStderrHandler))
        previous_app = prompt_handler._app
        bind_handler_app(prompt_handler, app)
        try:
            with patch("sys.stdin", stdin), patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush") as mock_flush:
                app_module.logger.info("[DISPATCH] sending to agent=%s", AGENT_CODEX)

            self.assertNotIn(call("\r\x1b[2K"), mock_write.call_args_list)
            self.assertNotIn(call("Alex: "), mock_write.call_args_list)
            self.assertEqual(mock_flush.call_count, 0)
            app.input_gate.redisplay.assert_not_called()
        finally:
            bind_handler_app(prompt_handler, previous_app)

    def test_staging_logger_still_shows_warning_logs_while_tty_reader_is_active(self):
        """Verifica que staging logger still shows warning logs while tty reader is active."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "reading"
        app.runtime_state.nonblocking_prompt_text = "Alex: "
        app.input_gate = Mock()
        app.input_gate.get_line_buffer.return_value = ""

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        prompt_handler = next(handler for handler in app_module.logger.handlers if
                              isinstance(handler, app_module.PromptAwareStderrHandler))
        previous_app = prompt_handler._app
        bind_handler_app(prompt_handler, app)
        try:
            with patch("sys.stdin", stdin), patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush"):
                app_module.logger.warning("[DISPATCH] retry for agent=%s", AGENT_CODEX)

            self.assertEqual(mock_write.call_args_list, [])
            self.assertEqual(len(app.renderer.warnings), 1)
            self.assertTrue(
                app.renderer.warnings[0].endswith("[DISPATCH] retry for agent=codex")
            )
            app.input_gate.redisplay.assert_not_called()
        finally:
            bind_handler_app(prompt_handler, previous_app)

    def test_show_system_message_uses_prompt_toolkit_redisplay_without_manual_clear(self):
        """Verifica que show system message uses prompt toolkit redisplay without manual clear."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "reading"
        app.runtime_state.nonblocking_prompt_text = "Alex: "
        app.input_gate = Mock()
        app.input_gate.get_line_buffer.return_value = "oi"
        app.input_gate.is_active.return_value = True

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush"):
            app.system_layer.show_system_message("erro: timeout")

        clear_calls = [call_args for call_args in mock_write.call_args_list if call_args == call("\r\x1b[2K")]
        self.assertEqual(len(clear_calls), 0)
        app.input_gate.redisplay.assert_called_once_with()

    def test_print_response_uses_prompt_toolkit_redisplay_without_manual_prompt_rewrite(self):
        """Verifica que print response uses prompt toolkit redisplay without manual prompt rewrite."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "reading"
        app.runtime_state.nonblocking_prompt_text = "Alex: "
        app.renderer = Mock()
        app.input_gate = Mock()
        app.input_gate.get_line_buffer.return_value = "oi"
        app.input_gate.is_active.return_value = True

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush"):
            app.print_response("claude", "resposta final")

        app.renderer.show_message.assert_called_once_with("claude", "resposta final")
        clear_calls = [call_args for call_args in mock_write.call_args_list if call_args == call("\r\x1b[2K")]
        self.assertEqual(len(clear_calls), 0)
        self.assertNotIn(call("Alex: oi"), mock_write.call_args_list)
        app.input_gate.redisplay.assert_called_once_with()

    def test_show_system_message_defers_task_output_while_tty_reader_is_active(self):
        """Verifica que show system message defers task output while tty reader is active."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        from quimera.app.runtime_state import AppRuntimeState
        app.runtime_state = AppRuntimeState()
        app.runtime_state.nonblocking_input_status = "reading"

        app.system_layer.show_system_message("[task 7] claude:\nresultado final")

        self.assertEqual(app.renderer.system_messages, ["[task 7] claude:\nresultado final"])
        self.assertEqual(app._deferred_system_messages, [])

    def test_parse_routing_selects_first_initial_agent(self):
        """Verifica que parse routing selects first initial agent."""
        app = QuimeraApp.__new__(QuimeraApp)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.round_index = 0
        app.renderer = DummyRenderer()

        materialize_internal_services(app)
        agent, message, explicit = app.parse_routing("oi")

        self.assertEqual(agent, AGENT_CLAUDE)
        self.assertEqual(message, "oi")
        self.assertFalse(explicit)

    def test_parse_routing_fallback_normalizes_profile_objects_to_agent_names(self):
        """Verifica que parse routing fallback normalizes profile objects to agent names."""
        app = QuimeraApp.__new__(QuimeraApp)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([])
        app.active_agents = []
        app.selected_agents = []
        app.round_index = 0
        app.renderer = DummyRenderer()
        app.get_active_agent_profiles = Mock(return_value=[])
        profile = ExecutionProfile(
            name=AGENT_CLAUDE,
            prefix="/claude",
            style=("blue", "Claude"),
            driver="claude",
            model="claude-sonnet",
            supports_tools=True,
        )
        app.get_available_profiles = Mock(return_value=[profile])

        materialize_internal_services(app)
        agent, message, explicit = app.parse_routing("oi")

        self.assertEqual(agent, AGENT_CLAUDE)
        self.assertEqual(message, "oi")
        self.assertFalse(explicit)
        self.assertEqual(app.active_agents, [AGENT_CLAUDE])
        self.assertIsInstance(app.active_agents[0], str)

    def test_record_failure_accepts_agent_profile_instance(self):
        """Verifica que record failure accepts agent profile instance."""
        from quimera.app.agent_failure_tracker import AgentFailureTracker
        from quimera.app.agent_pool import AgentPool

        agent_pool = AgentPool([AGENT_CLAUDE])
        record_metric = Mock()
        tracker = AgentFailureTracker(
            normalize_agent_name=normalize_agent_name,
            agent_pool=agent_pool,
            release_agent_tasks=lambda _name: None,
            record_metric=record_metric,
            file_bug=None,
            get_session_id=lambda: "test",
        )
        profile = ExecutionProfile(
            name=AGENT_CLAUDE,
            prefix="/claude",
            style=("blue", "Claude"),
            driver="claude",
            model="claude-sonnet",
            supports_tools=True,
        )

        tracker.record_failure(profile)

        self.assertEqual(tracker.failures[AGENT_CLAUDE], 1)
        self.assertEqual(list(tracker.failures.keys()), [AGENT_CLAUDE])
        record_metric.assert_called_once_with(AGENT_CLAUDE)

    def test_delegation_format_omits_priority_when_normal(self):
        """Verifica que delegation format omits priority when normal."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        delegation = {
            "task": "Revisar código",
            "priority": "normal",
            "delegation_id": "xyz789",
        }
        fields = DelegatePresenter.present(delegation, from_agent="codex")
        self.assertEqual(fields["delegation_id"], "xyz789")
        self.assertEqual(fields["delegation_priority"], "")

    def test_retry_on_none_response(self):
        """Verifica que retry on none response."""
        app = QuimeraApp.__new__(QuimeraApp)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool(["claude"])
        app.active_agents = ["claude"]
        app.session_metrics = SessionMetricsService()
        app.session_state = {}
        call_count = [0]

        def fake_delegate(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 2:
                return None
            return "sucesso no retry"

        dispatch = dispatch_services_from_app(app)
        dispatch.delegate_low_level = fake_delegate
        dispatch.resolve_agent_response = lambda agent, response, silent=False, persist_history=True, show_output=True: response

        result = dispatch.delegate("claude")
        self.assertEqual(result, "sucesso no retry")
        self.assertEqual(call_count[0], 2)

    def test_delegate_low_level_always_skips_tool_prompt_for_cli_builtin_tools(self):
        """Verifica que call agent low level always skips tool prompt for cli builtin tools."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.session_call_index = 0
        app.history = [{"role": "human", "content": "Pedido atual"}]
        app.shared_state = {}
        app.round_index = 0
        app.debug_prompt_metrics = False
        app.task_services = Mock()
        app.task_services.refresh_task_shared_state = Mock()
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = "PROMPT"
        app.agent_client = Mock()
        app.agent_client.call.return_value = "resposta"
        app.renderer = DummyRenderer()
        app.session_state = {"session_id": "sessao-teste"}
        app.get_agent_profile = Mock(return_value=ExecutionProfile(
            name="codex-cli",
            prefix="/codex-cli",
            style=("blue", "Codex CLI"),
            cmd=["codex"],
            driver="cli",
            supports_tools=True,
            has_builtin_tools=True,
        ))

        dispatch = dispatch_services_from_app(app)
        result = dispatch.delegate_low_level("codex-cli")

        self.assertEqual(result, "resposta")
        self.assertTrue(app.prompt_builder.build.call_args.kwargs["skip_tool_prompt"])

    def test_delegate_low_level_always_skips_tool_prompt_for_openai_compat(self):
        """Verifica que call agent low level always skips tool prompt for openai compat."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.session_call_index = 0
        app.history = [{"role": "human", "content": "Pedido atual"}]
        app.shared_state = {}
        app.round_index = 0
        app.debug_prompt_metrics = False
        app.task_services = Mock()
        app.task_services.refresh_task_shared_state = Mock()
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = "PROMPT"
        app.agent_client = Mock()
        app.agent_client.call.return_value = "resposta"
        app.renderer = DummyRenderer()
        app.session_state = {"session_id": "sessao-teste"}
        app.get_agent_profile = Mock(return_value=ExecutionProfile(
            name="chatgpt-api",
            prefix="/chatgpt-api",
            style=("yellow", "ChatGPT API"),
            driver="openai_compat",
            model="gpt-4o",
            base_url="http://localhost:5532/v1",
            api_key_env="OPENAI_API_KEY",
            supports_tools=True,
            has_builtin_tools=True,
        ))

        dispatch = dispatch_services_from_app(app)
        result = dispatch.delegate_low_level("chatgpt-api")

        self.assertEqual(result, "resposta")
        self.assertTrue(app.prompt_builder.build.call_args.kwargs["skip_tool_prompt"])

    def test_delegate_low_level_keeps_history_in_delegation_only_mode(self):
        """Verifica que call agent low level keeps history in delegation only mode."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.session_call_index = 0
        app.history = [
            {"role": "codex", "content": "Eu já estava investigando esse ponto."},
            {"role": "human", "content": "Continue daí."},
        ]
        app.shared_state = {}
        app.round_index = 0
        app.debug_prompt_metrics = False
        app.task_services = Mock()
        app.task_services.refresh_task_shared_state = Mock()
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = "PROMPT"
        app.agent_client = Mock()
        app.agent_client.call.return_value = "resposta"
        app.renderer = DummyRenderer()
        app.session_state = {"session_id": "sessao-teste"}
        app.get_agent_profile = Mock(return_value=ExecutionProfile(
            name="codex",
            prefix="/codex",
            style=("blue", "Codex"),
            cmd=["codex"],
            driver="cli",
            supports_tools=True,
            has_builtin_tools=True,
        ))

        dispatch = dispatch_services_from_app(app)
        result = dispatch.delegate_low_level(
            "codex",
            delegation={"task": "Continue a investigação"},
            delegation_only=True,
            primary=False,
        )

        self.assertEqual(result, "resposta")
        self.assertEqual(app.prompt_builder.build.call_args.args[1], app.history)
        self.assertTrue(app.prompt_builder.build.call_args.kwargs["delegation_only"])

    def test_delegate_low_level_streaming_respects_show_output_false(self):
        """Verifica que call agent low level streaming respects show output false."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.session_call_index = 0
        app.history = [{"role": "human", "content": "Pedido atual"}]
        app.shared_state = {}
        app.round_index = 0
        app.debug_prompt_metrics = False
        app.task_services = Mock()
        app.task_services.refresh_task_shared_state = Mock()
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = "PROMPT"
        app.renderer = Mock()
        app.session_state = {"session_id": "sessao-teste"}
        app.get_agent_profile = Mock(return_value=ExecutionProfile(
            name="chatgpt-api",
            prefix="/chatgpt-api",
            style=("yellow", "ChatGPT API"),
            driver="openai_compat",
            model="gpt-4o",
            base_url="http://localhost:5532/v1",
            api_key_env="OPENAI_API_KEY",
            supports_tools=True,
            has_builtin_tools=True,
        ))

        def fake_call(agent, prompt, silent=False, on_text_chunk=None, progress_callback=None, from_agent=None):
            del from_agent
            self.assertIsNotNone(on_text_chunk)
            on_text_chunk("parcial")
            return "resposta final"

        app.agent_client = Mock()
        app.agent_client.call.side_effect = fake_call

        dispatch = dispatch_services_from_app(app)
        result = dispatch.delegate_low_level("chatgpt-api", show_output=False)

        self.assertEqual(result, "resposta final")
        app.renderer.start_message_stream.assert_not_called()
        app.renderer.update_message_stream.assert_not_called()
        app.renderer.finish_message_stream.assert_not_called()

    def test_delegate_low_level_exports_last_spy_turn_detail_to_state(self):
        """Verifica que call agent low level exports last spy turn detail to state."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.session_call_index = 0
        app.history = [{"role": "human", "content": "Pedido atual"}]
        app.shared_state = {}
        app.round_index = 0
        app.debug_prompt_metrics = False
        app.task_services = Mock()
        app.task_services.refresh_task_shared_state = Mock()
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = "PROMPT"
        app.renderer = Mock()
        app.session_state = {"session_id": "sessao-teste"}
        app.get_agent_profile = Mock(return_value=ExecutionProfile(
            name="codex-cli",
            prefix="/codex-cli",
            style=("blue", "Codex CLI"),
            cmd=["codex"],
            driver="cli",
            supports_tools=True,
            has_builtin_tools=True,
        ))

        app.agent_client = Mock()
        app.agent_client.call.return_value = "resposta final"
        app.agent_client.last_spy_turn_detail = {
            "turn_id": "turn_0001",
            "tools": [
                {
                    "tool_call_id": "t_0001",
                    "tool": "exec_command",
                    "status": "ok",
                    "started_at": "2026-04-30T10:21:03+00:00",
                    "ended_at": "2026-04-30T10:21:05+00:00",
                    "duration_ms": 1820,
                    "input": {"cmd": "pytest -q"},
                }
            ],
        }

        dispatch = dispatch_services_from_app(app)
        result = dispatch.delegate_low_level("codex-cli")

        self.assertEqual(result, "resposta final")
        self.assertIn("spy_last_turn_detail", app.shared_state)
        exported = app.shared_state["spy_last_turn_detail"]
        self.assertEqual(exported["agent"], "codex-cli")
        self.assertEqual(exported["turn_detail"]["turn_id"], "turn_0001")
        self.assertEqual(
            app.session_state["last_spy_turn_detail"]["turn_detail"]["tools"][0]["tool"],
            "exec_command",
        )

    def test_sanitize_spy_turn_detail_preserves_full_payload(self):
        """Verifica que sanitize spy turn detail preserves full payload."""
        long_text = "x" * 500
        detail = {
            "turn_id": "turn_1234",
            "tools": [
                {
                    "tool_call_id": f"tool_{i}",
                    "tool": "exec_command",
                    "status": "ok",
                    "started_at": "2026-04-30T10:21:03+00:00",
                    "ended_at": "2026-04-30T10:21:05+00:00",
                    "duration_ms": 2000,
                    "input": {"cmd": long_text, "path": "/tmp/example"},
                    "output_meta": {"preview": long_text},
                    "error": {"type": "ToolError", "message": long_text, "stack": long_text},
                }
                for i in range(20)
            ],
        }
        sanitized = AppDispatchServices._sanitize_spy_turn_detail(detail)

        self.assertIsNotNone(sanitized)
        self.assertEqual(len(sanitized["tools"]), 20)
        self.assertFalse(sanitized["truncated_tools"])
        cmd = sanitized["tools"][0]["input"]["cmd"]
        self.assertEqual(cmd, long_text)
        self.assertEqual(set(sanitized["tools"][0]["error"].keys()), {"type", "message", "stack"})

    def test_task_handler_prints_and_persists_agent_response(self):
        """Verifica que task handler prints and persists agent response."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE])
        app.active_agents = [AGENT_CLAUDE]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        app.dispatch_services.delegate = lambda *args, **kwargs: "resposta visivel da task"
        app.system_layer.show_system_message = lambda message: status_updates.append(message)
        app.system_layer.show_muted_message = lambda message: status_updates.append(message)
        app.classify_task_execution_result = lambda response: (True, response)

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.tasks.repository.TaskRepository.complete_task"
        ) as complete_task, patch("quimera.tasks.repository.TaskRepository.fail_task") as fail_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE](TaskRecord(id=1, job_id=0, description="rode a task", status="in_progress"))

        self.assertTrue(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 1] claude: iniciando — rode a task",
                "[task 1] claude:\nresposta visivel da task",
                "[task 1] claude: concluída",
            ],
        )
        complete_task.assert_called_once_with(
            1, result="resposta visivel da task"
        )
        fail_task.assert_not_called()

    def test_task_handler_marks_task_waiting_for_review_from_another_agent(self):
        """Verifica que task handler marks task waiting for review from another agent."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_GEMINI])
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        app.dispatch_services.delegate = lambda *args, **kwargs: "resposta visivel da task"
        app.system_layer.show_system_message = lambda message: status_updates.append(message)
        app.system_layer.show_muted_message = lambda message: status_updates.append(message)
        app.classify_task_execution_result = lambda response: (True, response)

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.tasks.repository.TaskRepository.submit_for_review"
        ) as submit_for_review, patch("quimera.tasks.repository.TaskRepository.complete_task") as complete_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE](TaskRecord(id=1, job_id=0, description="rode a task", status="in_progress"))

        self.assertTrue(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 1] claude: iniciando — rode a task",
                "[task 1] claude:\nresposta visivel da task",
                "[task 1] claude: aguardando review de outro agente",
            ],
        )
        submit_for_review.assert_called_once_with(
            1, result="resposta visivel da task"
        )
        complete_task.assert_not_called()

    def test_task_handler_completes_when_no_other_operational_reviewer_exists(self):
        """Verifica que task handler completes when no other operational reviewer exists."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_GEMINI])
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        class FakeProfile:
            def __init__(self, supports_task_execution):
                self.supports_task_execution = supports_task_execution

        app.dispatch_services.delegate = lambda *args, **kwargs: "resposta visivel da task"
        app.system_layer.show_system_message = lambda message: status_updates.append(message)
        app.system_layer.show_muted_message = lambda message: status_updates.append(message)
        app.classify_task_execution_result = lambda response: (True, response)

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.app.core.profiles.get",
                side_effect=lambda agent: FakeProfile(agent == AGENT_CLAUDE),
        ), patch("quimera.tasks.repository.TaskRepository.submit_for_review") as submit_for_review, patch(
            "quimera.tasks.repository.TaskRepository.complete_task"
        ) as complete_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE](TaskRecord(id=11, job_id=0, description="rode a task", status="in_progress"))

        self.assertTrue(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 11] claude: iniciando — rode a task",
                "[task 11] claude:\nresposta visivel da task",
                "[task 11] claude: concluída",
            ],
        )
        submit_for_review.assert_not_called()
        complete_task.assert_called_once_with(
            11, result="resposta visivel da task"
        )

    def test_review_handler_prints_review_progress_and_completion(self):
        """Verifica que review handler prints review progress and completion."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_GEMINI])
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        review_handlers = {}
        review_prompts = []

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        def fake_delegate(agent, **kwargs):
            review_prompts.append(kwargs.get("delegation", ""))
            return "ACEITE\nResultado validado com evidência concreta."

        app.dispatch_services.delegate = fake_delegate
        app.system_layer.show_system_message = lambda message: status_updates.append(message)
        app.system_layer.show_muted_message = lambda message: status_updates.append(message)

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.tasks.repository.TaskRepository.complete_task"
        ) as complete_task:
            app._setup_task_executors()
            ok = review_handlers[AGENT_GEMINI](
                TaskRecord(id=7, job_id=0, description="", status="reviewing", assigned_to=AGENT_CLAUDE, result="ok")
            )

        self.assertTrue(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 7] antigravity: revisando execução de claude",
                "[task 7] antigravity:\nACEITE\nResultado validado com evidência concreta.",
                "[task 7] antigravity: review concluído",
            ],
        )
        self.assertTrue(review_prompts)
        self.assertEqual(review_prompts[0]["delegation_id"], "task-review-7")
        self.assertIn("Resultado do executor:\nok", review_prompts[0]["context"])
        complete_task.assert_called_once_with(
            7, result="ok", reviewed_by=AGENT_GEMINI
        )

    def test_review_handler_reports_rejected_self_review(self):
        """Verifica que review handler reports rejected self review."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_GEMINI])
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        review_handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        app.dispatch_services.delegate = lambda *args, **kwargs: None
        app.system_layer.show_system_message = lambda message: status_updates.append(message)
        app.system_layer.show_muted_message = lambda message: status_updates.append(message)

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.tasks.repository.TaskRepository.transition_task"
        ) as transition_task, patch("quimera.tasks.repository.TaskRepository.complete_task") as complete_task:
            app._setup_task_executors()
            ok = review_handlers[AGENT_CLAUDE](
                TaskRecord(id=8, job_id=0, description="", status="reviewing", assigned_to=AGENT_CLAUDE, result="ok")
            )

        self.assertFalse(ok)
        self.assertEqual(
            status_updates,
            ["[task 8] claude: review rejeitado, aguardando outro agente"],
        )
        transition_task.assert_called_once_with(8, TaskStatus.PENDING_REVIEW, result='ok', notes=None)
        complete_task.assert_not_called()

    def test_review_handler_returns_task_to_pending_on_retentativa(self):
        """Verifica que review handler returns task to pending on retentativa."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_GEMINI])
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        review_handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        app.dispatch_services.delegate = lambda *args, **kwargs: "RETENTATIVA\nFaltou evidência de alteração no código."
        app.system_layer.show_system_message = lambda message: status_updates.append(message)
        app.system_layer.show_muted_message = lambda message: status_updates.append(message)

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.tasks.repository.TaskRepository.requeue_task_after_review"
        ) as requeue_task_after_review, patch("quimera.tasks.repository.TaskRepository.complete_task") as complete_task:
            app._setup_task_executors()
            ok = review_handlers[AGENT_GEMINI](
                TaskRecord(id=9, job_id=0, description="corrigir bug x", body="ajuste o fluxo y",
                           status="reviewing", assigned_to=AGENT_CLAUDE, result="ok")
            )

        self.assertFalse(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 9] antigravity: revisando execução de claude",
                "[task 9] antigravity:\nRETENTATIVA\nFaltou evidência de alteração no código.",
                "[task 9] antigravity: review pediu retentativa, task voltou para pending",
            ],
        )
        requeue_task_after_review.assert_called_once_with(
            9,
            AGENT_CLAUDE,
            result="ok",
            notes="RETENTATIVA\nFaltou evidência de alteração no código.",
        )
        complete_task.assert_not_called()

    def test_task_completed_event_shows_consolidated_feedback_to_human(self):
        """Verifica que task completed event shows consolidated feedback to human."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        status_updates = []
        app.event_sink = EventSink()
        app.show_muted_message = lambda message: status_updates.append(message)
        from quimera.app.ui_event_handler import UiEventHandler
        app._ui_event_handler = UiEventHandler(
            renderer=app.renderer,
            input_gate=getattr(app, "input_gate", None),
            runtime_state=getattr(app, "runtime_state", None),
            system_layer=app.system_layer,
            event_sink=app.event_sink,
            show_muted_message=app.show_muted_message,
            show_system_message=app.system_layer.show_system_message,
            show_warning_message=app.system_layer.show_warning_message,
            show_error_message=app.system_layer.show_error_message,
            redisplay_user_prompt=getattr(app, "_redisplay_user_prompt_if_needed", lambda: None),
            output_lock=app._output_lock,
        )

        app._wire_event_ui()
        app.event_sink.publish(TaskCompleted(
            task_id=252,
            job_id=1,
            result="ACEITE\nCommit criado com hash abc123 e worktree limpo.",
            reviewed_by=AGENT_GEMINI,
        ))

        self.assertEqual(
            status_updates,
            [
                "[task 252] concluída | aprovada por antigravity: ACEITE\nCommit criado com hash abc123 e worktree limpo.",
            ],
        )

    def test_review_handler_returns_task_to_pending_review_on_failure(self):
        """Verifica que review handler returns task to pending review on failure."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_GEMINI, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI, AGENT_CODEX]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        review_handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        def fake_delegate(*_args, **_kwargs):
            raise RuntimeError("timeout")

        class FakeProfile:
            def __init__(self, supports_task_execution):
                self.supports_task_execution = supports_task_execution

        app.dispatch_services.delegate = fake_delegate
        app.system_layer.show_system_message = lambda message: status_updates.append(message)
        app.system_layer.show_muted_message = lambda message: status_updates.append(message)

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.app.core.profiles.get",
                side_effect=lambda _agent: FakeProfile(True),
        ), patch("quimera.tasks.repository.TaskRepository.transition_task") as transition_task, patch(
            "quimera.tasks.repository.TaskRepository.fail_task"
        ) as fail_task:
            app._setup_task_executors()
            ok = review_handlers[AGENT_GEMINI](
                TaskRecord(id=10, job_id=0, description="", status="reviewing", assigned_to=AGENT_CLAUDE, result="ok")
            )

        self.assertFalse(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 10] antigravity: revisando execução de claude",
                "[task 10] antigravity: review falhou: timeout",
            ],
        )
        transition_task.assert_called_once_with(
            10,
            TaskStatus.PENDING_REVIEW,
            result="ok",
            notes="timeout",
        )
        fail_task.assert_not_called()

    def test_review_handler_fails_when_pending_review_transition_fails(self):
        """Verifica que review handler fails when pending review transition fails."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_GEMINI, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI, AGENT_CODEX]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        review_handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        def fake_delegate(*_args, **_kwargs):
            raise RuntimeError("timeout")

        class FakeProfile:
            def __init__(self, supports_task_execution):
                self.supports_task_execution = supports_task_execution

        app.dispatch_services.delegate = fake_delegate
        app.system_layer.show_system_message = lambda message: status_updates.append(message)
        app.system_layer.show_muted_message = lambda message: status_updates.append(message)

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.app.core.profiles.get",
                side_effect=lambda _agent: FakeProfile(True),
        ), patch("quimera.tasks.repository.TaskRepository.transition_task") as transition_task, patch(
            "quimera.tasks.repository.TaskRepository.fail_task"
        ) as fail_task:
            transition_task.return_value = False
            app._setup_task_executors()
            ok = review_handlers[AGENT_GEMINI](
                TaskRecord(id=12, job_id=0, description="", status="reviewing", assigned_to=AGENT_CLAUDE, result="ok")
            )

        self.assertFalse(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 12] antigravity: revisando execução de claude",
                "[task 12] antigravity: review falhou: timeout",
            ],
        )
        transition_task.assert_called_once_with(
            12,
            TaskStatus.PENDING_REVIEW,
            result="ok",
            notes="timeout",
        )
        fail_task.assert_called_once_with(
            12,
            reason="review failed and fallback transition failed: timeout",
        )

    def test_review_handler_fails_when_no_other_operational_reviewer_exists(self):
        """Verifica que review handler fails when no other operational reviewer exists."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_GEMINI])
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        review_handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def set_review_eligibility(self, predicate):
                return None

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        class FakeProfile:
            def __init__(self, supports_task_execution):
                self.supports_task_execution = supports_task_execution

        def fake_delegate(*_args, **_kwargs):
            raise RuntimeError("timeout")

        app.dispatch_services.delegate = fake_delegate
        app.system_layer.show_system_message = lambda message: status_updates.append(message)
        app.system_layer.show_muted_message = lambda message: status_updates.append(message)

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.app.core.profiles.get",
                side_effect=lambda agent: FakeProfile(agent == AGENT_GEMINI),
        ), patch("quimera.tasks.repository.TaskRepository.transition_task") as update_task, patch(
            "quimera.tasks.repository.TaskRepository.fail_task"
        ) as fail_task:
            app._setup_task_executors()
            ok = review_handlers[AGENT_GEMINI](
                TaskRecord(id=11, job_id=0, description="", status="reviewing", assigned_to=AGENT_CLAUDE, result="ok")
            )

        self.assertFalse(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 11] antigravity: revisando execução de claude",
                "[task 11] antigravity: review falhou: timeout",
            ],
        )
        update_task.assert_not_called()
        fail_task.assert_called_once_with(
            11,
            reason="review failed without operational fallback: timeout",
        )

    def test_setup_task_executors_only_registers_review_for_operational_agents(self):
        """Verifica que setup task executors only registers review for operational agents."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_GEMINI])
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        review_handlers = {}
        review_eligibility = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def set_review_eligibility(self, predicate):
                review_eligibility[self.agent] = predicate

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        class FakeProfile:
            def __init__(self, supports_task_execution):
                self.supports_task_execution = supports_task_execution

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.app.core.profiles.get",
                side_effect=lambda agent: FakeProfile(agent == AGENT_GEMINI),
        ):
            app._setup_task_executors()
            self.assertFalse(review_eligibility[AGENT_CLAUDE]())
            self.assertTrue(review_eligibility[AGENT_GEMINI]())

        self.assertNotIn(AGENT_CLAUDE, review_handlers)
        self.assertIn(AGENT_GEMINI, review_handlers)

    def test_review_eligibility_tracks_operational_agent_state_dynamically(self):
        """Verifica que review eligibility tracks operational agent state dynamically."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_GEMINI])
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        review_eligibility = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                return None

            def set_review_eligibility(self, predicate):
                review_eligibility[self.agent] = predicate

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        class FakeProfile:
            def __init__(self, supports_task_execution):
                self.supports_task_execution = supports_task_execution

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.app.core.profiles.get",
                side_effect=lambda agent: FakeProfile(True),
        ):
            app._setup_task_executors()
            self.assertTrue(review_eligibility[AGENT_GEMINI]())
            app.active_agents.remove(AGENT_GEMINI)
            self.assertFalse(review_eligibility[AGENT_GEMINI]())

    def test_task_handler_executes_with_serialized_chat_context_in_body(self):
        """Verifica que task handler executes with serialized chat context in body."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE])
        app.active_agents = [AGENT_CLAUDE]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        handlers = {}
        captured = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        def fake_delegate(agent, **kwargs):
            captured["agent"] = agent
            captured["kwargs"] = kwargs
            return "resposta visivel da task"

        app.dispatch_services.delegate = fake_delegate
        app.system_layer.show_system_message = lambda message: None
        app.system_layer.show_muted_message = lambda message: None
        app.classify_task_execution_result = lambda response: (True, response)

        task_body = (
            "TAREFA:\nvalidar regressão\n\n"
            "CONTEXTO DA TASK (sanitizado):\n"
            "[ALEX]: a execução da tarefa precisa receber o contexto da task\n"
            "[CLAUDE]: alguém passou contexto errado\n\n"
            "INSTRUÇÃO:\n"
            "Execute a tarefa usando o contexto acima como referência."
        )

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.tasks.repository.TaskRepository.complete_task"
        ) as complete_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE](
                TaskRecord(id=1, job_id=0, description="validar regressão", body=task_body, status="in_progress")
            )

        self.assertTrue(ok)
        self.assertEqual(captured["agent"], AGENT_CLAUDE)
        self.assertTrue(captured["kwargs"]["delegation_only"])
        self.assertFalse(captured["kwargs"]["primary"])
        self.assertEqual(captured["kwargs"]["prompt_kind"], "task_executor")
        self.assertEqual(captured["kwargs"]["delegation"]["delegation_id"], "task-1")
        self.assertEqual(captured["kwargs"]["delegation"]["task"], "validar regressão")
        self.assertIn("TAREFA:\nvalidar regressão", captured["kwargs"]["delegation"]["context"])
        self.assertIn("[ALEX]: a execução da tarefa precisa receber o contexto da task", captured["kwargs"]["delegation"]["context"])
        self.assertIn("[CLAUDE]: alguém passou contexto errado", captured["kwargs"]["delegation"]["context"])
        complete_task.assert_called_once_with(
            1, result="resposta visivel da task"
        )

    def test_task_handler_requeues_failed_execution_for_other_agent(self):
        """Verifica que task handler requeues failed execution for other agent."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        app.dispatch_services.delegate = lambda *args, **kwargs: None
        app.system_layer.show_system_message = lambda message: None
        app.system_layer.show_muted_message = lambda message: None
        app.classify_task_execution_result = lambda response: (True, response)
        app.record_failure = lambda agent: None

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.tasks.repository.TaskRepository.can_reassign_task", return_value=True
        ), patch("quimera.tasks.repository.TaskRepository.requeue_task") as requeue_task, patch(
            "quimera.tasks.repository.TaskRepository.fail_task"
        ) as fail_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE](TaskRecord(id=1, job_id=0, description="rode a task", status="in_progress"))

        self.assertFalse(ok)
        requeue_task.assert_called_once_with(
            1, AGENT_CLAUDE, reason="communication failed"
        )
        fail_task.assert_not_called()

    def test_task_handler_fails_when_all_other_agents_already_failed(self):
        """Verifica que task handler fails when all other agents already failed."""
        app = QuimeraApp.__new__(QuimeraApp)
        materialize_internal_services(app)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None, repository=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        app.dispatch_services.delegate = lambda *args, **kwargs: None
        app.system_layer.show_system_message = lambda message: None
        app.system_layer.show_muted_message = lambda message: None
        app.classify_task_execution_result = lambda response: (True, response)
        app.record_failure = lambda agent: None

        with patch("quimera.app.bootstrap.wiring.create_executor", side_effect=fake_create_executor), patch(
                "quimera.tasks.repository.TaskRepository.can_reassign_task", return_value=False
        ) as can_reassign_task, patch("quimera.tasks.repository.TaskRepository.requeue_task") as requeue_task, patch(
            "quimera.tasks.repository.TaskRepository.fail_task"
        ) as fail_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE](TaskRecord(id=1, job_id=0, description="rode a task", status="in_progress"))

        self.assertFalse(ok)
        can_reassign_task.assert_called_once_with(
            1, [AGENT_CODEX]
        )
        requeue_task.assert_not_called()
        fail_task.assert_called_once_with(1, reason="communication failed")

    def test_per_agent_metrics_tracking(self):
        """Test that per-agent metrics are tracked correctly."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.session_state = {
            "session_id": "test",
            "history_count": 0,
            "summary_loaded": False,
            "delegations_sent": 0,
            "delegations_received": 0,
            "delegations_succeeded": 0,
            "delegations_failed": 0,
            "total_latency": 0.0,
            "agent_metrics": {},
        }
        app.session_metrics = SessionMetricsService()

        # Simulate successful call to claude
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 1.5)
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 0.8)

        # Simulate failed call to codex
        app.session_metrics.record_agent_metric(app, "codex", "failed", 0.0)

        metrics = app.session_state["agent_metrics"]
        self.assertEqual(metrics["claude"]["succeeded"], 2)
        self.assertEqual(metrics["claude"]["latency"], 2.3)
        self.assertEqual(metrics["codex"]["failed"], 1)
        self.assertEqual(metrics["codex"]["succeeded"], 0)

    def test_per_agent_tool_metrics_tracking(self):
        """Tool use deve ser rastreado por agente na sessão."""
        from quimera.metrics import BehaviorMetricsTracker

        app = QuimeraApp.__new__(QuimeraApp)
        app.session_state = {
            "session_id": "test",
            "history_count": 0,
            "summary_loaded": False,
            "delegations_sent": 0,
            "delegations_received": 0,
            "delegations_succeeded": 0,
            "delegations_failed": 0,
            "total_latency": 0.0,
            "agent_metrics": {},
        }
        app.behavior_metrics = BehaviorMetricsTracker()
        app.session_metrics = SessionMetricsService()

        app._record_tool_event("ollama-qwen", result=SimpleNamespace(ok=True, error=None))
        app._record_tool_event("ollama-qwen",
                               result=SimpleNamespace(ok=False, error="Sem política para a ferramenta: run"))
        app._record_tool_event("ollama-qwen", loop_abort=True, reason="invalid_tool_loop")

        metrics = app.session_state["agent_metrics"]["ollama-qwen"]
        self.assertEqual(metrics["tool_calls_total"], 2)
        self.assertEqual(metrics["tool_calls_failed"], 1)
        self.assertEqual(metrics["invalid_tool_calls"], 1)
        self.assertEqual(metrics["tool_loop_abortions"], 1)
        self.assertEqual(metrics["tool_errors_by_type"]["policy"], 1)
        self.assertEqual(metrics["tool_loop_abort_reasons"]["invalid_tool_loop"], 1)

        summary = app.behavior_metrics.get_agent_summary("ollama-qwen")
        self.assertEqual(summary["tool_calls_total"], 2)
        self.assertEqual(summary["tool_calls_failed"], 1)
        self.assertEqual(summary["invalid_tool_calls"], 1)
        self.assertEqual(summary["tool_loop_abortions"], 1)
        self.assertEqual(summary["tool_errors_by_type"]["policy"], 1)
        self.assertEqual(summary["tool_loop_abort_reasons"]["invalid_tool_loop"], 1)

    def test_resolve_agent_response_leaves_textual_tool_like_content_untouched(self):
        """Verifica que resolve agent response leaves textual tool like content untouched."""
        from quimera.app.dispatch import AppDispatchServices

        app = Mock()
        app.tool_executor.execute = Mock(side_effect=AssertionError("text must not execute tools"))
        dispatch = dispatch_services_from_app(app)

        response = 'Resposta <tool function="read_file" path="secret.txt" /> final'
        result = dispatch.resolve_agent_response("codex", response, show_output=False)

        self.assertEqual(result, response)
        app.tool_executor.execute.assert_not_called()

    def test_route_rule_is_removed_from_template(self):
        """Route rule genérica não deve estar inline no template principal."""
        main = prompt_template._load()

        self.assertIn("delegate", main)
        self.assertIn("target_agent", main)
        self.assertIn("request", main)
        self.assertIn("obrigatório", main)
        self.assertNotIn("não improvise", main)
        self.assertNotIn("Agentes: {route_agents}", main)


class FallbackChainTests(unittest.TestCase):
    """Testes para fallback chain quando agente secundário falha."""

    def test_first_agent_failover_to_another_agent(self):
        """Quando o primeiro agente não responde, outro agente deve assumir a rodada."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.user_name = "Você"
        app.round_index = 0
        app.session_call_index = 0
        app.debug_prompt_metrics = False
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = DummyAgentClient()
        app.prompt_builder = None
        app.summary_agent_preference = None
        app.session_state = {
            "session_id": "test-first-agent-failover",
            "history_count": 0,
            "summary_loaded": False,
        }
        printed = []
        persisted = []
        calls = []

        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.threads = 1
        app.shared_state = {}
        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content, **kwargs: persisted.append((role, content))

        materialize_internal_services(app)

        app.dispatch_services.print_response = lambda agent, response: printed.append((agent, response))

        responses = iter([
            None,
            "codex assumiu e respondeu",
        ])

        def fake_call(
                agent,
                is_first_speaker=False,
                delegation=None,
                primary=True,
                protocol_mode="standard",
                delegation_only=False,
                from_agent=None,
                prompt_kind=None,
                **kwargs,
        ):
            calls.append((agent, is_first_speaker, delegation, delegation_only, from_agent))
            return next(responses)

        app.dispatch_services.delegate = fake_call

        QuimeraApp._do_process_chat_message(app, "oi")

        self.assertEqual(calls[0][0], AGENT_CLAUDE)
        self.assertEqual(calls[1][0], AGENT_CODEX)
        self.assertTrue(calls[1][1])
        self.assertIn((AGENT_CODEX, "codex assumiu e respondeu"), printed)
        self.assertIn((AGENT_CODEX, "codex assumiu e respondeu"), persisted)
        self.assertEqual(app.summary_agent_preference, AGENT_CODEX)

    def test_no_fallback_when_no_candidates(self):
        """Se não há candidatos de fallback, o sistema não deve tentar."""
        app = QuimeraApp.__new__(QuimeraApp)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool(["claude", "codex"])
        app.active_agents = ["claude", "codex"]
        chain = ["claude"]
        route_target = "codex"
        first_agent = "claude"

        fallback_candidates = [
            a for a in app.active_agents
            if a != first_agent and a != route_target and a not in chain
        ]

        self.assertEqual(fallback_candidates, [])


class MetricsFeedbackTests(unittest.TestCase):
    """Testes para métricas e feedback operacional."""

    def test_has_clear_next_step_detects_clear_indicators(self):
        """SessionMetricsService.has_clear_next_step deve detectar indicadores de próximo passo."""
        self.assertTrue(SessionMetricsService.has_clear_next_step("Próximo passo: revisar o código."))
        self.assertTrue(SessionMetricsService.has_clear_next_step("Próxima etapa: implementar a feature."))
        self.assertTrue(SessionMetricsService.has_clear_next_step("Tarefa completa."))
        self.assertTrue(SessionMetricsService.has_clear_next_step("Concluído."))
        self.assertFalse(SessionMetricsService.has_clear_next_step("Apenas uma resposta qualquer."))
        self.assertFalse(SessionMetricsService.has_clear_next_step(""))

    def test_is_response_redundant_detects_similarity(self):
        """SessionMetricsService.is_response_redundant deve detectar respostas similares."""
        history = [
            {"role": "human", "content": "Faça algo"},
            {"role": "claude", "content": "Vou implementar a feature X agora. Isso envolve criar o arquivo e testar."},
        ]

        similar_response = "Vou implementar a feature X agora. Isso envolve criar o arquivo e testar."
        self.assertTrue(SessionMetricsService.is_response_redundant(similar_response, history))

        different_response = "Vou corrigir o bug Y no parser."
        self.assertFalse(SessionMetricsService.is_response_redundant(different_response, history))

    def test_session_state_tracks_new_metrics(self):
        """session_state deve rastrear as novas métricas."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.session_state = {}

        app.session_state["total_responses"] = 5
        app.session_state["responses_with_clear_next_step"] = 3
        app.session_state["consecutive_redundant_responses"] = 2
        app.session_state["delegation_invalid_count"] = 1
        app.session_state["rounds_without_progress"] = 0

        self.assertEqual(app.session_state["total_responses"], 5)
        self.assertEqual(app.session_state["responses_with_clear_next_step"], 3)
        self.assertEqual(app.session_state["consecutive_redundant_responses"], 2)
        self.assertEqual(app.session_state["delegation_invalid_count"], 1)

    def test_delegation_format_includes_chain(self):
        """Delegation format deve incluir cadeia de delegação quando presente."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        delegation = {
            "task": "Revisar parser",
            "context": "Parser quebrado",
            "chain": ["claude", "codex"],
            "delegation_id": "abc123",
        }
        fields = DelegatePresenter.present(delegation, from_agent="qwen")
        self.assertEqual(fields["delegation_chain"], "claude -> codex")
        self.assertEqual(fields["delegation_id"], "abc123")
        self.assertEqual(fields["delegation_from"], "qwen")

    def test_delegation_format_omits_chain_when_empty(self):
        """Delegation format não deve incluir CHAIN quando vazio."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        delegation = {
            "task": "Tarefa simples",
            "chain": [],
            "delegation_id": "xyz",
        }
        fields = DelegatePresenter.present(delegation, from_agent="claude")
        self.assertEqual(fields["delegation_chain"], "")

    def test_prompt_includes_collaboration_rules(self):
        """Prompt deve incluir regras de colaboração."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CLAUDE, history, is_first_speaker=True)

        self.assertIn("prioridade", prompt.lower())
        self.assertIn("foco", prompt.lower())
        self.assertIn("fazem parte deste chat", prompt.lower())

    def test_prompt_is_concise(self):
        """Prompt deve ser conciso após enxugamento."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CLAUDE, history, is_first_speaker=True)

        self.assertLess(len(prompt), 6250)

    def test_get_task_routing_profiles_respects_explicit_active_agents(self):
        """Verifica que get task routing profiles respects explicit active agents."""
        app = QuimeraApp.__new__(QuimeraApp)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool([AGENT_CLAUDE, AGENT_CODEX])
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.tasks_db_path = str(Path(self.enterContext(tempfile.TemporaryDirectory())) / "tasks.db")

        selected = [profile.name for profile in build_task_services(app).get_task_routing_profiles()]

        self.assertEqual(selected, [AGENT_CLAUDE, AGENT_CODEX])

    def test_get_task_routing_profiles_expands_wildcard_to_all_profiles(self):
        """Verifica que get task routing profiles expands wildcard to all profiles."""
        app = QuimeraApp.__new__(QuimeraApp)
        from quimera.app.agent_pool import AgentPool
        app.agent_pool = AgentPool(["*"])
        app.active_agents = ["*"]
        app.tasks_db_path = str(Path(self.enterContext(tempfile.TemporaryDirectory())) / "tasks.db")

        selected = [profile.name for profile in build_task_services(app).get_task_routing_profiles()]

        self.assertEqual(
            selected,
            [profile.name for profile in profiles.all_profiles() if getattr(profile, "supports_task_execution", True)],
        )

    def test_delegation_rule_mentions_ack(self):
        """DELEGATION_RULE deve estar inline no template e mencionar ACK."""
        main = prompt_template._load()
        self.assertIn("ACK", main)
        self.assertIn("delegate", main)
        self.assertIn("arquivos", main)

    def test_behavior_metrics_tracker_integrated_with_app(self):
        """BehaviorMetricsTracker deve ser alimentado pelo app."""
        from quimera.metrics import BehaviorMetricsTracker

        app = QuimeraApp.__new__(QuimeraApp)
        app.session_state = {
            "session_id": "test",
            "history_count": 0,
            "summary_loaded": False,
            "delegations_sent": 0,
            "delegations_received": 0,
            "delegations_succeeded": 0,
            "delegations_failed": 0,
            "total_latency": 0.0,
            "agent_metrics": {},
        }
        app.behavior_metrics = BehaviorMetricsTracker()
        app.session_metrics = SessionMetricsService()

        # Simulate successful calls
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 1.5)
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 2.0)
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 1.0)
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 0.5)

        # Verifica que o tracker foi alimentado
        claude_metrics = app.behavior_metrics.get_agent_summary("claude")
        self.assertEqual(claude_metrics["responses_total"], 4)

    def test_behavior_metrics_tracks_invalid_delegation(self):
        """Delegation inválido (sem route/delegations) não deve crashar nem gerar target."""
        from quimera.metrics import BehaviorMetricsTracker

        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = _make_protocol(app)
        app.shared_state = {}
        app.behavior_metrics = BehaviorMetricsTracker()

        # Texto sem envelope JSON não é mais detectado como delegation
        response, target, delegation, extend, ack_id = app.parse_response(
            "Resposta visivel\nsem formato de delegation válido"
        )

        self.assertIsNone(target)
        self.assertIsNone(delegation)
        self.assertEqual(response, "Resposta visivel\nsem formato de delegation válido")

    def test_route_rule_removed_from_chat_prompt(self):
        """Chat prompt não deve carregar regra genérica de route_agents."""
        main = prompt_template._load()

        self.assertIn("task", main)
        self.assertIn("obrigatório", main)
        self.assertNotIn("Agentes: {route_agents}", main)
        self.assertNotIn("<!-- IF:route_agents -->", main)

    def test_reviewer_rule_is_concise(self):
        """REVIEWER_RULE deve estar inline no template e ser conciso."""
        main = prompt_template._load()
        self.assertIn("veredicto", main.lower())
        self.assertIn("ACEITE", main)

    def test_delegation_rule_is_concise(self):
        """DELEGATION_RULE deve estar inline no template e ser conciso."""
        main = prompt_template._load()
        self.assertIn("continue do ponto já avançado", main.lower())

    def test_base_rules_are_concise(self):
        """Regras base devem estar inline no template principal."""
        main = prompt_template._load().lower()
        self.assertIn("humano", main)
        self.assertIn("prioridade", main)
        self.assertIn("foco", main)
        self.assertIn("continuação direta do mesmo chat", main)


    def test_main_template_does_not_embed_static_tool_instructions_block(self):
        """Verifica que main template does not embed static tool instructions block."""
        main = prompt_template._load()
        self.assertNotIn('<tools title="Ferramentas disponíveis">', main)
        self.assertNotIn("a ferramenta não executa", main)
        self.assertNotIn("não escreva chamadas como list_files(...)", main)
        self.assertNotIn('function="apply_patch"', main)
        self.assertNotIn("exec_command / write_stdin / close_command_session", main)

    def test_build_tools_prompt_is_compact_but_preserves_essentials(self):
        """Verifica que build tools prompt is compact but preserves essentials."""
        from quimera.constants import build_tools_prompt

        prompt = build_tools_prompt()

        self.assertIn('apply_patch: patch: str', prompt)
        self.assertIn('list_files: path: str', prompt)
        self.assertIn('exec_command: cmd: str', prompt)
        self.assertIn("- list_files:", prompt)
        self.assertIn("- read_file:", prompt)
        self.assertIn("- apply_patch:", prompt)
        self.assertIn("- run_shell:", prompt)
        self.assertNotIn("| exemplo:", prompt)
        self.assertNotIn("Ferramentas disponíveis", prompt)
        self.assertNotIn("a ferramenta não executa", prompt)
        self.assertNotIn("não escreva chamadas como list_files(...)", prompt)
        self.assertLess(len(prompt), 3400)

    def test_build_task_body_includes_operational_protocol(self):
        """Verifica que build task body includes operational protocol."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.user_name = "Alex"
        app.history = [{"role": "human", "content": "Corrija o parser atual"}]
        app.shared_state = {}

        body = build_task_services(app).build_task_body("corrigir parser")

        self.assertIn("PROTOCOLO OPERACIONAL:", body)
        self.assertIn("Descubra o alvo antes de mudar", body)
        self.assertIn("apply_patch", body)
        self.assertIn("run_shell", body)
        self.assertIn("exec_command", body)
        self.assertIn("arquivos alterados", body)

    def test_build_task_body_uses_shared_state_as_reference_only(self):
        """Verifica que build task body uses shared state as reference only."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.user_name = "Alex"
        app.history = [{"role": "human", "content": "Corrija o parser atual"}]
        app.shared_state = {
            "goal_canonical": "Corrigir parser legado",
            "current_step": "Ajustar tokenizer",
            "allowed_scope": ["parser.py"],
        }

        body = build_task_services(app).build_task_body("corrigir parser")

        self.assertIn("ESTADO COMPARTILHADO (referência):", body)
        self.assertIn('"goal_canonical": "Corrigir parser legado"', body)
        self.assertNotIn("CONTEXTO DE EXECUÇÃO:", body)
        self.assertNotIn("GOAL_CANONICAL:", body)
        self.assertIn("Use o estado compartilhado apenas como referência auxiliar", body)

    def test_build_task_body_accepts_deque_history(self):
        """Verifica que build task body accepts deque history."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.user_name = "Alex"
        app.history = deque([
            {"role": "human", "content": "Corrija o parser atual"},
            {"role": "codex", "content": "Vou inspecionar o arquivo antes de editar."},
        ])
        app.shared_state = {}
        app.prompt_builder = type("PromptBuilderStub", (), {"history_window": 4})()

        body = build_task_services(app).build_task_body("corrigir parser")

        self.assertIn("ALEX]: Corrija o parser atual", body)
        self.assertIn("CODEX]: Vou inspecionar o arquivo antes de editar.", body)

    def test_prompt_builder_accepts_deque_history(self):
        """Verifica que prompt builder accepts deque history."""
        builder = PromptBuilder(DummyContextManager(), history_window=4, user_name="Alex")
        history = deque([
            {"role": "human", "content": "Primeiro pedido"},
            {"role": "claude", "content": "Resposta anterior"},
            {"role": "human", "content": "Pedido atual"},
        ])

        prompt = builder.build(AGENT_CODEX, history)

        self.assertIn('<current_turn title="Pedido atual de ALEX">', prompt)
        self.assertIn("[CLAUDE]: Resposta anterior", prompt)

    def test_refresh_task_shared_state_adds_completed_task_results(self):
        """Verifica que refresh task shared state adds completed task results."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.shared_state = {}
        app.current_job_id = 1
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        task_id = create_task(
            1,
            "validar cobertura dos testes",
            db_path=str(db_path),
            status="in_progress",
        )
        complete_task(task_id, result="ok" * 200, db_path=str(db_path))
        app.tasks_db_path = str(db_path)
        build_task_services(app).refresh_task_shared_state()

        self.assertIn("task_overview", app.shared_state)
        results = app.shared_state.get("completed_task_results", "")
        self.assertIn("[task ", results)
        self.assertIn("validar cobertura dos testes", results)
        self.assertLessEqual(len(results.split(": ", 1)[1]), 200)

    def test_refresh_task_shared_state_caps_completed_task_results_budget(self):
        """Verifica que refresh task shared state caps completed task results budget."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.shared_state = {}
        app.current_job_id = 1
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)

        for index in range(1, 11):
            task_id = create_task(
                1,
                f"task-delegation-{index:02d} com descricao longa para ocupar espaco",
                db_path=str(db_path),
                status="in_progress",
            )
            complete_task(task_id, result="resultado " * 30, db_path=str(db_path))

        app.tasks_db_path = str(db_path)
        build_task_services(app).refresh_task_shared_state()

        results = app.shared_state.get("completed_task_results", "")
        self.assertLessEqual(len(results), 2000)
        self.assertIn("omitida", results)
        self.assertNotIn("task-delegation-01", results)
        self.assertNotIn("task-delegation-02", results)
        self.assertNotIn("task-delegation-03", results)
        self.assertIn("[task 9]", results)
        self.assertIn("[task 10]", results)

    def test_refresh_task_shared_state_removes_completed_task_results_when_none_exist(self):
        """Verifica que refresh task shared state removes completed task results when none exist."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.shared_state = {"completed_task_results": "stale"}
        app.current_job_id = 1
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)
        build_task_services(app).refresh_task_shared_state()

        self.assertIn("task_overview", app.shared_state)
        self.assertNotIn("completed_task_results", app.shared_state)

    def test_refresh_task_shared_state_returns_when_shared_state_is_invalid(self):
        """Verifica que refresh task shared state returns when shared state is invalid."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.shared_state = None
        app.current_job_id = 1
        app.tasks_db_path = "/tmp/unused.db"

        build_task_services(app).refresh_task_shared_state()

        self.assertIsNone(app.shared_state)

    def test_prompt_includes_completed_task_results_without_goal_lock(self):
        """Verifica que prompt includes completed task results without goal lock."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={
                "task_overview": {"job_id": 1},
                "completed_task_results": "[task 1] testes: ok",
            },
        )

        self.assertIn('<completed_tasks title="Tarefas concluídas">', prompt)
        self.assertIn("</completed_tasks>", prompt)
        self.assertIn("[task 1] testes: ok", prompt)

    def test_prompt_debug_metrics_include_prompt_sizes(self):
        """Verifica que prompt debug metrics include prompt sizes."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [
            {"role": "human", "content": "Pergunta"},
            {"role": "claude", "content": "Resposta objetiva"},
        ]

        prompt, metrics = builder.build(AGENT_CLAUDE, history, debug=True)

        self.assertIn('<recent_conversation title="Conversa recente">', prompt)
        self.assertIn("</recent_conversation>", prompt)
        self.assertTrue(metrics["primary"])
        self.assertGreater(metrics["total_chars"], 0)
        self.assertIn("execution_state_chars", metrics)

    def test_prompt_wraps_core_sections_with_consistent_boundaries(self):
        """Verifica que prompt wraps core sections with consistent boundaries."""
        class ContextManagerWithData(DummyContextManager):
            def load(self):
                return "Contexto persistente ativo"

        builder = PromptBuilder(
            ContextManagerWithData(),
            history_window=5,
            session_state={
                "session_id": "sessao-2026-03-27-123456",
                "current_job_id": 1,
                "workspace_root": "/tmp/quimera",
                "current_dir": ".",
            },
        )
        history = [
            {"role": "human", "content": "Pedido atual"},
            {"role": "claude", "content": "Contexto de agente"},
        ]

        prompt = builder.build(AGENT_CODEX, history, delegation="Revise este ponto.")

        self.assertIn('<header title="Identificação">', prompt)
        self.assertIn("</header>", prompt)
        self.assertIn('<session_state title="Estado da sessão">', prompt)
        self.assertIn('<rules title="Suas regras">', prompt)
        self.assertIn("</rules>", prompt)
        self.assertIn('<persistent_context title="Contexto persistente do workspace">', prompt)
        self.assertIn("</persistent_context>", prompt)
        self.assertIn('<current_turn title="Pedido atual de >>>">', prompt)
        self.assertIn('<delegation title="Mensagem direta do outro agente">', prompt)
        self.assertIn('<recent_conversation title="Conversa recente">', prompt)
        self.assertNotIn('<response_prefix title="PREFIXO DE RESPOSTA">', prompt)
        self.assertNotIn("</response_prefix>", prompt)

    def test_behavior_metrics_generate_feedback_empty_when_few_responses(self):
        """generate_feedback deve retornar vazio com menos de 3 respostas."""
        from quimera.metrics import BehaviorMetricsTracker

        tracker = BehaviorMetricsTracker()
        tracker.record_response("claude", 1.0)
        tracker.record_response("claude", 1.0)

        feedback = tracker.generate_feedback("claude")
        self.assertEqual(feedback, "")

    def test_behavior_metrics_generate_feedback_with_synthesis_correction(self):
        """Feedback deve indicar sínteses imprecisas quando correction rate é alto."""
        from quimera.metrics import BehaviorMetricsTracker

        tracker = BehaviorMetricsTracker()
        for i in range(5):
            tracker.record_response("claude", 1.0)
        for i in range(4):
            tracker.record_synthesis("claude", needed_correction=True)

        feedback = tracker.generate_feedback("claude")
        self.assertIn("SÍNTESES IMPRECISAS", feedback)

    def test_behavior_metrics_generate_feedback_for_invalid_delegation_context_gap(self):
        """Feedback de delegation inválido deve tratar falta de contexto como erro de roteamento."""
        from quimera.metrics import BehaviorMetricsTracker

        tracker = BehaviorMetricsTracker()
        for _ in range(5):
            tracker.record_response("claude", 1.0)
        for _ in range(2):
            tracker.record_delegation_sent("claude", is_invalid=True)

        feedback = tracker.generate_feedback("claude")
        self.assertIn("ALTA TAXA DE DELEGAÇÃO INVÁLIDA", feedback)
        self.assertIn("faltar contexto suficiente", feedback)
        self.assertIn("falha no roteamento inicial", feedback)
        self.assertIn("delegue", feedback)
        self.assertIn("não improvise", feedback)
        self.assertNotIn("resolva você mesmo", feedback)

    def test_prompt_builder_injects_metrics_when_tracker_has_data(self):
        """PromptBuilder deve incluir bloco de métricas com framing de referência quando há feedback do tracker."""
        from quimera.metrics import BehaviorMetricsTracker

        tracker = BehaviorMetricsTracker()
        # Gera dados suficientes para acionar feedback (>= 3 respostas + sínteses com correção)
        for _ in range(5):
            tracker.record_response("claude", 1.0)
        for _ in range(4):
            tracker.record_synthesis("claude", needed_correction=True)

        builder = PromptBuilder(DummyContextManager(), history_window=3, metrics_tracker=tracker)
        prompt = builder.build("claude", [])

        self.assertIn('<agent_metrics title="Suas métricas (apenas referência)">', prompt)
        self.assertIn("</agent_metrics>", prompt)
        self.assertIn("SÍNTESES IMPRECISAS", prompt)

    def test_prompt_builder_omits_metrics_when_no_tracker(self):
        """PromptBuilder sem metrics_tracker não deve incluir bloco de métricas."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        prompt = builder.build("claude", [])

        self.assertNotIn('<agent_metrics title="Suas métricas (apenas referência)">', prompt)

    def test_prompt_builder_omits_metrics_when_insufficient_data(self):
        """PromptBuilder não deve incluir métricas se generate_feedback retornar vazio."""
        from quimera.metrics import BehaviorMetricsTracker

        tracker = BehaviorMetricsTracker()
        tracker.record_response("claude", 1.0)  # apenas 1 resposta — abaixo do threshold

        builder = PromptBuilder(DummyContextManager(), history_window=3, metrics_tracker=tracker)
        prompt = builder.build("claude", [])

        self.assertNotIn('<agent_metrics title="Suas métricas (apenas referência)">', prompt)


class AppProtocolDirectTests(unittest.TestCase):
    """Testes unitários diretos de AppProtocol para cobertura de ramos não exercidos."""

    def _make_app(self, shared_state=None, session_state=None):
        app = QuimeraApp.__new__(QuimeraApp)
        import threading
        from unittest.mock import Mock
        from quimera.app.session_state import SessionStateManager
        app._lock = threading.Lock()
        if session_state is None:
            ss = shared_state if shared_state is not None else {}
            mgr = SessionStateManager(storage=Mock(), shared_state=ss)
            app._shared_state_lock = mgr._lock
            app._turn_stamps = mgr._turn_stamps
            app.shared_state = mgr.shared_state
            app.session_state_mgr = mgr
        else:
            app._shared_state_lock = threading.Lock()
            app._turn_stamps = {}
            app.shared_state = shared_state if shared_state is not None else {}
            app.session_state_mgr = session_state
        return app

    # --- _get_decisions_logger ---

    def test_get_decisions_logger_returns_none_when_no_path(self):
        """Verifica que get decisions logger returns none when no path."""
        proto = AppProtocol(lock=threading.Lock(), shared_state={}, decisions_log_path=None)
        result = proto._get_decisions_logger()
        self.assertIsNone(result)

    def test_get_decisions_logger_caches_instance(self):
        """Verifica que get decisions logger caches instance."""
        with patch("quimera.workspace.DecisionsLogger") as MockDL:
            import tempfile
            tmp = tempfile.mktemp(suffix=".json")
            proto = AppProtocol(lock=threading.Lock(), shared_state={}, decisions_log_path=tmp)
            # first call creates it
            first = proto._get_decisions_logger()
            # second call returns cached (line 34)
            second = proto._get_decisions_logger()
            self.assertIs(first, second)

    def test_get_decisions_logger_creates_instance_with_path(self):
        """Verifica que get decisions logger creates instance with path."""
        with patch("quimera.app.protocol.DecisionsLogger", create=True) as MockDL:
            pass
        # test via apply_state_update which calls the logger (lines 37-39, 80-81)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log_path = tmp + "/decisions.json"
            mock_logger = Mock()
            with patch("quimera.workspace.DecisionsLogger", return_value=mock_logger):
                app = self._make_app()
                app.workspace = SimpleNamespace(cwd="/tmp")
                proto = AppProtocol(lock=app._lock, shared_state=app.shared_state, workspace=app.workspace, decisions_log_path=log_path)
                payload = {"decisions": ["dec1", "dec2"]}
                result = proto.apply_state_update(payload)
            self.assertTrue(result)
            mock_logger.append.assert_called()

    # --- merge_state_value ---

    def test_merge_state_value_incoming_none_returns_current(self):
        """Verifica que merge state value incoming none returns current."""
        result = AppProtocol.merge_state_value("existing", None)
        self.assertEqual(result, "existing")

    def test_merge_state_value_incoming_empty_string_returns_none(self):
        """Verifica que merge state value incoming empty string returns none."""
        result = AppProtocol.merge_state_value("existing", "")
        self.assertIsNone(result)

    # --- apply_state_update ---

    def test_apply_state_update_non_dict_returns_false(self):
        """Verifica que apply state update non dict returns false."""
        app = self._make_app()
        proto = _make_protocol(app)
        result = proto.apply_state_update("just a string")
        self.assertFalse(result)

    def test_apply_state_update_empty_dict_returns_false(self):
        """Verifica que apply state update empty dict returns false."""
        app = self._make_app()
        proto = _make_protocol(app)
        result = proto.apply_state_update({})
        self.assertFalse(result)

    def test_apply_state_update_skips_empty_key(self):
        """Verifica que apply state update skips empty key."""
        app = self._make_app()
        proto = _make_protocol(app)
        result = proto.apply_state_update({"": "value", "valid": "ok"})
        self.assertTrue(result)
        self.assertNotIn("", app.shared_state)
        self.assertEqual(app.shared_state, {})

    def test_apply_state_update_pops_key_when_merged_is_none(self):
        """Verifica que apply state update pops key when merged is none."""
        app = self._make_app(shared_state={"goal": "old"})
        proto = _make_protocol(app)
        # incoming "" causes merge to return None → pop
        result = proto.apply_state_update({"goal": ""})
        self.assertTrue(result)
        self.assertNotIn("goal", app.shared_state)

    def test_apply_state_update_ignores_keys_outside_agent_allowlist(self):
        """Verifica que apply state update ignores keys outside agent allowlist."""
        app = self._make_app(shared_state={"goal": "old"})
        proto = _make_protocol(app)

        result = proto.apply_state_update(
            {
                "goal_canonical": "corrigir parser",
                "task_overview": {"job_id": 1},
                "completed_task_results": "hack",
                "spy_last_turn_detail": {"agent": "claude"},
                "mode": "test",
            }
        )

        self.assertTrue(result)
        self.assertEqual(
            app.shared_state,
            {
                "goal": "old",
                "goal_canonical": "corrigir parser",
            },
        )
        self.assertLessEqual(set(app.shared_state), AGENT_STATE_KEYS)

    def test_apply_state_update_rejects_invalid_value_types_for_agent_contract(self):
        """Verifica que apply state update rejects invalid value types for agent contract."""
        app = self._make_app()
        proto = _make_protocol(app)

        result = proto.apply_state_update(
            {
                "goal_canonical": ["lista inválida"],
                "allowed_scope": "parser.py",
                "evidence": ["pytest -q"],
                "next_step": "executar suíte",
            }
        )

        self.assertTrue(result)
        self.assertEqual(
            app.shared_state,
            {
                "evidence": ["pytest -q"],
                "next_step": "executar suíte",
            },
        )

    def test_apply_state_update_ignores_invalid_type_for_list_field(self):
        """Verifica que apply state update ignores invalid type for list field."""
        app = self._make_app()
        proto = _make_protocol(app)
        result = proto.apply_state_update({"allowed_scope": "parser.py"})
        self.assertTrue(result)
        self.assertEqual(app.shared_state, {})

    def test_apply_state_update_stamps_only_effective_keys(self):
        """Verifica que apply state update stamps only effective keys."""
        app = self._make_app(shared_state={"_current_turn": 9})
        proto = _make_protocol(app)

        result = proto.apply_state_update(
            {
                "goal_canonical": "valid",
                "allowed_scope": "invalid",
                "non_agent": "x",
                "next_step": "seguir",
            }
        )

        self.assertTrue(result)
        self.assertEqual(
            app.shared_state,
            {"_current_turn": 9, "goal_canonical": "valid", "next_step": "seguir"},
        )
        self.assertEqual(app._turn_stamps["goal_canonical"], 9)
        self.assertEqual(app._turn_stamps["next_step"], 9)
        self.assertNotIn("allowed_scope", app._turn_stamps)

    def test_apply_state_update_clearing_key_removes_stamp(self):
        """Verifica que apply state update clearing key removes stamp."""
        app = self._make_app(shared_state={"_current_turn": 5, "goal_canonical": "old"})
        app._turn_stamps["goal_canonical"] = 4
        proto = _make_protocol(app)

        result = proto.apply_state_update({"goal_canonical": ""})

        self.assertTrue(result)
        self.assertNotIn("goal_canonical", app.shared_state)
        self.assertNotIn("goal_canonical", app._turn_stamps)

    def test_advance_shared_state_turn_increments_once_per_call_and_expires(self):
        """Verifica que advance shared state turn increments once per call and expires."""
        app = self._make_app(
            shared_state={
                "_current_turn": 10,
                "goal_canonical": "g",
                "current_step": "s",
            }
        )
        app._turn_stamps["goal_canonical"] = 0
        app._turn_stamps["current_step"] = 10

        app.session_state_mgr.advance_turn()

        self.assertEqual(app.shared_state["_current_turn"], 11)
        self.assertNotIn("goal_canonical", app.shared_state)
        self.assertIn("current_step", app.shared_state)

    # --- parse_response ---

    def test_parse_response_none_returns_all_none(self):
        """Verifica que parse response none returns all none."""
        app = self._make_app()
        proto = _make_protocol(app)
        result = proto.parse_response(None)
        self.assertEqual(result, (None, None, None, False, None))

    def test_parse_response_json_envelope_no_response_content(self):
        """Envelope JSON puro de delegation é tratado como texto (MCP-first)."""
        app = self._make_app()
        proto = _make_protocol(app)
        response, route_target, delegation, extend, ack_id = proto.parse_response(
            '{"type": "delegation", "route": "codex", "content": "task: fazer algo"}'
        )
        self.assertEqual(
            response,
            '{"type": "delegation", "route": "codex", "content": "task: fazer algo"}',
        )
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertFalse(extend)
        self.assertIsNone(ack_id)

    def test_parse_response_json_envelope_delegations_array_no_surrounding_text(self):
        """Envelope JSON com delegations[] também fica como texto no modo MCP-first."""
        app = self._make_app()
        proto = _make_protocol(app)
        response, route_target, delegation, _, _ = proto.parse_response(
            '{"type": "delegation", "steps": ['
            '{"route": "codex", "content": "task: fazer algo"}, '
            '{"route": "claude", "content": "task: validar"}'
            ']}'
        )
        self.assertIn('"type": "delegation"', response)
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)

    def test_parse_response_json_delegation_is_plain_content(self):
        """JSON type=delegation não rota nem dispara delegação."""
        app = self._make_app()
        proto = _make_protocol(app)

        response, route_target, delegation, extend, ack_id = proto.parse_response(
            '{"type": "delegation", "route": "codex", "content": "task: fazer algo"}'
        )

        self.assertEqual(
            response,
            '{"type": "delegation", "route": "codex", "content": "task: fazer algo"}',
        )
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertFalse(extend)
        self.assertIsNone(ack_id)

    def test_parse_response_json_delegation_is_always_ignored_for_routing(self):
        """Não existe mais modo textual: delegation JSON nunca vira route_target."""
        app = self._make_app()
        proto = _make_protocol(app)

        response, route_target, delegation, extend, ack_id = proto.parse_response(
            '{"type": "delegation", "route": "codex", "content": "task: fazer algo"}'
        )

        self.assertIsNotNone(response)
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertFalse(extend)
        self.assertIsNone(ack_id)

    def test_parse_response_json_state_update_is_plain_text(self):
        """JSON state_update solto é conteúdo comum; estado só muda via tool update_shared_state."""
        app = self._make_app()
        proto = _make_protocol(app)
        text = '{"type": "state_update", "content": "", "state_updates": {"goal_canonical": "corrigir parser"}}'

        response, route_target, delegation, extend, ack_id = proto.parse_response(text)

        self.assertEqual(response, text)
        self.assertEqual(app.shared_state, {})
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertFalse(extend)
        self.assertIsNone(ack_id)

    def test_parse_response_ack_marker_works_in_mcp_mode(self):
        """[ACK:id] continua extraído para o fluxo de delegação."""
        app = self._make_app()
        proto = _make_protocol(app)

        response, route_target, delegation, extend, ack_id = proto.parse_response(
            '[ACK:abc123] done'
        )

        self.assertEqual(ack_id, "abc123")
        self.assertEqual(response, "done")
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertFalse(extend)


    # --- parse_response com envelope JSON ---

    def test_parse_response_json_envelope_delegation(self):
        """Verifica que parse response json envelope delegation."""
        app = self._make_app()
        proto = _make_protocol(app)
        response, route_target, delegation, extend, ack_id = (
            proto.parse_response('{"type": "delegation", "content": "task: refactor", "route": "codex"}')
        )
        self.assertEqual(
            response,
            '{"type": "delegation", "content": "task: refactor", "route": "codex"}',
        )
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertFalse(extend)
        self.assertIsNone(ack_id)

    def test_parse_response_json_envelope_state_update_is_plain_text(self):
        """Verifica que parse response json envelope state update is plain text."""
        app = self._make_app()
        proto = _make_protocol(app)
        text = '{"type": "state_update", "content": "", "state_updates": {"goal_canonical": "corrigir parser","mode":"test"}}'
        response, route_target, delegation, extend, ack_id = proto.parse_response(text)
        self.assertEqual(response, text)
        self.assertEqual(app.shared_state, {})
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertFalse(extend)
        self.assertIsNone(ack_id)

    def test_parse_response_json_envelope_ack_is_plain_text(self):
        """Verifica que parse response json envelope ack is plain text."""
        app = self._make_app()
        proto = _make_protocol(app)
        text = '{"type": "ack", "content": "done", "delegation_id": "abc123"}'
        response, route_target, delegation, extend, ack_id = proto.parse_response(text)
        self.assertEqual(response, text)
        self.assertIsNone(ack_id)
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)

    def test_parse_response_embedded_delegation_json_with_text_before(self):
        """JSON type=delegation é conteúdo comum e não roteia."""
        text = (
            'Aqui está minha análise\n'
            '{"type": "delegation", "content": "task: refactor", "route": "codex"}'
        )
        app = self._make_app()
        proto = _make_protocol(app)
        response, route_target, delegation, extend, ack_id = (
            proto.parse_response(text)
        )
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertEqual(response, text)
        self.assertIsNone(ack_id)
        self.assertFalse(extend)

    def test_parse_response_embedded_delegation_array_json_is_plain_text(self):
        """JSON type=delegation com delegations[] é conteúdo comum."""
        text = (
            'Análise\n'
            '{"type": "delegation", "steps": ['
            '{"route": "gemini", "content": "task: analyze this"}, '
            '{"route": "codex", "content": "task: validate this"}'
            ']}'
        )
        app = self._make_app()
        proto = _make_protocol(app)
        response, route_target, delegation, extend, ack_id = (
            proto.parse_response(text)
        )
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertEqual(response, text)

    # --- Embedded envelope tests ---

    def test_parse_response_embedded_delegation_json_text_before_and_after(self):
        """JSON type=delegation embutido permanece no conteúdo e não roteia."""
        text = (
            'Análise inicial\n'
            '{"type": "delegation", "content": "task: revise", "route": "gemini"}\n'
            'Observação final'
        )
        app = self._make_app()
        proto = _make_protocol(app)
        response, route_target, delegation, _, _ = proto.parse_response(text)
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertIn("Análise inicial", response)
        self.assertIn("Observação final", response)
        self.assertIn("task: revise", response)

    def test_parse_response_embedded_delegation_json_text_after_only(self):
        """JSON type=delegation com texto depois permanece no conteúdo."""
        text = (
            '{"type": "delegation", "content": "task: check", "route": "codex"}\n'
            'Resultado da análise'
        )
        app = self._make_app()
        proto = _make_protocol(app)
        response, route_target, delegation, _, _ = proto.parse_response(text)
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertEqual(response, text)

    def test_parse_response_embedded_delegation_json_empty_content_plain_text(self):
        """JSON type=delegation com content vazio permanece conteúdo comum."""
        text = (
            'texto antes\n'
            '{"type": "delegation", "content": "", "route": "codex"}'
        )
        app = self._make_app()
        proto = _make_protocol(app)
        response, route_target, delegation, _, _ = proto.parse_response(text)
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertEqual(response, text)

    def test_parse_response_embedded_delegation_json_whitespace_content_plain_text(self):
        """JSON type=delegation com content whitespace permanece conteúdo comum."""
        text = (
            'texto antes\n'
            '{"type": "delegation", "content": "   ", "route": "codex"}'
        )
        app = self._make_app()
        proto = _make_protocol(app)
        response, route_target, delegation, _, _ = proto.parse_response(text)
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertEqual(response, text)

    def test_parse_response_embedded_json_state_update_is_plain_text(self):
        """JSON state_update embutido não aplica estado; só a tool update_shared_state altera o estado."""
        text = (
            'relatório\n'
            '{"type": "state_update", "content": "", "state_updates": {"goal":"embedded","mode":"ignored"}}'
        )
        app = self._make_app()
        proto = _make_protocol(app)
        response, _, _, _, ack_id = proto.parse_response(text)
        self.assertEqual(app.shared_state, {})
        self.assertEqual(response, text)
        self.assertIsNone(ack_id)

    def test_parse_response_embedded_json_ack_is_plain_text(self):
        """JSON ack embutido não extrai ack_id; use [ACK:id]."""
        text = (
            'processando\n'
            '{"type": "ack", "content": "done", "delegation_id": "xyz789"}'
        )
        app = self._make_app()
        proto = _make_protocol(app)
        response, _, _, _, ack_id = proto.parse_response(text)
        self.assertIsNone(ack_id)
        self.assertEqual(response, text)

    # --- Gap 2: envelope com content vazio ---

    def test_parse_response_json_envelope_empty_content_delegation(self):
        """Delegation envelope com content vazio segue como texto no modo MCP-first."""
        app = self._make_app()
        proto = _make_protocol(app)
        response, route_target, delegation, extend, ack_id = (
            proto.parse_response('{"type": "delegation", "content": "", "route": "codex"}')
        )
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertEqual(response, '{"type": "delegation", "content": "", "route": "codex"}')

    def test_parse_response_json_envelope_whitespace_content_delegation(self):
        """Delegation envelope com whitespace segue como texto no modo MCP-first."""
        app = self._make_app()
        proto = _make_protocol(app)
        response, route_target, delegation, extend, ack_id = (
            proto.parse_response('{"type": "delegation", "content": "   ", "route": "codex"}')
        )
        self.assertIsNone(route_target)
        self.assertIsNone(delegation)
        self.assertEqual(response, '{"type": "delegation", "content": "   ", "route": "codex"}')

    def test_parse_response_delegations_array_single_item_has_no_pending(self):
        """delegations com 1 item não roteia no modo MCP-first."""
        app = self._make_app()
        proto = _make_protocol(app)
        response, target, delegation, _, _ = proto.parse_response(
            '{"type": "delegation", "steps": [{"route": "codex", "content": "task: single target"}]}'
        )
        self.assertIn('"type": "delegation"', response)
        self.assertIsNone(target)
        self.assertIsNone(delegation)

    def test_parse_response_delegations_rejected_empty_content(self):
        """delegations + content vazio continua sem rotear (texto puro)."""
        app = self._make_app()
        proto = _make_protocol(app)
        response, target, delegation, _, _ = proto.parse_response(
            '{"type": "delegation", "steps": [{"route": "codex", "content": ""}]}'
        )
        self.assertIn('"type": "delegation"', response)
        self.assertIsNone(target)
        self.assertIsNone(delegation)

    def test_parse_response_delegation_routes_field_is_plain_content(self):
        """type=delegation com routes não roteia e segue como texto."""
        app = self._make_app()
        proto = _make_protocol(app)
        response, target, delegation, _, _ = proto.parse_response(
            '{"type": "delegation", "routes": ["codex"], "content": "task: something"}'
        )
        self.assertIsNone(target)
        self.assertIsNone(delegation)
        self.assertEqual(
            response,
            '{"type": "delegation", "routes": ["codex"], "content": "task: something"}',
        )

    def test_parse_response_rejected_missing_route_and_delegations_value(self):
        """type=delegation sem route/delegations não roteia e segue como texto."""
        app = self._make_app()
        proto = _make_protocol(app)
        response, target, delegation, _, _ = proto.parse_response(
            '{"type": "delegation", "content": "task: something"}'
        )
        self.assertEqual(response, '{"type": "delegation", "content": "task: something"}')
        self.assertIsNone(target)
        self.assertIsNone(delegation)


# =========================================================================
# Fase 0 — Guardrails: smoke tests de run() e contratos públicos
# =========================================================================


class TestRunSmoke(unittest.TestCase):
    """Smoke tests mínimos para QuimeraApp.run()."""

    def test_run_requires_renderer(self):
        """run() levanta RuntimeError se renderer não foi inicializado."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.session_services = Mock()
        materialize_internal_services(app)
        app.renderer = None
        with self.assertRaises(RuntimeError) as ctx:
            app.run()
        self.assertIn("renderer", str(ctx.exception))

    def test_run_requires_session_services(self):
        """run() levanta RuntimeError se session_services não foi inicializado."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.session_services = None
        with self.assertRaises(RuntimeError) as ctx:
            materialize_internal_services(app)
            app.run()
        self.assertIn("session_services", str(ctx.exception))

    def test_run_exit_immediately(self):
        """run() com /exit imediatamente faz shutdown limpo."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.user_name = "Test"
        app.execution_mode = None
        app.debug_prompt_metrics = False
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.session_state = {
            "session_id": "sessao-guardrail-test",
            "history_count": 0,
            "summary_loaded": False,
        }
        app.agent_client = DummyAgentClient()
        app.threads = 1
        app.read_user_input = Mock(side_effect=["/exit"])
        shutdown_order = []
        app.process_supervisor = Mock()
        app.process_supervisor.shutdown.side_effect = lambda: shutdown_order.append("process_supervisor")
        app.session_services = Mock()
        app.session_services.shutdown = Mock(side_effect=lambda **_kwargs: shutdown_order.append("session"))
        materialize_internal_services(app)
        app.run()
        app.session_services.shutdown.assert_called_once()
        app.process_supervisor.shutdown.assert_called_once()
        self.assertEqual(shutdown_order, ["process_supervisor", "session"])


class TestToolCallGuardrails(unittest.TestCase):
    """Guardrails de contrato público para ToolCall."""

    def test_tool_call_rejects_empty_name(self):
        """ToolCall com name vazio levanta ToolValidationError."""
        from quimera.runtime.errors import ToolValidationError
        with self.assertRaises(ToolValidationError) as ctx:
            ToolCall(name="", arguments={})
        self.assertIn("name", str(ctx.exception))

    def test_tool_call_rejects_non_dict_arguments(self):
        """ToolCall com arguments não-dict levanta ToolValidationError."""
        from quimera.runtime.errors import ToolValidationError
        with self.assertRaises(ToolValidationError) as ctx:
            ToolCall(name="read_file", arguments=None)
        self.assertIn("arguments", str(ctx.exception))

    def test_tool_call_accepts_valid(self):
        """ToolCall com name e arguments válidos é criado."""
        call = ToolCall(name="read_file", arguments={"path": "."})
        self.assertEqual(call.name, "read_file")
        self.assertEqual(call.arguments, {"path": "."})


class TestToolRuntimeConfigGuardrails(unittest.TestCase):
    """Guardrails de contrato público para ToolRuntimeConfig."""

    def test_config_rejects_non_path_workspace_root(self):
        """ToolRuntimeConfig com workspace_root não-Path levanta TypeError."""
        from pathlib import Path
        with self.assertRaises(TypeError) as ctx:
            ToolRuntimeConfig(workspace_root="/tmp")
        self.assertIn("workspace_root", str(ctx.exception))

    def test_config_accepts_valid_path(self):
        """ToolRuntimeConfig com Path válido funciona."""
        from pathlib import Path
        config = ToolRuntimeConfig(workspace_root=Path("/tmp"))
        self.assertEqual(config.workspace_root, Path("/tmp").resolve())


class TestPatchToolGuardrails(unittest.TestCase):
    """Guardrails de contrato público para PatchTool."""

    def test_patch_tool_rejects_invalid_config(self):
        """PatchTool.__init__ com config não-ToolRuntimeConfig levanta TypeError."""
        with self.assertRaises(TypeError) as ctx:
            from quimera.runtime.tools.patch import PatchTool
            PatchTool(config="not a config")
        self.assertIn("config", str(ctx.exception))

    def test_patch_tool_accepts_valid_config(self):
        """PatchTool com ToolRuntimeConfig válido é criado."""
        from quimera.runtime.tools.patch import PatchTool
        config = ToolRuntimeConfig(workspace_root=Path("/tmp"))
        tool = PatchTool(config)
        self.assertIs(tool.config, config)


if __name__ == "__main__":
    unittest.main()

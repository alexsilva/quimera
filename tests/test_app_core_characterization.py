"""Characterization tests for the current QuimeraApp composition root.

These tests intentionally snapshot the public shape and compatibility aliases
of ``QuimeraApp`` before the app/core refactor. Update the constants only when
the composition contract changes deliberately.
"""

import os

import pytest

from quimera.app.core import QuimeraApp


class FakeRenderer:
    theme_name = "fake"
    density = "compact"

    def __getattr__(self, _name):
        def _noop(*_args, **_kwargs):
            return None

        return _noop


class FakeInputGate:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.toolbar_context_resolver = None
        self.theme_cycle_handler = None

    def is_active(self):
        return False

    def get_owner_thread_id(self):
        return None

    def run_in_terminal_message(self, callback=None, *args, **kwargs):
        if callable(callback):
            return callback()
        return None

    def set_toolbar_context_resolver(self, resolver):
        self.toolbar_context_resolver = resolver

    def set_theme_cycle_handler(self, handler):
        self.theme_cycle_handler = handler

    def read_input(self, _prompt, timeout=None):
        return None

    def read_input_in_terminal(self, _prompt, timeout=None, metadata=None):
        return None

    def read_selection_in_terminal(self, _question, _options, metadata=None):
        return None

    def read_approval_in_terminal(self, _question, metadata=None):
        return None

    def redisplay(self):
        return None

    def get_line_buffer(self):
        return ""


EXPECTED_PUBLIC_ATTRS = [
    ("agent_bug_detector", "AgentRuntimeBugDetector"),
    ("agent_client", "AgentClient"),
    ("agent_pool", "AgentPool"),
    ("agent_run_sink", "AgentRunController"),
    ("auto_approve_mutations", "bool"),
    ("auto_summarize_threshold", "int"),
    ("behavior_metrics", "BehaviorMetricsTracker"),
    ("bug_correlator", "BugCorrelator"),
    ("bug_detector", "RenderBugDetector"),
    ("bug_services", "BugServices"),
    ("bug_store", "BugStore"),
    ("chat_lifecycle", "ChatLifecycle"),
    ("chat_round_orchestrator", "ChatRoundOrchestrator"),
    ("command_router", "CommandRouter"),
    ("config", "ConfigManager"),
    ("context_manager", "ContextManager"),
    ("current_job_id", "int"),
    ("debug_prompt_metrics", "bool"),
    ("dispatch_services", "AppDispatchServices"),
    ("event_sink", "EventSink"),
    # execution_mode virou property (Fase 3): o estado vive em
    # app._execution_mode_state (ExecutionModeState) e sai de vars(app).
    ("failure_tracker", "AgentFailureTracker"),
    ("history", "list"),
    ("history_file", "PosixPath"),
    ("idle_timeout_seconds", "int"),
    ("input_broker", "InputBroker"),
    ("input_gate", "FakeInputGate"),
    ("input_services", "AppInputServices"),
    ("process_supervisor", "ProcessSupervisor"),
    ("prompt_builder", "PromptBuilder"),
    ("protocol", "AppProtocol"),
    ("renderer", "FakeRenderer"),
    ("runtime_state", "AppRuntimeState"),
    ("selected_agents", "list"),
    ("session_metrics", "SessionMetricsService"),
    ("session_services", "AppSessionServices"),
    ("session_state", "SessionStateDict"),
    ("session_state_mgr", "SessionStateManager"),
    ("session_summarizer", "SessionSummarizer"),
    ("shared_state", "dict"),
    ("storage", "SessionStorage"),
    ("system_layer", "AppSystemLayer"),
    ("task_classifier", "NoneType"),
    ("task_executor_factory", "function"),
    ("task_executors", "list"),
    ("task_services", "AppTaskServices"),
    ("tasks_db_path", "str"),
    ("threads", "int"),
    ("tool_executor", "ToolExecutor"),
    ("toolbar", "ToolbarManager"),
    ("toolbar_coordinator", "ToolbarCoordinator"),
    ("turn_manager", "TurnManager"),
    ("user_name", "str"),
    ("visibility", "Visibility"),
    ("workspace", "Workspace"),
    ("workspace_policy", "WorkspacePolicy"),
    ("workspace_policy_name", "str"),
]


@pytest.fixture
def app(tmp_path):
    previous_job_id = os.environ.get("QUIMERA_CURRENT_JOB_ID")
    instance = QuimeraApp(
        tmp_path,
        renderer_override=FakeRenderer(),
        input_gate_factory=lambda **kwargs: FakeInputGate(**kwargs),
    )
    try:
        yield instance
    finally:
        instance._stop_task_executors()
        instance._restore_current_job_env()
        if previous_job_id is None:
            os.environ.pop("QUIMERA_CURRENT_JOB_ID", None)
        else:
            os.environ["QUIMERA_CURRENT_JOB_ID"] = previous_job_id


def _public_attr_snapshot(instance):
    return [
        (name, type(value).__name__)
        for name, value in sorted(vars(instance).items())
        if not name.startswith("_")
    ]


def test_quimera_app_public_attribute_graph_is_stable(app):
    assert _public_attr_snapshot(app) == EXPECTED_PUBLIC_ATTRS


def test_quimera_app_state_aliases_are_documented(app):
    assert app._chat_state.history is app.history
    assert app._chat_state.history is app.session_state_mgr.history
    assert app._session_runtime_state.history is app.history
    assert app.session_state_mgr.history is app.history

    assert app._chat_state.shared_state is app.shared_state
    assert app._chat_state.shared_state is app.session_state_mgr.shared_state
    assert app._session_runtime_state.shared_state is app.shared_state
    assert app.session_state_mgr.shared_state is app.shared_state

    assert app._chat_state.session_meta is app.session_state
    assert app._session_runtime_state.session_state is app.session_state

    assert app._chat_state.history_lock is app._history_lock
    assert app._chat_state.history_lock is app.session_state_mgr.history_lock
    assert app.session_state_mgr._history_lock is app._history_lock
    assert app._chat_state.shared_state_lock is app._shared_state_lock
    assert app._chat_state.shared_state_lock is app.session_state_mgr.shared_state_lock
    assert app.session_state_mgr._lock is app._shared_state_lock
    assert app._turn_stamps is app.session_state_mgr.turn_stamps
    assert app.session_state_mgr._turn_stamps is app._turn_stamps
    assert app._turn_stamps is app._session_runtime_state.turn_stamps

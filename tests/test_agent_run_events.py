from quimera.app.agent_gateway import AgentGateway
from quimera.app.agent_run_events import (
    AgentRunController,
    AgentRunEvent,
    AgentRunRegistry,
    NullAgentRunSink,
)
from quimera.prompt_kinds import PromptKind
from quimera.runtime.input_broker import InputBroker, _InputRequest
from quimera.ui.base import RendererBase


class RecordingSink:
    def __init__(self):
        self.events = []

    def emit(self, event: AgentRunEvent) -> None:
        self.events.append(event)


class FakePromptBuilder:
    history_window = 10

    def build(self, *args, **kwargs):
        return "prompt"


class FakeAgentClient:
    _user_cancelled = False

    def __init__(self, result="resposta final", chunks=None, exc=None):
        self.result = result
        self.chunks = list(chunks or [])
        self.exc = exc
        self.flushed = False

    def call(self, agent, prompt, *, silent=False, on_text_chunk=None, progress_callback=None, from_agent=None):
        del agent, prompt, silent, progress_callback, from_agent
        if self.exc is not None:
            raise self.exc
        for chunk in self.chunks:
            if on_text_chunk is not None:
                on_text_chunk(chunk)
        return self.result

    def flush_pending_summary(self):
        self.flushed = True


def make_gateway(client, sink=None):
    return AgentGateway(
        agent_client=client,
        prompt_builder=FakePromptBuilder(),
        renderer=None,
        profile_resolver=lambda agent: None,
        get_history=lambda: [],
        get_shared_state=lambda: {},
        get_execution_mode=lambda: None,
        refresh_task_state=lambda: None,
        session_state={"delegations_sent": 0, "total_latency": 0, "delegations_succeeded": 0, "delegations_failed": 0},
        increment_call_index=lambda: 1,
        get_round_index=lambda: 1,
        agent_run_sink=sink,
    )


def test_agent_gateway_emits_normalized_run_events_for_silent_task_path():
    sink = RecordingSink()
    gateway = make_gateway(FakeAgentClient(chunks=["contexto do agente"]), sink=sink)

    result = gateway.call(
        "codex",
        silent=True,
        show_output=False,
        delegation_only=True,
        prompt_kind=PromptKind.TASK_EXECUTOR,
    )

    assert result == "resposta final"
    assert [event.kind for event in sink.events] == ["started", "delta", "finished"]
    assert sink.events[0].agent == "codex"
    assert sink.events[0].metadata["prompt_kind"] == "task_executor"
    assert sink.events[0].metadata["delegation_only"] is True
    assert sink.events[0].metadata["silent"] is True
    assert sink.events[0].metadata["show_output"] is False
    assert sink.events[0].run_id.startswith("agentrun:")
    assert {event.run_id for event in sink.events} == {sink.events[0].run_id}
    assert sink.events[0].metadata["run_id"] == sink.events[0].run_id
    assert sink.events[0].transport == "task"
    assert sink.events[1].text == "contexto do agente"
    assert sink.events[2].text == "resposta final"


def test_agent_gateway_uses_null_sink_without_changing_behavior():
    gateway = make_gateway(FakeAgentClient(chunks=["ignorado"]), sink=NullAgentRunSink())

    assert gateway.call("claude") == "resposta final"


def test_agent_gateway_propagates_delegation_run_metadata():
    sink = RecordingSink()
    gateway = make_gateway(FakeAgentClient(), sink=sink)

    result = gateway.call(
        "claude",
        delegation={
            "delegation_id": "dlg-123",
            "parent_run_id": "agentrun:parent",
        },
        delegation_only=True,
        protocol_mode="delegation",
    )

    assert result == "resposta final"
    assert [event.kind for event in sink.events] == ["started", "finished"]
    assert sink.events[0].run_id.startswith("agentrun:")
    assert {event.run_id for event in sink.events} == {sink.events[0].run_id}
    assert sink.events[0].delegation_id == "dlg-123"
    assert sink.events[0].parent_run_id == "agentrun:parent"
    assert sink.events[0].transport == "delegate"
    assert sink.events[0].metadata["delegation_id"] == "dlg-123"


def test_agent_gateway_emits_failed_event_when_backend_raises():
    sink = RecordingSink()
    gateway = make_gateway(FakeAgentClient(exc=RuntimeError("boom")), sink=sink)

    try:
        gateway.call("opencode")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected backend exception")

    assert [event.kind for event in sink.events] == ["started", "failed"]
    assert sink.events[-1].agent == "opencode"
    assert sink.events[-1].metadata["error"] == "boom"


def test_agent_gateway_emits_failed_event_for_empty_response():
    sink = RecordingSink()
    gateway = make_gateway(FakeAgentClient(result=""), sink=sink)

    assert gateway.call("openai") == ""
    assert [event.kind for event in sink.events] == ["started", "failed"]
    assert sink.events[-1].agent == "openai"
    assert sink.events[-1].text == ""


def test_input_broker_emits_human_action_events_without_changing_default_flow():
    sink = RecordingSink()
    broker = InputBroker(renderer=None, input_gate=None, agent_run_sink=sink)
    req = _InputRequest(
        kind="ask_user",
        source="codex",
        question="Escolha o próximo passo",
        options=["testar", "parar"],
        timeout=0.01,
        default=(0, "testar"),
    )

    broker._process_request(req, allow_direct_gate=True)

    assert req.wait() == (0, "testar")
    assert [event.kind for event in sink.events] == [
        "human_action_requested",
        "human_action_answered",
    ]
    assert sink.events[0].agent == "codex"
    assert sink.events[0].text == "Escolha o próximo passo"
    assert sink.events[0].metadata["kind"] == "ask_user"
    assert sink.events[0].metadata["options"] == ["testar", "parar"]
    assert sink.events[1].text == "(0, 'testar')"
    assert sink.events[1].metadata["result"] == (0, "testar")


def test_agent_run_controller_commits_stream_on_human_action_request():
    class Renderer(RendererBase):
        def __init__(self):
            self.committed = []

        def commit_agent_stream(self, agent):
            self.committed.append(agent)
            return True

    renderer = Renderer()
    controller = AgentRunController(renderer)

    controller.emit(AgentRunEvent("started", "codex"))
    controller.emit(AgentRunEvent("human_action_requested", "codex"))

    assert renderer.committed == ["codex"]


def test_agent_run_controller_tracks_registry_and_renderer_context():
    class Renderer(RendererBase):
        def __init__(self):
            self.started = []
            self.ended = []

        def begin_agent_run(self, agent, **metadata):
            self.started.append((agent, metadata))

        def end_agent_run(self, agent, **metadata):
            self.ended.append((agent, metadata))

    renderer = Renderer()
    registry = AgentRunRegistry(clock=iter([1.0, 2.0, 3.0]).__next__)
    controller = AgentRunController(renderer, registry=registry)

    controller.emit(AgentRunEvent("started", "codex", run_id="agentrun:test", transport="chat"))
    controller.emit(AgentRunEvent("delta", "codex", text="chunk", run_id="agentrun:test", transport="chat"))
    controller.emit(AgentRunEvent("finished", "codex", text="final", run_id="agentrun:test", transport="chat"))

    record = registry.get("agentrun:test")
    assert record is not None
    assert record.status == "finished"
    assert record.event_count == 3
    assert record.started_at == 1.0
    assert record.finished_at == 3.0
    assert renderer.started == [
        (
            "codex",
            {
                "run_id": "agentrun:test",
                "parent_run_id": "",
                "delegation_id": "",
                "transport": "chat",
            },
        )
    ]
    assert renderer.ended == [("codex", {"run_id": "agentrun:test", "status": "finished"})]


def test_agent_run_registry_marks_tool_events_as_finished():
    registry = AgentRunRegistry(clock=iter([1.0, 2.0]).__next__)

    registry.record(AgentRunEvent("tool_started", "mcp-http", run_id="http:run", transport="mcp_http"))
    record = registry.record(AgentRunEvent("tool_finished", "mcp-http", run_id="http:run", transport="mcp_http"))

    assert record is not None
    assert record.status == "finished"
    assert record.finished_at == 2.0
    assert registry.active_runs() == []


def test_input_broker_human_action_request_commits_agent_before_answer():
    order = []

    class Renderer(RendererBase):
        def commit_agent_stream(self, agent):
            order.append(("commit", agent))
            return True

    class Broker(InputBroker):
        def _handle_ask_user(self, req, *, allow_direct_gate=False):
            order.append(("answer", req.source))
            return req.default

    controller = AgentRunController(Renderer())
    broker = Broker(renderer=None, input_gate=None, agent_run_sink=controller)
    req = _InputRequest(
        kind="ask_user",
        source="codex",
        question="Confirmar?",
        options=["sim"],
        timeout=0.01,
        default=(0, "sim"),
    )

    broker._process_request(req, allow_direct_gate=True)

    assert order == [("commit", "codex"), ("answer", "codex")]


def test_agent_gateway_emits_failed_when_prompt_build_raises():
    sink = RecordingSink()
    gateway = make_gateway(FakeAgentClient(), sink=sink)
    gateway._prompt_builder.build = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("prompt boom"))

    try:
        gateway.call("codex")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected prompt exception")

    assert [event.kind for event in sink.events] == ["started", "failed"]
    assert sink.events[-1].metadata["error"] == "prompt boom"


def test_agent_gateway_emits_cancelled_before_backend_call():
    sink = RecordingSink()
    client = FakeAgentClient()
    gateway = make_gateway(client, sink=sink)

    def build_and_cancel(*args, **kwargs):
        client._user_cancelled = True
        return "prompt"

    gateway._prompt_builder.build = build_and_cancel
    result = gateway.call("codex")

    assert result is None
    assert [event.kind for event in sink.events] == ["started", "cancelled"]


def test_agent_run_registry_default_retention_limit():
    registry = AgentRunRegistry()

    assert registry.max_runs == 100


def test_agent_run_registry_prunes_oldest_finished_run_when_limit_is_exceeded():
    registry = AgentRunRegistry(max_runs=2, clock=iter([1.0, 2.0, 3.0]).__next__)

    registry.record(AgentRunEvent("finished", "codex", run_id="run:1"))
    registry.record(AgentRunEvent("finished", "codex", run_id="run:2"))
    registry.record(AgentRunEvent("finished", "codex", run_id="run:3"))

    assert registry.get("run:1") is None
    assert registry.get("run:2") is not None
    assert registry.get("run:3") is not None
    assert [run.run_id for run in registry.snapshot()] == ["run:2", "run:3"]


def test_agent_run_registry_never_prunes_active_runs():
    registry = AgentRunRegistry(max_runs=1, clock=iter([1.0, 2.0, 3.0]).__next__)

    registry.record(AgentRunEvent("started", "codex", run_id="run:active-1"))
    registry.record(AgentRunEvent("started", "claude", run_id="run:active-2"))
    registry.record(AgentRunEvent("finished", "opencode", run_id="run:finished"))

    assert registry.get("run:active-1") is not None
    assert registry.get("run:active-2") is not None
    assert registry.get("run:finished") is None
    assert {run.run_id for run in registry.active_runs()} == {"run:active-1", "run:active-2"}


def test_agent_run_registry_prune_preserves_snapshot_and_active_runs():
    registry = AgentRunRegistry(max_runs=2, clock=iter([1.0, 2.0, 3.0, 4.0]).__next__)

    registry.record(AgentRunEvent("started", "codex", run_id="run:active"))
    registry.record(AgentRunEvent("finished", "codex", run_id="run:old"))
    registry.record(AgentRunEvent("finished", "codex", run_id="run:new"))
    registry.prune()

    assert {run.run_id for run in registry.snapshot()} == {"run:active", "run:new"}
    assert [run.run_id for run in registry.active_runs()] == ["run:active"]
    assert registry.get("run:old") is None


def test_agent_run_registry_notifies_pruned_run_ids():
    pruned_batches = []
    registry = AgentRunRegistry(
        max_runs=1,
        clock=iter([1.0, 2.0]).__next__,
        on_prune=pruned_batches.append,
    )

    registry.record(AgentRunEvent("finished", "codex", run_id="run:old"))
    registry.record(AgentRunEvent("finished", "codex", run_id="run:new"))

    assert pruned_batches == [["run:old"]]

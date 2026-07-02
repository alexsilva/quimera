from quimera.app.agent_gateway import AgentGateway
from quimera.app.agent_run_events import AgentRunController, AgentRunEvent, NullAgentRunSink
from quimera.prompt_kinds import PromptKind
from quimera.runtime.input_broker import InputBroker, _InputRequest


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

    def call(self, agent, prompt, *, silent=False, on_text_chunk=None, progress_callback=None):
        del agent, prompt, silent, progress_callback
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
    assert sink.events[1].text == "contexto do agente"
    assert sink.events[2].text == "resposta final"


def test_agent_gateway_uses_null_sink_without_changing_behavior():
    gateway = make_gateway(FakeAgentClient(chunks=["ignorado"]), sink=NullAgentRunSink())

    assert gateway.call("claude") == "resposta final"


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
    class Renderer:
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


def test_input_broker_human_action_request_commits_agent_before_answer():
    order = []

    class Renderer:
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

"""Isolamento visual entre execuções simultâneas do mesmo agente.

Cenário relatado: o agente cria um workflow no chat (execução A) e o workflow
despacha um passo para o mesmo agente (execução B). As duas rodam juntas e só
o ``run_id`` as distingue; nada que uma faça pode aparecer, encerrar ou
apagar o cartão da outra.

As execuções passam pelo ``AgentGateway`` real, com controlador, renderer
Textual e feed reais; só o backend do agente é roteirizado.
"""
from __future__ import annotations

import io
import json
import queue
import threading
from unittest.mock import MagicMock

import pytest

from quimera.app.agent_gateway import cleanup_agent_transient_if_unowned
from quimera.app.dispatch import AppDispatchServices
from quimera.app.ui_event_handler import UiEventHandler
from quimera.domain.agent_run_events import AgentRunController
from quimera.domain.agent_run_scope import (
    AgentRunScope,
    bind_agent_run_scope,
    current_agent_run_scope,
    run_in_current_context,
)
from quimera.prompt_kinds import PromptKind
from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.feed_model import AgentLifecycleStatus, TextualFeedModel
from quimera.ui.textual.renderer import TextualRenderer
from tests.test_agent_run_events import make_gateway

AGENT = "codexcloud-gpt-6"
_WAIT = 5


class _Feed:
    """Renderer Textual real ligado a um feed real, seguro para várias threads."""

    def __init__(self) -> None:
        self.model = TextualFeedModel()
        self.events = []
        self._lock = threading.Lock()
        bridge = TextualUiBridge()
        bridge.emit = self._apply
        self.renderer = TextualRenderer(bridge)
        self.renderer.flush = lambda timeout=5.0: None
        self.controller = AgentRunController(self.renderer)

    def _apply(self, event) -> None:
        with self._lock:
            self.events.append(event)
            self.model.apply(event)

    def snapshot(self) -> list[tuple[str, bool, str, str]]:
        with self._lock:
            rows = []
            for item in self.model.items:
                payload = item.event.payload if isinstance(item.event.payload, dict) else {}
                rows.append(
                    (
                        item.event.kind,
                        item.transient,
                        str(payload.get("run_id") or ""),
                        str(payload.get("content") or ""),
                    )
                )
            return rows

    def transient_runs(self) -> dict[str, str]:
        """run_id → conteúdo de cada cartão transitório do agente."""
        return {
            run_id: content
            for kind, transient, run_id, content in self.snapshot()
            if transient and kind == "agent_update"
        }

    def finals(self) -> list[tuple[str, str]]:
        return [
            (run_id, content)
            for kind, transient, run_id, content in self.snapshot()
            if kind == "agent_message" and not transient
        ]


class _Step:
    """Passo roteirizado: espera a liberação do teste, age e avisa que agiu."""

    def __init__(self, action) -> None:
        self.action = action
        self.release = threading.Event()
        self.done = threading.Event()


class _ScriptedClient:
    """Backend de agente que executa passos sob o controle do teste.

    Metade dos passos roda numa thread de "driver" disparada com
    ``run_in_current_context``, como o ``AgentClient`` faz com drivers de API;
    a outra metade roda na thread do gateway, como o presenter de CLI.
    """

    _user_cancelled = False

    def __init__(self, steps: list[_Step], result: str, *, driver_thread: bool = True) -> None:
        self.steps = steps
        self.result = result
        self.driver_thread = driver_thread

    def call(self, agent, prompt, *, on_text_chunk=None, **_kwargs):
        del prompt

        def _run_steps() -> None:
            for step in self.steps:
                assert step.release.wait(_WAIT), "passo não liberado"
                step.action(agent, on_text_chunk)
                step.done.set()

        if not self.driver_thread:
            _run_steps()
            return self.result
        driver = threading.Thread(target=run_in_current_context(_run_steps), daemon=True)
        driver.start()
        driver.join(_WAIT * 4)
        assert not driver.is_alive()
        return self.result

    def flush_pending_summary(self):
        return None


def _advance(step: _Step) -> None:
    step.release.set()
    assert step.done.wait(_WAIT), "passo não concluiu"


def _run_in_thread(feed: _Feed, client, *, prompt_kind=PromptKind.CHAT, show_final=True):
    """Executa uma chamada completa (gateway + resposta final) numa thread."""
    gateway = make_gateway(client, sink=feed.controller)
    gateway._renderer = feed.renderer
    outcome: dict = {}

    def _target() -> None:
        try:
            result = gateway.call(AGENT, prompt_kind=prompt_kind, show_output=True)
            outcome["result"] = result
            if show_final:
                feed.renderer.show_turn_summary(
                    AGENT,
                    {"runtime": "openai", "tools": [{"status": "ok", "duration_ms": 5}]},
                )
                feed.renderer.show_message(AGENT, result)
        except BaseException as exc:  # pragma: no cover - torna a falha visível
            outcome["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, outcome


def _hold():
    """Passo vazio: mantém a execução aberta até o teste liberar."""
    return _Step(lambda agent, on_chunk: None)


def _think(text):
    return lambda agent, on_chunk: on_chunk(f"<think>{text}</think>")


def _status(text, status=AgentLifecycleStatus.RUNNING):
    return lambda feed: (lambda agent, on_chunk: feed.renderer.update_status(agent, text, status=status))


def _run_ids(feed: _Feed) -> dict[str, str]:
    """Mapeia o rótulo do conteúdo para o run_id real (o gateway gera os ids)."""
    ids = {}
    for event in feed.events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        content = str(payload.get("content") or "")
        if event.kind == "agent_update" and content.startswith("<"):
            ids.setdefault(content[1], str(payload.get("run_id") or ""))
    return ids


def test_second_run_of_same_agent_never_touches_the_first_card():
    """Workflow despacha B para o mesmo agente enquanto A (chat) segue rodando."""
    feed = _Feed()
    a_steps = [
        _Step(_think("<A> vou criar o workflow")),
        _Step(_think("<A> esperando o workflow")),
        _Step(_status("concluído", AgentLifecycleStatus.COMPLETED)(feed)),
    ]
    b_steps = [
        _Step(_think("<B> passo do workflow")),
        _Step(lambda agent, on_chunk: feed.renderer.show_tool_preview("⚒ read_file README.md", agent=agent)),
        _Step(_status("concluído", AgentLifecycleStatus.COMPLETED)(feed)),
    ]
    thread_a, out_a = _run_in_thread(feed, _ScriptedClient(a_steps, "<A> resposta do chat"))
    _advance(a_steps[0])
    thread_b, out_b = _run_in_thread(
        feed,
        _ScriptedClient(b_steps, "<B> resposta do passo"),
        prompt_kind=PromptKind.TASK_EXECUTOR,
    )
    _advance(b_steps[0])
    ids = _run_ids(feed)
    run_a, run_b = ids["A"], ids["B"]
    assert run_a and run_b and run_a != run_b
    assert feed.transient_runs() == {
        run_a: "<A> vou criar o workflow",
        run_b: "<B> passo do workflow",
    }

    # A escreve depois que B começou: vai para o cartão de A, não o de B.
    _advance(a_steps[1])
    _advance(b_steps[1])
    assert feed.transient_runs() == {
        run_a: "<A> esperando o workflow",
        run_b: "<B> passo do workflow",
    }

    # B termina: só o cartão de B vira resposta final; A segue vivo.
    _advance(b_steps[2])
    thread_b.join(_WAIT)
    assert not thread_b.is_alive() and "error" not in out_b
    assert feed.finals() == [(run_b, "<B> resposta do passo")]
    assert feed.transient_runs() == {run_a: "<A> esperando o workflow"}

    # A termina depois, com a própria resposta no próprio lugar.
    _advance(a_steps[2])
    thread_a.join(_WAIT)
    assert not thread_a.is_alive() and "error" not in out_a
    assert feed.transient_runs() == {}
    assert sorted(feed.finals()) == sorted(
        [(run_a, "<A> resposta do chat"), (run_b, "<B> resposta do passo")]
    )
    # O rodapé de tools de B foi para a resposta de B, e o de A para a de A.
    footers = [
        (item.event.payload.get("run_id"), bool(item.event.payload.get("turn_summary")))
        for item in feed.model.items
        if item.event.kind == "agent_message"
    ]
    assert sorted(footers) == sorted([(run_a, True), (run_b, True)])


def test_first_run_finishing_first_keeps_the_second_alive():
    """Ordem inversa: A termina antes de B, e B continua com o cartão próprio."""
    feed = _Feed()
    a_steps = [_Step(_think("<A> chat")), _Step(_status("concluído", AgentLifecycleStatus.COMPLETED)(feed))]
    b_steps = [_Step(_think("<B> task")), _Step(_think("<B> ainda na task")), _hold()]
    thread_a, _ = _run_in_thread(feed, _ScriptedClient(a_steps, "<A> fim"))
    _advance(a_steps[0])
    thread_b, _ = _run_in_thread(feed, _ScriptedClient(b_steps, "<B> fim"), prompt_kind=PromptKind.TASK_EXECUTOR)
    _advance(b_steps[0])
    ids = _run_ids(feed)

    _advance(a_steps[1])
    thread_a.join(_WAIT)
    assert feed.transient_runs() == {ids["B"]: "<B> task"}

    _advance(b_steps[1])
    assert feed.transient_runs() == {ids["B"]: "<B> ainda na task"}
    _advance(b_steps[2])
    thread_b.join(_WAIT)
    assert feed.transient_runs() == {}
    assert sorted(feed.finals()) == sorted([(ids["A"], "<A> fim"), (ids["B"], "<B> fim")])


def test_failed_or_cleared_run_does_not_clear_the_other_run():
    """Falha e limpeza sem run_id (caminho CLI) ficam restritas à própria execução."""
    feed = _Feed()
    a_steps = [_Step(_think("<A> trabalhando")), _hold()]
    b_steps = [
        _Step(_think("<B> trabalhando")),
        # CLI limpa o transitório pelo nome do agente no meio do turno.
        _Step(lambda agent, on_chunk: feed.renderer.clear_agent_transient(agent)),
        _Step(_status("falhou", AgentLifecycleStatus.FAILED)(feed)),
    ]
    thread_a, _ = _run_in_thread(feed, _ScriptedClient(a_steps, "<A> fim"))
    _advance(a_steps[0])
    thread_b, _ = _run_in_thread(
        feed,
        _ScriptedClient(b_steps, None, driver_thread=False),
        prompt_kind=PromptKind.TASK_EXECUTOR,
        show_final=False,
    )
    _advance(b_steps[0])
    ids = _run_ids(feed)

    # Reset pelo nome vindo de fora das duas execuções (sem escopo): não há
    # como saber de qual delas é, então nenhuma das duas pode sumir.
    feed.renderer.reset_visual_state(AGENT)
    feed.renderer.clear_agent_transient(AGENT)
    assert feed.transient_runs() == {ids["A"]: "<A> trabalhando", ids["B"]: "<B> trabalhando"}

    _advance(b_steps[1])
    assert feed.transient_runs() == {ids["A"]: "<A> trabalhando"}
    _advance(b_steps[2])
    thread_b.join(_WAIT)
    assert feed.transient_runs() == {ids["A"]: "<A> trabalhando"}
    _advance(a_steps[1])
    thread_a.join(_WAIT)
    assert feed.transient_runs() == {}
    assert feed.finals() == [(ids["A"], "<A> fim")]


def test_final_message_through_ui_queue_keeps_its_run():
    """A resposta final enfileirada é exibida por outra thread e mesmo assim acha seu run."""
    feed = _Feed()
    ui_queue: queue.Queue = queue.Queue()
    dispatch = AppDispatchServices(ui_queue=ui_queue)
    handler = UiEventHandler(
        renderer=feed.renderer,
        input_gate=None,
        system_layer=MagicMock(),
        event_sink=MagicMock(),
        show_muted_message=MagicMock(),
        show_system_message=MagicMock(),
        show_warning_message=MagicMock(),
        show_error_message=MagicMock(),
        redisplay_user_prompt=MagicMock(),
    )
    b_steps = [_Step(_think("<B> task")), _hold()]
    a_steps = [_Step(_think("<A> chat")), _hold()]

    def _run_b() -> None:
        gateway = make_gateway(_ScriptedClient(b_steps, "<B> fim"), sink=feed.controller)
        gateway._renderer = feed.renderer
        result = gateway.call(AGENT, prompt_kind=PromptKind.TASK_EXECUTOR, show_output=True)
        dispatch.print_response(AGENT, result)

    # B começa antes e A depois: a execução mais recente do agente é A,
    # mas a resposta enfileirada é de B.
    thread_b = threading.Thread(target=_run_b, daemon=True)
    thread_b.start()
    _advance(b_steps[0])
    thread_a, _ = _run_in_thread(feed, _ScriptedClient(a_steps, "<A> fim"), show_final=False)
    _advance(a_steps[0])
    _advance(b_steps[1])
    thread_b.join(_WAIT)
    ids = _run_ids(feed)

    assert feed.finals() == []
    # O dreno roda em outra thread e não deixa o escopo de B grudado nela.
    assert threading_result(lambda: _drain_and_probe(handler, ui_queue)) is None
    assert feed.finals() == [(ids["B"], "<B> fim")]
    assert feed.transient_runs() == {ids["A"]: "<A> chat"}
    _advance(a_steps[1])
    thread_a.join(_WAIT)


def test_delegator_cleanup_after_its_delegate_spares_concurrent_visible_run():
    """Limpeza do delegador (pelo nome) depois do delegate não apaga o chat do mesmo agente."""
    feed = _Feed()
    chat_steps = [_Step(_think("<A> chat visível")), _hold()]
    thread_a, _ = _run_in_thread(feed, _ScriptedClient(chat_steps, "<A> fim"))
    _advance(chat_steps[0])
    run_a = _run_ids(feed)["A"]

    def _delegate_then_cleanup() -> None:
        gateway = make_gateway(
            _ScriptedClient([], "resultado do delegate", driver_thread=False),
            sink=feed.controller,
        )
        gateway._renderer = feed.renderer
        gateway.call(
            AGENT,
            delegation={"delegation_id": "dlg:1"},
            delegation_only=True,
            protocol_mode="delegation",
            show_output=False,
        )
        cleanup_agent_transient_if_unowned(feed.renderer, AGENT)

    worker = threading.Thread(target=_delegate_then_cleanup, daemon=True)
    worker.start()
    worker.join(_WAIT)
    assert not worker.is_alive()
    assert feed.transient_runs() == {run_a: "<A> chat visível"}
    _advance(chat_steps[1])
    thread_a.join(_WAIT)
    assert feed.finals() == [(run_a, "<A> fim")]


def _mcp_socket_preview(feed: _Feed, trusted_run_id: str) -> None:
    """Tool de CLI chegando pelo socket MCP: servidor → executor → preview do client.

    O prelude autenticado do socket deixa ``agent_name`` e ``trusted_run_id``
    no estado da conexão; a tool roda numa thread do pool do servidor.
    """
    from types import SimpleNamespace

    from quimera.agents.client import AgentClient
    from quimera.runtime.mcp import MCPServer
    from quimera.runtime.models import ToolResult
    from tests.helpers import make_mcp_executor

    client = AgentClient(feed.renderer)
    preview_target = SimpleNamespace(set_tool_preview_callback=MagicMock())
    client.bind_tool_preview_callback(preview_target)
    preview = preview_target.set_tool_preview_callback.call_args.args[0]

    executor = make_mcp_executor(tool_names=["git_status"])

    def execute(call, progress_callback=None):
        preview(call.name, call.arguments, call.metadata)
        return ToolResult(ok=True, tool_name=call.name, content="ok")

    executor.execute.side_effect = execute
    server = MCPServer(executor)
    out = io.StringIO()
    setattr(out, "_mcp_state", {"agent_name": AGENT, "trusted_run_id": trusted_run_id})
    server.serve(
        stdin=io.StringIO(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "git_status", "arguments": {}},
            })
            + "\n"
        ),
        stdout=out,
    )
    server.shutdown()


def _tools_by_run(feed: _Feed) -> dict[str, list]:
    return {
        str(item.event.payload.get("run_id") or ""): list(item.event.payload.get("tools") or [])
        for item in feed.model.items
        if item.transient and item.event.kind == "agent_update"
    }


def test_mcp_tool_preview_from_server_thread_uses_trusted_run_id():
    """Tool de CLI via socket MCP chega por thread do servidor e acha o run certo."""
    feed = _Feed()
    a_steps = [_Step(_think("<A> chat")), _hold()]
    b_steps = [_Step(_think("<B> task")), _hold()]
    thread_a, _ = _run_in_thread(feed, _ScriptedClient(a_steps, "<A> fim"), show_final=False)
    _advance(a_steps[0])
    thread_b, _ = _run_in_thread(
        feed, _ScriptedClient(b_steps, "<B> fim"), prompt_kind=PromptKind.TASK_EXECUTOR, show_final=False
    )
    _advance(b_steps[0])
    ids = _run_ids(feed)

    _mcp_socket_preview(feed, ids["A"])
    previews = [event.payload.get("run_id") for event in feed.events if event.kind == "tool_preview"]
    assert previews == [ids["A"]]
    tools = _tools_by_run(feed)
    assert tools[ids["A"]] and not tools[ids["B"]]

    # run_id de MCP que não é execução conhecida do agente não escolhe cartão:
    # com duas abertas, nenhuma das duas recebe a tool.
    _mcp_socket_preview(feed, "internal_mcp:desconhecido")
    previews = [event.payload.get("run_id") for event in feed.events if event.kind == "tool_preview"]
    assert previews == [ids["A"], None]
    tools = _tools_by_run(feed)
    assert len(tools[ids["A"]]) == 1 and not tools[ids["B"]]

    _advance(a_steps[1])
    _advance(b_steps[1])
    thread_a.join(_WAIT)
    thread_b.join(_WAIT)


def test_mcp_tool_preview_with_unknown_run_id_uses_the_single_open_run():
    """Com uma só execução aberta, o run_id de MCP desconhecido não cria bloco à parte."""
    feed = _Feed()
    a_steps = [_Step(_think("<A> chat")), _hold()]
    thread_a, _ = _run_in_thread(feed, _ScriptedClient(a_steps, "<A> fim"), show_final=False)
    _advance(a_steps[0])
    run_a = _run_ids(feed)["A"]

    _mcp_socket_preview(feed, "internal_mcp:desconhecido")
    previews = [event.payload.get("run_id") for event in feed.events if event.kind == "tool_preview"]
    assert previews == [run_a]
    _advance(a_steps[1])
    thread_a.join(_WAIT)


@pytest.mark.parametrize("remember", [True, False])
def test_scope_binding_restores_and_optionally_remembers(remember):
    scope = AgentRunScope(agent=AGENT, run_id="run:x", transport="chat")

    def _probe():
        with bind_agent_run_scope(scope, remember=remember):
            inside = current_agent_run_scope(AGENT)
        return inside, current_agent_run_scope(AGENT), current_agent_run_scope("outro")

    inside, after, other = threading_result(_probe)
    assert inside is scope
    assert after is (scope if remember else None)
    assert other is None


def test_scope_binding_survives_exceptions_and_does_not_leak_to_new_threads():
    scope = AgentRunScope(agent=AGENT, run_id="run:y")

    def _probe():
        with pytest.raises(RuntimeError):
            with bind_agent_run_scope(scope):
                raise RuntimeError("falha do backend")
        seen_in_plain_thread = threading_result(lambda: current_agent_run_scope(AGENT))
        seen_in_propagated = threading_result(run_in_current_context(lambda: current_agent_run_scope(AGENT)))
        return current_agent_run_scope(AGENT), seen_in_plain_thread, seen_in_propagated

    recent, plain, propagated = threading_result(_probe)
    assert recent is scope
    assert plain is None
    assert propagated is scope


def _drain_and_probe(handler, ui_queue):
    handler.drain_ui_events(ui_queue)
    return current_agent_run_scope(AGENT)


def threading_result(fn):
    """Roda ``fn`` numa thread nova (contexto limpo) e devolve o retorno."""
    box: dict = {}

    def _target() -> None:
        box["value"] = fn()

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join(_WAIT)
    return box["value"]

"""Thinking ao vivo de delegações: parser, registry e enriquecimento do list_tasks."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quimera.app.agent_run_events import (
    AgentRunController,
    AgentRunEvent,
    AgentRunRegistry,
    ThinkingStreamParser,
)
from quimera.agents import AgentClient
from quimera.domain.task_states import Visibility
from quimera.runtime.config import ToolRuntimeConfig
from quimera.workspace import Workspace
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.tasks import TaskTools


# ── ThinkingStreamParser ─────────────────────────────────────────────────


def test_parser_extracts_complete_thinking_block():
    parser = ThinkingStreamParser()
    parser.feed("prefixo <think>preciso revisar o parser</think> resposta")

    assert parser.last_thinking == "preciso revisar o parser"


def test_parser_updates_partial_block_progressively():
    parser = ThinkingStreamParser()
    parser.feed("<thinking>primeira parte do raciocínio que ainda ")
    partial = parser.last_thinking
    parser.feed("continua em outro chunk e não fechou")

    assert partial.startswith("primeira parte")
    assert len(parser.last_thinking) > len(partial)


def test_parser_keeps_latest_block_after_multiple_blocks():
    parser = ThinkingStreamParser()
    parser.feed("<think>bloco antigo</think> meio <think>bloco novo</think>")

    assert parser.last_thinking == "bloco novo"


def test_parser_keeps_closed_block_while_answer_streams():
    parser = ThinkingStreamParser()
    parser.feed("<think>plano fechado</think>")
    parser.feed("agora a resposta final em texto corrido, sem tags")

    assert parser.last_thinking == "plano fechado"


def test_parser_stream_tail_is_bounded_fallback_without_tags():
    parser = ThinkingStreamParser(tail_limit=20)
    parser.feed("stdout bruto de um agente CLI sem tags de raciocínio")

    assert parser.last_thinking == ""
    assert parser.stream_tail == "sem tags de raciocínio"[-20:]
    assert len(parser.stream_tail) == 20


def test_parser_invokes_callback_with_stripped_thinking():
    published: list[str] = []
    parser = ThinkingStreamParser(on_thinking=published.append)
    parser.feed("<think>  ideia central  </think>")

    assert published[-1] == "ideia central"


def test_parser_handles_tag_split_across_chunks():
    parser = ThinkingStreamParser()
    parser.feed("<thi")
    parser.feed("nk>raciocínio dividido</th")
    parser.feed("ink>")

    assert parser.last_thinking == "raciocínio dividido"


def test_parser_publishes_answer_outside_thinking_progressively():
    answers: list[str] = []
    parser = ThinkingStreamParser(on_answer=answers.append)
    parser.feed("<think>plano</th")
    parser.feed("ink>Claro. O trabalho foi concentrado ")
    first = parser.answer_text
    parser.feed("na EXEC-013, seguindo o histórico de execução.")

    assert answers, "a resposta parcial deve sair antes do fim do stream"
    assert first and "plano" not in first and "<" not in first
    assert answers[-1].startswith("Claro. O trabalho foi concentrado na EXEC-013")
    assert parser.last_thinking == "plano"


def test_parser_answer_never_contains_split_open_tag():
    parser = ThinkingStreamParser(on_answer=lambda text: None)
    parser.feed("resposta antes do bloco <thi")
    assert "<thi" not in parser.answer_text
    parser.feed("nk>ideia</think> e depois")

    assert parser.answer_text == "resposta antes do bloco "
    assert parser.last_thinking == "ideia"


# ── AgentRunRegistry ─────────────────────────────────────────────────────


def _event(kind: str, *, text: str = "", run_id: str = "agentrun:1", delegation_id: str = "dlg-1"):
    return AgentRunEvent(
        kind,
        "codex",
        text=text,
        run_id=run_id,
        delegation_id=delegation_id,
        transport="delegate",
    )


def test_registry_accumulates_thinking_from_delta_events():
    registry = AgentRunRegistry()
    registry.record(_event("started"))
    registry.record(_event("delta", text="<think>vou analisar "))
    registry.record(_event("delta", text="o arquivo delegate.py</think>"))
    record = registry.record(_event("delta", text="Resposta parcial"))

    assert record.last_thinking == "vou analisar o arquivo delegate.py"


def test_registry_stream_tail_covers_agents_without_think_tags():
    registry = AgentRunRegistry()
    registry.record(_event("started"))
    record = registry.record(_event("delta", text="saída bruta de CLI"))

    assert record.last_thinking == ""
    assert record.stream_tail.endswith("saída bruta de CLI")


def test_registry_preserves_thinking_after_final_event_drops_parser():
    registry = AgentRunRegistry()
    registry.record(_event("delta", text="<think>plano</think>"))
    record = registry.record(_event("finished", text="resposta"))

    assert record.last_thinking == "plano"
    assert registry._stream_parsers == {}


def test_registry_ignores_late_delta_after_final_event():
    registry = AgentRunRegistry()
    registry.record(_event("delta", text="<think>plano</think>"))
    finished = registry.record(_event("finished", text="resposta"))

    record = registry.record(_event("delta", text="delta atrasado"))

    assert record is finished
    assert record.status == "finished"
    assert record.last_thinking == "plano"
    assert record.last_text == "resposta"
    assert record.event_count == 2


def test_find_by_delegation_returns_latest_run():
    clock = iter(range(1, 100))
    registry = AgentRunRegistry(clock=lambda: float(next(clock)))
    registry.record(_event("started", run_id="agentrun:a"))
    registry.record(_event("failed", run_id="agentrun:a"))
    registry.record(_event("started", run_id="agentrun:b"))

    record = registry.find_by_delegation("dlg-1")

    assert record is not None
    assert record.run_id == "agentrun:b"
    assert registry.find_by_delegation("dlg-inexistente") is None
    assert registry.find_by_delegation("") is None


def test_live_delegation_view_exposes_thinking_and_age():
    now = {"t": 10.0}
    registry = AgentRunRegistry(clock=lambda: now["t"])
    registry.record(_event("started"))
    registry.record(_event("delta", text="<think>último raciocínio</think>"))
    now["t"] = 12.5

    view = registry.live_delegation_view("dlg-1")

    assert view == {
        "agent": "codex",
        "status": "running",
        "last_thinking": "último raciocínio",
        "updated_seconds_ago": 2.5,
    }
    assert registry.live_delegation_view("dlg-x") is None


def test_live_delegation_view_falls_back_to_stream_tail():
    registry = AgentRunRegistry()
    registry.record(_event("delta", text="raciocínio sem tags direto no stdout"))

    view = registry.live_delegation_view("dlg-1")

    assert view is not None
    assert view["last_thinking"].endswith("direto no stdout")


def test_registry_activity_refreshes_run_without_overwriting_thinking():
    """Tool activity mantém o run vivo sem ser confundida com reasoning."""
    now = {"t": 1.0}
    registry = AgentRunRegistry(clock=lambda: now["t"])
    registry.record(_event("started"))
    registry.record(_event("delta", text="<think>vou revisar o fluxo</think>"))

    now["t"] = 20.0
    registry.record(_event("activity", text="$ rg -n last_thinking quimera"))
    now["t"] = 23.0

    view = registry.live_delegation_view("dlg-1")

    assert view == {
        "agent": "codex",
        "status": "running",
        "last_thinking": "vou revisar o fluxo",
        "last_activity": "$ rg -n last_thinking quimera",
        "updated_seconds_ago": 3.0,
    }


def test_registry_ignores_late_activity_after_final_event():
    registry = AgentRunRegistry()
    registry.record(_event("activity", text="$ pytest"))
    finished = registry.record(_event("finished", text="ok"))

    record = registry.record(_event("activity", text="✓ pytest"))

    assert record is finished
    assert record.status == "finished"
    assert record.last_activity == "$ pytest"


def test_registry_prune_drops_parser_state():
    registry = AgentRunRegistry(max_runs=1)
    registry.record(_event("delta", text="<think>a</think>", run_id="agentrun:a", delegation_id="dlg-a"))
    registry.record(_event("finished", run_id="agentrun:a", delegation_id="dlg-a"))
    registry.record(_event("delta", text="<think>b</think>", run_id="agentrun:b", delegation_id="dlg-b"))

    assert registry.get("agentrun:a") is None
    assert "agentrun:a" not in registry._stream_parsers


# ── AgentGateway: normalização de chunks dict nos deltas ────────────────


def test_gateway_delta_extracts_text_from_dict_chunks():
    from tests.test_agent_run_events import FakeAgentClient, RecordingSink, make_gateway

    sink = RecordingSink()
    gateway = make_gateway(
        FakeAgentClient(chunks=[{"text": "<think>pensando</think>"}, "texto plano"]),
        sink=sink,
    )
    gateway.call("codex", silent=True, show_output=False)

    deltas = [event.text for event in sink.events if event.kind == "delta"]
    assert deltas == ["<think>pensando</think>", "texto plano"]


def test_codexcloud_thinking_reaches_feed_via_delegate_step():
    """Codexcloud chamado pelo caminho real do step de delegate mantém thinking.

    O driver codexcloud entrega reasoning/commentary ao AgentClient como blocos
    <think>. O delegate força show_output=False para não duplicar a resposta
    final; esse detalhe não pode também suprimir o thinking transitório.
    """
    from tests.test_agent_run_events import FakeAgentClient, RecordingSink, make_gateway
    from quimera.runtime.tools.delegate import DelegateTools

    renderer = MagicMock()
    gateway = make_gateway(
        FakeAgentClient(
            chunks=[
                "<think>hop 0 antes da tool</think>",
                "<think>hop 1 após a tool</think>",
                "resposta final",
            ]
        ),
        sink=RecordingSink(),
    )
    gateway._renderer = renderer

    forwarded = {}

    def delegate_fn(agent, **options):
        forwarded.update(options)
        gateway_options = {
            key: options[key]
            for key in (
                "delegation",
                "delegation_only",
                "protocol_mode",
                "primary",
                "silent",
                "show_output",
                "history_snapshot",
                "from_agent",
                "progress_callback",
            )
        }
        return gateway.call(agent, **gateway_options)

    selected, result, error = DelegateTools._execute_single_step(
        {
            "target_agent": "codexcloud-gpt-5-6",
            "request": "investigue o feed",
            "context": "",
            "fallback_agents": [],
            "source_agent": "claude-sonnet",
            "delegation_id": "dlg-api-live",
        },
        delegate_fn,
        progress_callback=None,
        normalize_agent_fn=lambda value: str(value),
    )

    assert (selected, result, error) == (
        "codexcloud-gpt-5-6",
        "resposta final",
        None,
    )
    assert forwarded["silent"] is False
    assert forwarded["show_output"] is False
    update_calls = renderer.update_agent_transient.call_args_list
    transient_texts = [call.args[1] for call in update_calls]
    assert "hop 0 antes da tool" in transient_texts
    assert "hop 1 após a tool" in transient_texts
    assert "resposta final" not in transient_texts
    assert {call.kwargs["run_id"] for call in update_calls} == {
        renderer.clear_agent_transient.call_args.kwargs["run_id"]
    }
    assert all(call.kwargs["delegation_id"] == "dlg-api-live" for call in update_calls)
    assert all(call.kwargs["transport"] == "delegate" for call in update_calls)
    assert renderer.clear_agent_transient.call_args.args == ("codexcloud-gpt-5-6",)
    assert renderer.clear_agent_transient.call_args.kwargs["delegation_id"] == "dlg-api-live"


def test_gateway_silent_delegation_keeps_feed_quiet():
    from tests.test_agent_run_events import FakeAgentClient, RecordingSink, make_gateway

    renderer = MagicMock()
    gateway = make_gateway(
        FakeAgentClient(chunks=["<think>raciocínio oculto</think>"]),
        sink=RecordingSink(),
    )
    gateway._renderer = renderer

    gateway.call(
        "codexcloud-gpt-5-6",
        delegation={"delegation_id": "dlg-silent"},
        delegation_only=True,
        protocol_mode="delegation",
        silent=True,
        show_output=False,
    )

    renderer.update_agent_transient.assert_not_called()
    renderer.clear_agent_transient.assert_not_called()


def test_gateway_quiet_visibility_suppresses_delegated_thinking_but_keeps_deltas():
    """Quiet oculta narrativa no feed sem perder eventos estruturados do run."""
    from tests.test_agent_run_events import FakeAgentClient, RecordingSink, make_gateway

    renderer = MagicMock()
    sink = RecordingSink()
    client = FakeAgentClient(chunks=["<think>raciocínio estruturado</think>"])
    client.visibility = Visibility.QUIET
    gateway = make_gateway(client, sink=sink)
    gateway._renderer = renderer

    gateway.call(
        "codexcloud-gpt-5-6",
        delegation={"delegation_id": "dlg-quiet"},
        delegation_only=True,
        protocol_mode="delegation",
        silent=False,
        show_output=False,
    )

    deltas = [event.text for event in sink.events if event.kind == "delta"]
    assert deltas == ["<think>raciocínio estruturado</think>"]
    renderer.update_agent_transient.assert_not_called()
    renderer.clear_agent_transient.assert_not_called()


def test_gateway_delegation_skips_cli_semantic_chunks():
    """CLI delegado já renderiza via spy; o relay não pode duplicar o transitório."""
    from tests.test_agent_run_events import FakeAgentClient, RecordingSink, make_gateway

    renderer = MagicMock()
    gateway = make_gateway(
        FakeAgentClient(
            chunks=[
                {
                    "text": "<think>já exibido pelo CLI</think>",
                    "_quimera_cli_semantic": True,
                }
            ]
        ),
        sink=RecordingSink(),
    )
    gateway._renderer = renderer

    gateway.call(
        "codex",
        delegation={"delegation_id": "dlg-cli-sem"},
        delegation_only=True,
        protocol_mode="delegation",
        silent=False,
        show_output=False,
    )

    renderer.update_agent_transient.assert_not_called()
    renderer.clear_agent_transient.assert_not_called()


def test_gateway_chat_shows_partial_answer_while_api_agent_streams():
    """A resposta de agentes de API aparece no transitório antes do fim do turno."""
    from tests.test_agent_run_events import FakeAgentClient, RecordingSink, make_gateway

    renderer = MagicMock()
    seen_before_return = []

    class StreamingClient(FakeAgentClient):
        def call(self, agent, prompt, *, on_text_chunk=None, **kwargs):
            on_text_chunk("<think>planejando</think>")
            on_text_chunk("Claro. O trabalho foi concentrado na EXEC-013 ")
            on_text_chunk("e no histórico de execução.")
            seen_before_return.extend(renderer.update_agent_transient.call_args_list)
            return "Claro. O trabalho foi concentrado na EXEC-013 e no histórico de execução."

    gateway = make_gateway(StreamingClient(), sink=RecordingSink())
    gateway._renderer = renderer

    gateway.call("chatgpt", silent=False, show_output=True)

    thinking = [c for c in seen_before_return if not c.kwargs.get("answer")]
    answers = [c for c in seen_before_return if c.kwargs.get("answer")]
    assert thinking and thinking[0].args == ("chatgpt", "planejando")
    assert answers, "a resposta parcial deve chegar ao feed antes do retorno"
    assert answers[-1].args[0] == "chatgpt"
    assert answers[-1].args[1].startswith("Claro. O trabalho foi concentrado na EXEC-013")


def test_gateway_hidden_delegate_does_not_expose_partial_answer():
    """Delegação oculta mostra o raciocínio, mas a resposta fica com quem chamou."""
    from tests.test_agent_run_events import FakeAgentClient, RecordingSink, make_gateway

    renderer = MagicMock()
    gateway = make_gateway(
        FakeAgentClient(chunks=["<think>raciocínio visível</think>", "resposta reservada ao chamador"]),
        sink=RecordingSink(),
    )
    gateway._renderer = renderer

    gateway.call(
        "codexcloud-gpt-5-6",
        delegation={"delegation_id": "dlg-answer"},
        delegation_only=True,
        protocol_mode="delegation",
        silent=False,
        show_output=False,
    )

    for call in renderer.update_agent_transient.call_args_list:
        assert not call.kwargs.get("answer")
        assert "resposta reservada" not in str(call.args)


def test_gateway_non_delegate_show_output_false_does_not_expose_thinking():
    """show_output=False mantém seu contrato fora do transporte delegate."""
    from tests.test_agent_run_events import FakeAgentClient, RecordingSink, make_gateway

    renderer = MagicMock()
    gateway = make_gateway(
        FakeAgentClient(chunks=["<think>conteúdo que deve ficar oculto</think>"]),
        sink=RecordingSink(),
    )
    gateway._renderer = renderer

    gateway.call(
        "codexcloud-gpt-5-6",
        silent=False,
        show_output=False,
    )

    renderer.update_agent_transient.assert_not_called()
    renderer.clear_agent_transient.assert_not_called()


def test_gateway_failed_delegate_clears_partial_thinking():
    """Falha do backend não deixa o thinking já publicado preso no feed."""
    from tests.test_agent_run_events import FakeAgentClient, RecordingSink, make_gateway

    class PartialFailureClient(FakeAgentClient):
        def call(self, agent, prompt, *, on_text_chunk=None, **kwargs):
            del agent, prompt, kwargs
            if on_text_chunk is not None:
                on_text_chunk("<think>análise antes da falha</think>")
            raise RuntimeError("falha simulada")

    renderer = MagicMock()
    gateway = make_gateway(PartialFailureClient(), sink=RecordingSink())
    gateway._renderer = renderer

    with pytest.raises(RuntimeError, match="falha simulada"):
        gateway.call(
            "codexcloud-gpt-5-6",
            delegation={"delegation_id": "dlg-failure"},
            delegation_only=True,
            protocol_mode="delegation",
            silent=False,
            show_output=False,
        )

    update_call = renderer.update_agent_transient.call_args
    clear_call = renderer.clear_agent_transient.call_args
    assert update_call.args == (
        "codexcloud-gpt-5-6",
        "análise antes da falha",
    )
    assert clear_call.args == ("codexcloud-gpt-5-6",)
    assert update_call.kwargs["run_id"] == clear_call.kwargs["run_id"]
    assert update_call.kwargs["delegation_id"] == "dlg-failure"
    assert clear_call.kwargs["delegation_id"] == "dlg-failure"


def test_concurrent_delegate_runs_are_scoped_in_textual_renderer():
    """Dois gateways reais não atribuem update/cleanup ao run concorrente."""
    from quimera.app.agent_gateway import cleanup_agent_transient_if_unowned
    from quimera.app.agent_run_events import AgentRunController
    from quimera.ui.textual.bridge import TextualUiBridge
    from quimera.ui.textual.renderer import TextualRenderer
    from tests.test_agent_run_events import FakeAgentClient, make_gateway

    old_published = threading.Event()
    release_old = threading.Event()
    new_backend_entered = threading.Event()
    release_new_thinking = threading.Event()

    class OldClient(FakeAgentClient):
        def call(self, agent, prompt, *, on_text_chunk=None, **kwargs):
            del agent, prompt, kwargs
            on_text_chunk("<think>thinking A</think>")
            old_published.set()
            assert release_old.wait(2)
            return "resposta A"

    class NewClient(FakeAgentClient):
        def call(self, agent, prompt, *, on_text_chunk=None, **kwargs):
            del agent, prompt, kwargs
            new_backend_entered.set()
            assert release_new_thinking.wait(2)
            on_text_chunk("<think>thinking B</think>")
            return "resposta B"

    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    bridge.clear_agent_active = lambda _agent: None
    renderer = TextualRenderer(bridge)
    renderer.flush = lambda timeout=5.0: None
    sink = AgentRunController(renderer)

    old_gateway = make_gateway(OldClient(), sink=sink)
    new_gateway = make_gateway(NewClient(), sink=sink)
    old_gateway._renderer = renderer
    new_gateway._renderer = renderer
    errors = []

    def run(gateway, delegation):
        try:
            gateway.call(
                "codexcloud-gpt-5-6",
                delegation=delegation,
                delegation_only=True,
                protocol_mode="delegation",
                silent=False,
                show_output=False,
            )
        except Exception as exc:  # pragma: no cover - deixa falha da thread visível
            errors.append(exc)

    old_thread = threading.Thread(
        target=run,
        args=(
            old_gateway,
            {"delegation_id": "dlg:A", "run_id": "run:A"},
        ),
    )
    new_thread = threading.Thread(
        target=run,
        args=(
            new_gateway,
            {
                "delegation_id": "dlg:B",
                "run_id": "run:B",
                "parent_run_id": "run:parent-B",
            },
        ),
    )

    old_thread.start()
    assert old_published.wait(1)
    new_thread.start()
    assert new_backend_entered.wait(1)

    release_old.set()
    old_thread.join(2)
    assert not old_thread.is_alive()
    assert not errors
    assert renderer._agent_run_context("codexcloud-gpt-5-6")["run_id"] == "run:B"

    # B já está ativo, mas ainda não publicou thinking. O cleanup externo do
    # step A não pode remover seu contexto/transitório futuro.
    assert cleanup_agent_transient_if_unowned(
        renderer, "codexcloud-gpt-5-6"
    ) is False
    assert renderer._agent_run_context("codexcloud-gpt-5-6")["run_id"] == "run:B"

    release_new_thinking.set()
    new_thread.join(2)
    assert not new_thread.is_alive()
    assert not errors

    updates = [event for event in emitted if event.kind == "agent_update"]
    resets = [event for event in emitted if event.kind == "visual_reset"]
    assert [(event.payload["run_id"], event.payload["content"]) for event in updates] == [
        ("run:A", "thinking A"),
        ("run:B", "thinking B"),
    ]
    assert [event.payload["run_id"] for event in resets] == ["run:A", "run:B"]
    assert "parent_run_id" not in resets[0].payload
    assert resets[1].payload["parent_run_id"] == "run:parent-B"


def test_terminal_renderer_scopes_delegate_transient_cleanup_by_run():
    """No terminal, cleanup antigo não apaga o thinking do run mais novo."""
    from rich.console import Console
    from quimera.ui import TerminalRenderer

    renderer = TerminalRenderer(theme="line")
    renderer._console = Console(width=100, record=True, force_terminal=False)
    try:
        renderer.update_agent_transient(
            "codexcloud-gpt-5-6", "thinking A", run_id="run:A"
        )
        renderer.update_agent_transient(
            "codexcloud-gpt-5-6", "thinking B", run_id="run:B"
        )

        renderer.clear_agent_transient("codexcloud-gpt-5-6", run_id="run:A")
        container = renderer._deck.get("codexcloud-gpt-5-6")
        assert container is not None
        assert container.transient_run_id == "run:B"
        assert container.transient == ["thinking B"]

        renderer.clear_agent_transient("codexcloud-gpt-5-6", run_id="run:B")
        assert container.transient_run_id == ""
        assert container.transient == []
    finally:
        renderer.close(timeout=1.0)


def test_gateway_does_not_render_cli_semantic_chunk_twice():
    """O spy do AgentClient já renderiza CLI; gateway só registra seu delta."""
    from tests.test_agent_run_events import FakeAgentClient, RecordingSink, make_gateway

    renderer = MagicMock()
    sink = RecordingSink()
    gateway = make_gateway(
        FakeAgentClient(
            chunks=[
                {
                    "text": "<think>pensamento já exibido pelo CLI</think>",
                    "_quimera_cli_semantic": True,
                }
            ]
        ),
        sink=sink,
    )
    gateway._renderer = renderer

    gateway.call("codex", silent=False, show_output=True)

    assert [event.text for event in sink.events if event.kind == "delta"] == [
        "<think>pensamento já exibido pelo CLI</think>"
    ]
    renderer.update_agent_transient.assert_not_called()


def test_cli_delegate_updates_live_thinking_before_process_finishes():
    """Cobre CLI stdout → gateway delta → registry durante a execução."""
    from tests.test_agent_run_events import make_gateway

    release_final = threading.Event()

    def stdout_lines():
        yield '{"type":"item.started","item":{"type":"reasoning","summary":"Inspecionando o fluxo CLI"}}\n'
        assert release_final.wait(2)
        yield '{"type":"item.completed","item":{"type":"agent_message","text":"Fluxo validado"}}\n'

    proc = MagicMock()
    proc.stdout = stdout_lines()
    proc.stderr = iter([])
    proc.returncode = 0
    proc.stdin = MagicMock()
    renderer = MagicMock()
    client = AgentClient(renderer)
    registry = AgentRunRegistry()
    gateway = make_gateway(client, sink=AgentRunController(registry=registry))
    result = {}

    with patch("subprocess.Popen", return_value=proc), patch.object(
        client, "_should_use_warm_pool", return_value=False
    ):
        worker = threading.Thread(
            target=lambda: result.setdefault(
                "value",
                gateway.call(
                    "codex",
                    delegation={"delegation_id": "dlg-cli-live"},
                    delegation_only=True,
                    protocol_mode="delegation",
                    silent=False,
                    show_output=False,
                ),
            )
        )
        worker.start()
        deadline = time.monotonic() + 2
        live = None
        while time.monotonic() < deadline:
            live = registry.live_delegation_view("dlg-cli-live")
            if live and live["last_thinking"]:
                break
            time.sleep(0.01)

        assert live is not None
        assert live["status"] == "running"
        assert live["last_thinking"] == "Inspecionando o fluxo CLI"
        release_final.set()
        worker.join(3)

    assert not worker.is_alive()
    assert result["value"] == "Fluxo validado"


def test_silent_cli_delegate_tracks_tool_activity_before_process_finishes():
    """Tool activity continua observável mesmo sem o pipeline visual do presenter."""
    from tests.test_agent_run_events import make_gateway

    release_final = threading.Event()

    def stdout_lines():
        yield '{"type":"item.started","item":{"type":"reasoning","summary":"Vou revisar o fluxo"}}\n'
        yield (
            '{"type":"item.started","item":{"type":"command_execution",'
            '"command":"rg -n last_thinking quimera","id":"tool-1"}}\n'
        )
        assert release_final.wait(2)
        yield (
            '{"type":"item.completed","item":{"type":"command_execution",'
            '"command":"rg -n last_thinking quimera","exit_code":0,"id":"tool-1"}}\n'
        )
        yield '{"type":"item.completed","item":{"type":"agent_message","text":"Fluxo validado"}}\n'

    proc = MagicMock()
    proc.stdout = stdout_lines()
    proc.stderr = iter([])
    proc.returncode = 0
    proc.stdin = MagicMock()
    renderer = MagicMock()
    client = AgentClient(renderer)
    registry = AgentRunRegistry()
    gateway = make_gateway(client, sink=AgentRunController(registry=registry))
    result = {}

    with patch("subprocess.Popen", return_value=proc), patch.object(
        client, "_should_use_warm_pool", return_value=False
    ):
        worker = threading.Thread(
            target=lambda: result.setdefault(
                "value",
                gateway.call(
                    "codex",
                    delegation={"delegation_id": "dlg-cli-tool-live"},
                    delegation_only=True,
                    protocol_mode="delegation",
                    silent=True,
                    show_output=False,
                ),
            )
        )
        worker.start()
        deadline = time.monotonic() + 2
        live = None
        while time.monotonic() < deadline:
            live = registry.live_delegation_view("dlg-cli-tool-live")
            if live and live.get("last_activity"):
                break
            time.sleep(0.01)

        assert live is not None
        assert live["status"] == "running"
        assert live["last_thinking"] == "Vou revisar o fluxo"
        assert live["last_activity"] == "$ rg -n last_thinking quimera"
        assert live["updated_seconds_ago"] < 1

        release_final.set()
        worker.join(3)

    assert not worker.is_alive()
    assert result["value"] == "Fluxo validado"


# ── TaskTools.list_tasks: campo live ─────────────────────────────────────


@pytest.fixture
def task_tools():
    config = ToolRuntimeConfig(workspace=Workspace(Path("/tmp")))
    return TaskTools(config)


def _delegate_task_row(status: str = "in_progress", body: str | None = None) -> dict:
    steps = [
        {"target_agent": "codex", "request": "revisar", "delegation_id": "dlg-1"},
        {"target_agent": "claude", "request": "testar", "delegation_id": "dlg-2"},
    ]
    return {
        "id": 7,
        "job_id": 3,
        "description": "revisar",
        "status": status,
        "origin": "delegate",
        "body": json.dumps(steps) if body is None else body,
    }


@patch("quimera.runtime.tools.tasks._list_tasks")
def test_list_tasks_attaches_live_thinking_for_running_delegations(mock_list, task_tools):
    mock_list.return_value = [_delegate_task_row()]
    views = {
        "dlg-1": {
            "agent": "codex",
            "status": "running",
            "last_thinking": "analisando o diff",
            "last_activity": "$ git diff",
            "updated_seconds_ago": 1.2,
        },
    }
    task_tools.set_delegation_run_lookup(views.get)

    result = task_tools.list_tasks(ToolCall(name="list_tasks", arguments={"id": 7}))

    assert result.ok is True
    tasks = json.loads(result.content)
    assert tasks[0]["live"] == [
        {
            "delegation_id": "dlg-1",
            "agent": "codex",
            "status": "running",
            "last_thinking": "analisando o diff",
            "last_activity": "$ git diff",
            "updated_seconds_ago": 1.2,
        },
    ]


@patch("quimera.runtime.tools.tasks._list_tasks")
def test_list_tasks_limits_live_thinking_excerpt_keeping_tail(mock_list, task_tools):
    mock_list.return_value = [_delegate_task_row()]
    long_thinking = "x" * 500 + "FINAL"
    task_tools.set_delegation_run_lookup(
        lambda dlg: {"agent": "codex", "status": "running", "last_thinking": long_thinking}
        if dlg == "dlg-1"
        else None
    )

    result = task_tools.list_tasks(ToolCall(name="list_tasks", arguments={"id": 7}))

    excerpt = json.loads(result.content)[0]["live"][0]["last_thinking"]
    assert excerpt.startswith("…")
    assert excerpt.endswith("FINAL")
    assert len(excerpt) == 401


@patch("quimera.runtime.tools.tasks._list_tasks")
def test_list_tasks_does_not_enrich_finished_or_foreign_tasks(mock_list, task_tools):
    mock_list.return_value = [
        _delegate_task_row(status="completed"),
        {"id": 8, "status": "in_progress", "origin": "user", "body": ""},
    ]
    task_tools.set_delegation_run_lookup(
        lambda dlg: {"agent": "codex", "status": "running", "last_thinking": "x"}
    )

    result = task_tools.list_tasks(ToolCall(name="list_tasks", arguments={"job_id": 3}))

    tasks = json.loads(result.content)
    assert all("live" not in task for task in tasks)


@patch("quimera.runtime.tools.tasks._list_tasks")
def test_list_tasks_survives_lookup_failure_and_bad_body(mock_list, task_tools):
    def boom(_dlg):
        raise RuntimeError("registry indisponível")

    mock_list.return_value = [
        _delegate_task_row(),
        _delegate_task_row(body="não é json"),
    ]
    task_tools.set_delegation_run_lookup(boom)

    result = task_tools.list_tasks(ToolCall(name="list_tasks", arguments={"job_id": 3}))

    assert result.ok is True
    assert all("live" not in task for task in json.loads(result.content))


@patch("quimera.runtime.tools.tasks._list_tasks")
def test_list_tasks_unchanged_without_injected_lookup(mock_list, task_tools):
    mock_list.return_value = [_delegate_task_row()]

    result = task_tools.list_tasks(ToolCall(name="list_tasks", arguments={"id": 7}))

    assert result.ok is True
    assert "live" not in json.loads(result.content)[0]

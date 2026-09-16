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

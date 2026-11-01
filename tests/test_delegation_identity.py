"""Identidade das chamadas de tool: tool_call_id do executor ao evento de delegação."""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from quimera.agents.client import AgentClient
from quimera.domain.task_states import Visibility
from quimera.runtime.approval_broker import TrustedToolExecutionContext
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.delegate import DelegateTools
from quimera.workspace import Workspace
from tests.legacy_app_adapters import dispatch_services_from_app


@pytest.fixture
def executor(tmp_path):
    return ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())


def _capture_previews(executor):
    seen = []
    executor.set_tool_preview_callback(
        lambda name, args, metadata=None: seen.append((name, args, metadata))
    )
    return seen


class TestExecutorToolCallId:
    def test_call_id_do_provedor_vira_tool_call_id(self, executor):
        seen = _capture_previews(executor)

        executor.execute(ToolCall(name="list_files", arguments={"path": "."}, call_id="call_abc"))

        assert seen[0][2]["tool_call_id"] == "call_abc"

    def test_mcp_msg_id_ganha_prefixo_do_run(self, executor):
        seen = _capture_previews(executor)
        context = TrustedToolExecutionContext(run_id="run-9", transport="stdio_mcp", server_origin="mcp_stdio")

        executor.execute(
            ToolCall(
                name="list_files",
                arguments={"path": "."},
                metadata={"trusted_context": context, "mcp_msg_id": 7},
            )
        )

        assert seen[0][2]["tool_call_id"] == "mcp:run-9:7"

    def test_mcp_sem_run_usa_sessao(self, executor):
        seen = _capture_previews(executor)
        context = TrustedToolExecutionContext(session_id="sess-1", server_origin="mcp_http")

        executor.execute(
            ToolCall(
                name="list_files",
                arguments={"path": "."},
                metadata={"trusted_context": context, "mcp_msg_id": 0},
            )
        )

        assert seen[0][2]["tool_call_id"] == "mcp:sess-1:0"

    def test_sem_identidade_nao_inventa_id(self, executor):
        seen = _capture_previews(executor)

        executor.execute(ToolCall(name="list_files", arguments={"path": "."}))

        assert "tool_call_id" not in seen[0][2]


class TestPreviewPayload:
    def test_transporte_nao_mcp_http_carrega_tool_call_id(self):
        context = AgentClient._tool_preview_context({"tool_call_id": "call_1"})

        assert context == {"tool_call_id": "call_1"}

    def test_mcp_http_mantem_identidade_e_adiciona_tool_call_id(self):
        trusted = TrustedToolExecutionContext(run_id="r1", transport="http_mcp", server_origin="mcp_http")

        context = AgentClient._tool_preview_context(
            {"trusted_context": trusted, "tool_call_id": "mcp:r1:3", "mcp_msg_id": 3}
        )

        assert context["tool_call_id"] == "mcp:r1:3"
        assert context["run_id"] == "r1"

    def test_show_tool_preview_envia_payload_ao_renderer(self):
        renderer = MagicMock()
        renderer.supports_agent_feed = True
        fake = SimpleNamespace(
            visibility=Visibility.FULL,
            renderer=renderer,
            _tool_preview_context=AgentClient._tool_preview_context,
        )

        AgentClient._show_tool_preview(
            fake, "list_files", agent="codex", metadata={"tool_call_id": "call_2"}
        )

        payload = renderer.show_tool_preview.call_args.args[0]
        assert payload == {"content": "list_files", "tool_call_id": "call_2"}


class TestDelegateStepIdentity:
    def _tools(self, tmp_path):
        tools = DelegateTools(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
        tools._get_db_path = lambda: None
        fn = MagicMock(return_value="ok")
        tools.set_delegate_fn(fn)
        return tools, fn

    def test_passo_unico_propaga_ids(self, tmp_path):
        tools, fn = self._tools(tmp_path)
        context = TrustedToolExecutionContext(agent_name="claude", run_id="run-1")
        call = ToolCall(
            name="tasks_delegate",
            arguments={"target_agent": "codex", "request": "faz"},
            call_id="call_7",
            metadata={"trusted_context": context},
        )

        tools.tasks_delegate(call)

        delegation = fn.call_args.kwargs["delegation"]
        assert delegation["tool_call_id"] == "call_7"
        assert delegation["source_run_id"] == "run-1"
        assert delegation["step_index"] == 0

    def test_tool_call_id_vem_da_metadata_quando_nao_ha_call_id(self, tmp_path):
        tools, fn = self._tools(tmp_path)
        context = TrustedToolExecutionContext(agent_name="claude", run_id="run-2")
        call = ToolCall(
            name="tasks_delegate",
            arguments={"target_agent": "codex", "request": "faz"},
            metadata={"trusted_context": context, "tool_call_id": "mcp:run-2:4"},
        )

        tools.tasks_delegate(call)

        assert fn.call_args.kwargs["delegation"]["tool_call_id"] == "mcp:run-2:4"

    def test_steps_recebem_index_proprio(self, tmp_path):
        tools, fn = self._tools(tmp_path)
        context = TrustedToolExecutionContext(agent_name="claude", run_id="run-3")
        call = ToolCall(
            name="tasks_delegate",
            arguments={
                "target_agent": "codex",
                "request": "primeiro",
                "steps": [{"target_agent": "gemini", "request": "segundo"}],
            },
            call_id="call_8",
            metadata={"trusted_context": context},
        )

        tools.tasks_delegate(call)

        delegations = [c.kwargs["delegation"] for c in fn.call_args_list]
        assert sorted(d["step_index"] for d in delegations) == [0, 1]
        assert {d["tool_call_id"] for d in delegations} == {"call_8"}
        assert {d["source_run_id"] for d in delegations} == {"run-3"}


class TestDispatchForwardsIdentity:
    def _services(self):
        app = MagicMock()
        app.agent_client._cancel_event = threading.Event()
        app.session_state = {"session_id": "s", "delegations_sent": 0, "delegations_received": 0}
        app.shared_state = {}
        app.history = []
        app.MAX_RETRIES = 1
        app.RETRY_BACKOFF_SECONDS = 0.01
        return dispatch_services_from_app(app)

    def test_delegate_repassa_ids_ao_show_delegation(self):
        ds = self._services()
        delegation = {
            "delegation_id": "d1",
            "task": "faz",
            "tool_call_id": "call_9",
            "source_run_id": "run-5",
        }
        with (
            patch.object(ds, "_show_delegation") as show,
            patch.object(ds, "delegate_low_level", return_value="ok"),
            patch.object(ds, "resolve_agent_response", return_value="ok"),
        ):
            ds._delegate_reserved("codex", from_agent="claude", delegation=delegation)

        assert show.call_args.kwargs["tool_call_id"] == "call_9"
        assert show.call_args.kwargs["source_run_id"] == "run-5"

    def test_renderer_direto_recebe_kwargs(self):
        ds = self._services()
        renderer = MagicMock()
        ds._ui_queue = None
        ds._renderer = lambda: renderer

        ds._show_delegation(
            "claude", "codex", "faz", delegation_id="d1", chain=["claude", "codex"],
            tool_call_id="call_9", source_run_id="run-5",
        )

        renderer.show_delegation.assert_called_once_with(
            "claude", "codex", task="faz", delegation_id="d1", chain=["claude", "codex"],
            tool_call_id="call_9", source_run_id="run-5",
        )

    def test_fila_da_tui_carrega_ids_no_evento(self):
        ds = self._services()
        queue = MagicMock()
        ds._ui_queue = queue

        ds._show_delegation("claude", "codex", "faz", tool_call_id="call_9", source_run_id="run-5")

        event = queue.put.call_args.args[0]
        assert event.metadata["tool_call_id"] == "call_9"
        assert event.metadata["source_run_id"] == "run-5"

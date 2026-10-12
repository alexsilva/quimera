"""Testes do deferral de schemas de tools e da tool de descoberta tool_search."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.drivers.openai_compat import ToolCallingDriver
from quimera.runtime.drivers.tool_schemas import TOOL_SCHEMAS
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.tool_deferral import (
    DEFERRAL_OVERRIDE_STATE_KEY,
    DEFERRAL_STATE_KEY,
    ESSENTIAL_TOOLS,
    TOOL_SEARCH_TOOL_NAME,
    build_exposed_schemas,
    is_api_tool_deferral_enabled,
    is_mcp_tool_deferral_enabled,
    match_deferred_schemas,
    normalized_tool_deferral_mode,
)
from quimera.workspace import Workspace
from tests.helpers import make_mcp_server


def _schema_names(schemas: list[dict]) -> list[str]:
    return [s["function"]["name"] for s in schemas]


def _make_real_executor(tmp_path) -> ToolExecutor:
    config = ToolRuntimeConfig(workspace=Workspace(tmp_path))
    return ToolExecutor(config, MagicMock())


# ---------------------------------------------------------------------------
# Módulo tool_deferral (puro)
# ---------------------------------------------------------------------------

class TestBuildExposedSchemas:
    def test_expoe_essenciais_e_tool_search_com_catalogo(self):
        exposed = build_exposed_schemas(list(TOOL_SCHEMAS), ())
        names = _schema_names(exposed)

        assert set(names) == ESSENTIAL_TOOLS | {TOOL_SEARCH_TOOL_NAME}
        search = exposed[-1]
        assert search["function"]["name"] == TOOL_SEARCH_TOOL_NAME
        # O catálogo das diferidas viaja na descrição do tool_search.
        assert "tasks_delegate" in search["function"]["description"]
        assert "host_memory" in search["function"]["description"]

    def test_habilitadas_entram_na_visao_e_saem_do_catalogo(self):
        exposed = build_exposed_schemas(list(TOOL_SCHEMAS), {"tasks_delegate"})
        names = _schema_names(exposed)

        assert "tasks_delegate" in names
        search = next(
            s for s in exposed if s["function"]["name"] == TOOL_SEARCH_TOOL_NAME
        )
        assert "- delegate:" not in search["function"]["description"]
        assert "host_memory" in search["function"]["description"]

    def test_sem_tool_search_no_catalogo_devolve_intacto(self):
        schemas = [s for s in TOOL_SCHEMAS if s["function"]["name"] != TOOL_SEARCH_TOOL_NAME]
        assert build_exposed_schemas(schemas, ()) == schemas

    def test_sem_diferidas_omite_tool_search(self):
        schemas = [
            s
            for s in TOOL_SCHEMAS
            if s["function"]["name"] in (ESSENTIAL_TOOLS | {TOOL_SEARCH_TOOL_NAME})
        ]
        exposed = build_exposed_schemas(schemas, ())
        assert TOOL_SEARCH_TOOL_NAME not in _schema_names(exposed)

    def test_nao_muta_schema_original_do_tool_search(self):
        original = next(
            s for s in TOOL_SCHEMAS if s["function"]["name"] == TOOL_SEARCH_TOOL_NAME
        )
        before = original["function"]["description"]
        build_exposed_schemas(list(TOOL_SCHEMAS), ())
        assert original["function"]["description"] == before


class TestMatchDeferredSchemas:
    def test_nomes_exatos_e_desconhecidos(self):
        outcome = match_deferred_schemas(
            list(TOOL_SCHEMAS), names=["tasks_delegate", "read_file", "nao_existe"]
        )
        assert outcome["enabled"] == ["tasks_delegate"]
        assert outcome["already_available"] == ["read_file"]
        assert outcome["unknown"] == ["nao_existe"]
        assert {s["function"]["name"] for s in outcome["matched"]} == {
            "tasks_delegate",
            "read_file",
        }

    def test_query_rankeia_por_nome_e_respeita_limite(self):
        outcome = match_deferred_schemas(
            list(TOOL_SCHEMAS), query="git commit push", max_results=3
        )
        assert len(outcome["enabled"]) == 3
        assert all(name.startswith("git_") for name in outcome["enabled"])

    def test_query_sem_correspondencia(self):
        outcome = match_deferred_schemas(list(TOOL_SCHEMAS), query="zzzzxyz")
        assert outcome["matched"] == []
        assert outcome["enabled"] == []


class TestModeHelpers:
    def test_config_ausente_ou_invalida_degrada_para_off(self):
        assert normalized_tool_deferral_mode(None) == "off"
        assert normalized_tool_deferral_mode(SimpleNamespace(tool_deferral="banana")) == "off"

    def test_api_ativo_em_api_e_all(self):
        assert is_api_tool_deferral_enabled(SimpleNamespace(tool_deferral="api"))
        assert is_api_tool_deferral_enabled(SimpleNamespace(tool_deferral="all"))
        assert not is_api_tool_deferral_enabled(SimpleNamespace(tool_deferral="off"))

    def test_mcp_exige_all_e_honra_override_do_prelude(self):
        config_api = SimpleNamespace(tool_deferral="api")
        config_all = SimpleNamespace(tool_deferral="all")

        assert not is_mcp_tool_deferral_enabled(config_api, None)
        assert is_mcp_tool_deferral_enabled(config_all, None)
        assert is_mcp_tool_deferral_enabled(
            config_api, {DEFERRAL_OVERRIDE_STATE_KEY: "on"}
        )
        assert not is_mcp_tool_deferral_enabled(
            config_all, {DEFERRAL_OVERRIDE_STATE_KEY: "off"}
        )


# ---------------------------------------------------------------------------
# Handler tool_search via ToolExecutor real
# ---------------------------------------------------------------------------

class TestToolSearchHandler:
    def test_habilita_por_nome_e_retorna_schema_completo(self, tmp_path):
        executor = _make_real_executor(tmp_path)
        result = executor.execute(
            ToolCall(name=TOOL_SEARCH_TOOL_NAME, arguments={"names": ["host_memory"]})
        )

        assert result.ok
        assert result.data["enabled_tools"] == ["host_memory"]
        payload = json.loads(result.content)
        assert payload["tools"][0]["name"] == "host_memory"
        assert payload["tools"][0]["parameters"]["type"] == "object"
        assert "habilitadas" in payload["note"]

    def test_query_descobre_tools_diferidas(self, tmp_path):
        executor = _make_real_executor(tmp_path)
        result = executor.execute(
            ToolCall(
                name=TOOL_SEARCH_TOOL_NAME,
                arguments={"query": "memoria do host", "max_results": 2},
            )
        )

        assert result.ok
        assert result.data["enabled_tools"]

    def test_sem_names_e_sem_query_falha_validacao(self, tmp_path):
        executor = _make_real_executor(tmp_path)
        result = executor.execute(ToolCall(name=TOOL_SEARCH_TOOL_NAME, arguments={}))

        assert not result.ok
        assert "exige 'names' e/ou 'query'" in str(result.error)

    def test_max_results_fora_do_intervalo_falha(self, tmp_path):
        executor = _make_real_executor(tmp_path)
        result = executor.execute(
            ToolCall(
                name=TOOL_SEARCH_TOOL_NAME,
                arguments={"query": "git", "max_results": 50},
            )
        )

        assert not result.ok
        assert "max_results" in str(result.error)

    def test_acumula_habilitacoes_no_estado_mcp(self, tmp_path):
        executor = _make_real_executor(tmp_path)
        state: dict = {}

        executor.execute(
            ToolCall(
                name=TOOL_SEARCH_TOOL_NAME,
                arguments={"names": ["git_status"]},
                metadata={"_mcp_state": state},
            )
        )
        executor.execute(
            ToolCall(
                name=TOOL_SEARCH_TOOL_NAME,
                arguments={"names": ["host_memory"]},
                metadata={"_mcp_state": state},
            )
        )

        assert state[DEFERRAL_STATE_KEY] == frozenset({"git_status", "host_memory"})


# ---------------------------------------------------------------------------
# Loop do driver (ToolCallingDriver)
# ---------------------------------------------------------------------------

class _ScriptedDriver(ToolCallingDriver):
    """Driver com transporte roteirizado que captura o array de tools por hop."""

    def __init__(self, script):
        super().__init__(model="fake", base_url="http://localhost:1/v1")
        self._script = list(script)
        self.seen_tools: list[list[str]] = []

    def _chat_streaming(self, messages, *, tools=None, cancel_event=None, on_text_chunk=None):
        self.seen_tools.append(_schema_names(tools or []))
        return self._script.pop(0)


def _prompt():
    from quimera.prompt_templates import PromptText

    return PromptText('<current_turn title="Pedido atual">oi</current_turn>', "chat")


class TestDriverDeferral:
    def test_hop_seguinte_expande_tools_habilitadas(self, tmp_path):
        executor = _make_real_executor(tmp_path)
        assert executor.config.tool_deferral == "api"
        driver = _ScriptedDriver([
            (
                "",
                [{
                    "id": "call_1",
                    "name": TOOL_SEARCH_TOOL_NAME,
                    "arguments": {"names": ["host_memory"]},
                    "raw_arguments": "{}",
                    "argument_error": None,
                }],
            ),
            ("pronto", []),
        ])

        answer = driver.run(_prompt(), tool_executor=executor)

        assert answer == "pronto"
        first_hop, second_hop = driver.seen_tools
        # Capabilities podem remover essenciais (ex.: ask_user sem wiring);
        # o invariante é: nada além de essenciais + tool_search no hop 0.
        assert TOOL_SEARCH_TOOL_NAME in first_hop
        assert "host_memory" not in first_hop
        assert set(first_hop) - {TOOL_SEARCH_TOOL_NAME} <= ESSENTIAL_TOOLS
        assert "host_memory" in second_hop
        assert TOOL_SEARCH_TOOL_NAME in second_hop

    def test_deferral_off_envia_catalogo_completo(self, tmp_path):
        executor = _make_real_executor(tmp_path)
        executor.config.tool_deferral = "off"
        driver = _ScriptedDriver([("pronto", [])])

        driver.run(_prompt(), tool_executor=executor)

        (hop_tools,) = driver.seen_tools
        assert "host_memory" in hop_tools
        assert len(hop_tools) > len(ESSENTIAL_TOOLS) + 1


# ---------------------------------------------------------------------------
# Servidor MCP
# ---------------------------------------------------------------------------

def _exchange(server, *msgs, state=None):
    lines = "\n".join(json.dumps(m) for m in msgs) + "\n"
    inp = io.StringIO(lines)
    out = io.StringIO()
    if state is not None:
        setattr(out, "_mcp_state", state)
    server.serve(stdin=inp, stdout=out)
    responses = []
    for line in out.getvalue().splitlines():
        line = line.strip()
        if line:
            responses.append(json.loads(line))
    return responses


class TestMcpServerDeferral:
    def test_tools_list_filtra_no_modo_all(self, tmp_path):
        executor = _make_real_executor(tmp_path)
        executor.config.tool_deferral = "all"
        server = make_mcp_server(executor)

        responses = _exchange(
            server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        tools = responses[0]["result"]["tools"]
        names = {tool["name"] for tool in tools}

        assert TOOL_SEARCH_TOOL_NAME in names
        assert "tasks_delegate" not in names
        assert names - {TOOL_SEARCH_TOOL_NAME} <= ESSENTIAL_TOOLS

    def test_tools_list_modo_api_nao_afeta_mcp(self, tmp_path):
        executor = _make_real_executor(tmp_path)
        assert executor.config.tool_deferral == "api"
        server = make_mcp_server(executor)

        responses = _exchange(
            server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        names = {tool["name"] for tool in responses[0]["result"]["tools"]}

        assert "host_memory" in names

    def test_override_do_prelude_ativa_por_conexao(self, tmp_path):
        executor = _make_real_executor(tmp_path)
        server = make_mcp_server(executor)

        responses = _exchange(
            server,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            state={DEFERRAL_OVERRIDE_STATE_KEY: "on"},
        )
        names = {tool["name"] for tool in responses[0]["result"]["tools"]}

        assert TOOL_SEARCH_TOOL_NAME in names
        assert "host_memory" not in names

    def test_tool_search_emite_list_changed_e_expande_lista(self, tmp_path):
        executor = _make_real_executor(tmp_path)
        executor.config.tool_deferral = "all"
        server = make_mcp_server(executor)
        state: dict = {}

        responses = _exchange(
            server,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": TOOL_SEARCH_TOOL_NAME,
                    "arguments": {"names": ["host_memory"]},
                },
            },
            state=state,
        )

        methods = [r.get("method") for r in responses if "method" in r]
        assert "notifications/tools/list_changed" in methods

        call_response = next(r for r in responses if r.get("id") == 1)
        assert call_response["result"]["isError"] is False

        # O tools/list do refetch (pós-notificação) reflete a habilitação
        # acumulada no estado da conexão.
        assert state[DEFERRAL_STATE_KEY] == frozenset({"host_memory"})
        list_response = server._handle_tools_list(2, {}, state=state)
        names = {tool["name"] for tool in list_response["result"]["tools"]}
        assert "host_memory" in names

    def test_tools_call_aceita_tool_diferida_sem_habilitar(self, tmp_path):
        """Deferral é apresentação: a tool diferida continua executável por nome."""
        executor = _make_real_executor(tmp_path)
        executor.config.tool_deferral = "all"
        server = make_mcp_server(executor)

        responses = _exchange(
            server,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "host_memory", "arguments": {}},
            },
        )
        call_response = next(r for r in responses if r.get("id") == 1)

        assert "error" not in call_response
        assert call_response["result"]["isError"] is False

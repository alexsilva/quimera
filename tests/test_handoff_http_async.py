"""Testes para HandoffTools._call_agent_http_async e list_agents.

Cobre os dois caminhos de execução remota de agentes via MCP HTTP:
  - SSE path: execução inline na thread pool, resultado real enviado via SSE
  - Non-SSE path (Streamable HTTP): execução em background thread com
    job_id/task_id retornado imediatamente para polling posterior

E também a tool list_agents que retorna os agentes ativos na sessão.

Execute com:
  pytest tests/test_handoff_http_async.py -v
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall, ToolResult
from quimera.runtime.tools.handoff import HandoffTools
from quimera.runtime.approval_broker import TrustedToolExecutionContext


@pytest.fixture
def handoff_tools(tmp_path):
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    return HandoffTools(config)


@pytest.fixture
def dispatch_fn():
    return MagicMock(return_value="resultado do agente")


def _make_call(
    metadata: dict | None = None,
    args: dict | None = None,
) -> ToolCall:
    return ToolCall(
        name="call_agent",
        arguments=args or {"agent_name": "codex", "task": "faz algo"},
        metadata=metadata or {},
    )


STEPS = [
    {"agent_name": "codex", "task": "faz algo", "context": "", "fallback_agents": []},
]


class TestGetTransport:
    """Testes para HandoffTools._get_transport().

    Verifica a detecção do transporte a partir do TrustedToolExecutionContext
    presente no metadata da ToolCall. O transporte determina se o agente será
    executado inline (SSE) ou em background thread (Streamable HTTP).
    """

    def test_detecta_http_mcp_via_trusted_context(self):
        """Objeto TrustedToolExecutionContext com transport=http_mcp."""
        call = _make_call(metadata={
            "trusted_context": TrustedToolExecutionContext(
                transport="http_mcp", run_id="r1", agent_name="claude",
            ),
        })
        assert HandoffTools._get_transport(call) == "http_mcp"

    def test_detecta_http_mcp_via_dict(self):
        """Metadata contém dict com transport=http_mcp (serializado)."""
        call = _make_call(metadata={
            "trusted_context": {"transport": "http_mcp"},
        })
        assert HandoffTools._get_transport(call) == "http_mcp"

    def test_sem_trusted_context_retorna_native(self):
        """Metadata sem trusted_context → fallback para native_tool_call."""
        call = _make_call()
        assert HandoffTools._get_transport(call) == "native_tool_call"

    def test_trusted_context_vazio_retorna_native(self):
        """trusted_context vazio → fallback para native_tool_call."""
        call = _make_call(metadata={"trusted_context": {}})
        assert HandoffTools._get_transport(call) == "native_tool_call"


# ── SSE path ────────────────────────────────────────────────

class TestSSEPath:
    """SSE path: call_agent executa inline na thread pool do MCP.

    Quando _mcp_state["sse_queue"] está presente, o agente executa
    sincronamente dentro da thread pool do _handle_tools_call, e o
    resultado real é entregue ao cliente via SSE event: message.

    Não há polling — o cliente SSE recebe o resultado assincronamente.
    """

    def test_sse_path_executa_inline_e_retorna_resultado(self, handoff_tools, dispatch_fn):
        """Com sse_queue presente, executa inline e retorna o resultado real do agente."""
        handoff_tools.set_call_agent_fn(dispatch_fn)
        call = _make_call(metadata={
            "_mcp_state": {"sse_queue": MagicMock()},
        })
        result = handoff_tools._call_agent_http_async(call, STEPS)
        assert result.ok is True
        assert result.content == "resultado do agente"
        dispatch_fn.assert_called_once()

    def test_sse_path_passa_call_agent_fn_correta(self, handoff_tools, dispatch_fn):
        """call_agent_fn é invocada com a função de dispatch registrada."""
        handoff_tools.set_call_agent_fn(dispatch_fn)
        call = _make_call(metadata={
            "_mcp_state": {"sse_queue": MagicMock()},
        })
        result = handoff_tools._call_agent_http_async(call, STEPS)
        assert result.ok is True

    def test_sse_path_propaga_erro_do_agent(self, handoff_tools):
        """Exceção no dispatch é capturada e retornada como ToolResult com ok=False."""
        def failing_fn(*_a, **_kw):
            raise RuntimeError("falha no dispatch")

        handoff_tools.set_call_agent_fn(failing_fn)
        call = _make_call(metadata={
            "_mcp_state": {"sse_queue": MagicMock()},
        })
        result = handoff_tools._call_agent_http_async(call, STEPS)
        assert result.ok is False
        assert "falha no dispatch" in (result.error or "")

    def test_sse_path_com_chamada_sem_sse_queue(self, handoff_tools, dispatch_fn, tmp_path):
        """sse_queue=None + db_path configurado → cai no non-SSE path (background thread)."""
        handoff_tools.config.db_path = tmp_path / "tasks.db"
        handoff_tools.set_call_agent_fn(dispatch_fn)
        call = _make_call(metadata={
            "_mcp_state": {"sse_queue": None},
        })
        result = handoff_tools._call_agent_http_async(call, STEPS)
        assert result.ok is True

    def test_sse_path_com_metadata_vazio(self, handoff_tools, dispatch_fn, tmp_path):
        """Metadata sem _mcp_state + db_path configurado → cai no non-SSE path."""
        handoff_tools.config.db_path = tmp_path / "tasks.db"
        handoff_tools.set_call_agent_fn(dispatch_fn)
        call = _make_call()
        result = handoff_tools._call_agent_http_async(call, STEPS)
        assert result.ok is True

    def test_sse_path_com_steps_multiplos(self, handoff_tools):
        """Múltiplos steps são executados em sequência e os resultados agregados."""
        results = iter(["r1", "r2"])

        def multi_fn(agent_name, **_kw):
            return next(results)

        handoff_tools.set_call_agent_fn(multi_fn)
        call = _make_call(metadata={
            "_mcp_state": {"sse_queue": MagicMock()},
        })
        steps = [
            {"agent_name": "codex", "task": "t1", "context": "", "fallback_agents": []},
            {"agent_name": "claude", "task": "t2", "context": "", "fallback_agents": []},
        ]
        result = handoff_tools._call_agent_http_async(call, steps)
        assert result.ok is True
        assert "[codex] r1" in result.content
        assert "[claude] r2" in result.content


# ── Non-SSE path (Streamable HTTP) ─────────────────────────

class TestNonSSEPath:
    """Non-SSE path: call_agent executa em background thread.

    Quando sse_queue não está disponível, a chamada cria um job/task
    no SQLite, retorna {job_id, task_id, status} imediatamente e o
    agente executa em uma threading.Thread daemon. O cliente faz
    polling via get_job/list_tasks para obter o resultado.
    """

    @patch("quimera.runtime.tools.handoff.get_job", return_value={"status": "active", "started_at": "2026-06-11 20:23:11"})
    @patch("quimera.runtime.tools.handoff.update_job_status")
    @patch("quimera.runtime.tools.handoff.add_job", return_value=42)
    @patch("quimera.runtime.tools.handoff.create_task", return_value=99)
    def test_non_sse_path_retorna_job_id_task_id(
        self, mock_create, mock_add, mock_update_job, mock_get_job, handoff_tools, dispatch_fn, tmp_path,
    ):
        """Retorna status/timestamp inicial sem depender de polling posterior."""
        handoff_tools.config.db_path = tmp_path / "tasks.db"
        handoff_tools.set_call_agent_fn(dispatch_fn)
        call = _make_call(metadata={
            "_mcp_state": {"sse_queue": None},
        })
        result = handoff_tools._call_agent_http_async(call, STEPS)
        assert result.ok is True
        data = json.loads(result.content)
        assert data["job_id"] == 42
        assert data["task_id"] == 99
        assert data["status"] == "in_progress"
        assert data["job_status"] == "active"
        assert data["task_status"] == "in_progress"
        assert data["started_at"] == "2026-06-11 20:23:11"
        mock_add.assert_called_once()
        mock_create.assert_called_once()
        mock_update_job.assert_any_call(42, "active", db_path=str(tmp_path / "tasks.db"))
        mock_get_job.assert_called_once_with(42, db_path=str(tmp_path / "tasks.db"))

    def test_non_sse_path_sem_db_path_retorna_erro(self, handoff_tools, dispatch_fn):
        """Sem db_path configurado → erro: 'db_path not configured'."""
        handoff_tools.config.db_path = None
        handoff_tools.set_call_agent_fn(dispatch_fn)
        call = _make_call()
        result = handoff_tools._call_agent_http_async(call, STEPS)
        assert result.ok is False
        assert "db_path not configured" in (result.error or "")

    @patch("quimera.runtime.tools.handoff.add_job", side_effect=ValueError("db locked"))
    def test_non_sse_path_add_job_falha_retorna_erro(
        self, mock_add, handoff_tools, dispatch_fn, tmp_path,
    ):
        """add_job lança exceção → 'Failed to create job'."""
        handoff_tools.config.db_path = tmp_path / "tasks.db"
        handoff_tools.set_call_agent_fn(dispatch_fn)
        call = _make_call()
        result = handoff_tools._call_agent_http_async(call, STEPS)
        assert result.ok is False
        assert "Failed to create job" in (result.error or "")

    @patch("quimera.runtime.tools.handoff.add_job", return_value=42)
    @patch("quimera.runtime.tools.handoff.create_task", side_effect=RuntimeError("db error"))
    def test_non_sse_path_create_task_falha_retorna_erro(
        self, mock_create, mock_add, handoff_tools, dispatch_fn, tmp_path,
    ):
        """create_task lança exceção → 'Failed to create task'."""
        handoff_tools.config.db_path = tmp_path / "tasks.db"
        handoff_tools.set_call_agent_fn(dispatch_fn)
        call = _make_call()
        result = handoff_tools._call_agent_http_async(call, STEPS)
        assert result.ok is False
        assert "Failed to create task" in (result.error or "")

    @patch("quimera.runtime.tools.handoff.add_job", return_value=42)
    @patch("quimera.runtime.tools.handoff.create_task", return_value=99)
    def test_non_sse_path_step_one_info_usado_no_job_desc(
        self, mock_create, mock_add, handoff_tools, dispatch_fn, tmp_path,
    ):
        """A descrição do job contém agent_name e task do primeiro step."""
        handoff_tools.config.db_path = tmp_path / "tasks.db"
        handoff_tools.set_call_agent_fn(dispatch_fn)
        call = _make_call()
        handoff_tools._call_agent_http_async(call, STEPS)
        desc = mock_add.call_args[0][0] if mock_add.call_args else ""
        assert "codex" in desc
        assert "faz algo" in desc

    def test_non_sse_background_thread_completa_task(self, handoff_tools, tmp_path):
        """Background thread completa com sucesso: complete_task e update_job_status chamados."""
        db_path = tmp_path / "tasks.db"
        from quimera.runtime import tasks as task_mod
        import time
        task_mod.init_db(str(db_path))
        job_id = task_mod.add_job("test job", db_path=str(db_path))
        task_id = task_mod.create_task(
            job_id, "task test", status="in_progress",
            assigned_to="codex", origin="mcp_http_call_agent",
            db_path=str(db_path),
        )

        handoff_tools.config.db_path = db_path
        dispatch = MagicMock(return_value="sucesso")
        handoff_tools.set_call_agent_fn(dispatch)
        call = _make_call(metadata={
            "_mcp_state": {"sse_queue": None},
        })

        with (
            patch("quimera.runtime.tools.handoff.add_job", return_value=job_id),
            patch("quimera.runtime.tools.handoff.create_task", return_value=task_id),
            patch("quimera.runtime.tools.handoff.complete_task") as mock_complete,
            patch("quimera.runtime.tools.handoff.fail_task") as mock_fail,
            patch("quimera.runtime.tools.handoff.update_job_status") as mock_update_job,
        ):
            result = handoff_tools._call_agent_http_async(call, STEPS)
            assert result.ok is True
            time.sleep(0.3)
            mock_complete.assert_called_once()
            mock_fail.assert_not_called()
            assert mock_update_job.call_args_list[0].args == (job_id, "active")
            assert mock_update_job.call_args_list[0].kwargs == {"db_path": str(db_path)}
            assert mock_update_job.call_args_list[1].args == (job_id, "completed")
            assert mock_update_job.call_args_list[1].kwargs == {"db_path": str(db_path)}

    def test_non_sse_background_thread_falha_task(self, handoff_tools, tmp_path):
        """Dispatch retorna None (falha silenciosa) → fail_task e update_job_status(failed) chamados."""
        db_path = tmp_path / "tasks.db"
        from quimera.runtime import tasks as task_mod
        import time
        task_mod.init_db(str(db_path))
        job_id = task_mod.add_job("test job", db_path=str(db_path))
        task_id = task_mod.create_task(
            job_id, "task test", status="in_progress",
            assigned_to="codex", origin="mcp_http_call_agent",
            db_path=str(db_path),
        )

        handoff_tools.config.db_path = db_path
        dispatch = MagicMock(return_value=None)
        handoff_tools.set_call_agent_fn(dispatch)
        call = _make_call(metadata={
            "_mcp_state": {"sse_queue": None},
        })

        with (
            patch("quimera.runtime.tools.handoff.add_job", return_value=job_id),
            patch("quimera.runtime.tools.handoff.create_task", return_value=task_id),
            patch("quimera.runtime.tools.handoff.complete_task") as mock_complete,
            patch("quimera.runtime.tools.handoff.fail_task") as mock_fail,
            patch("quimera.runtime.tools.handoff.update_job_status") as mock_update_job,
        ):
            result = handoff_tools._call_agent_http_async(call, STEPS)
            assert result.ok is True
            time.sleep(0.3)
            mock_complete.assert_not_called()
            mock_fail.assert_called_once()
            assert mock_update_job.call_args_list[0].args == (job_id, "active")
            assert mock_update_job.call_args_list[0].kwargs == {"db_path": str(db_path)}
            assert mock_update_job.call_args_list[1].args == (job_id, "failed")
            assert mock_update_job.call_args_list[1].kwargs == {"db_path": str(db_path)}

    def test_non_sse_background_thread_exception_nao_propaga(self, handoff_tools, tmp_path):
        """Exceção no complete_task (pós-agente) não quebra o retorno inicial."""
        import time
        db_path = tmp_path / "tasks.db"
        handoff_tools.config.db_path = db_path
        dispatch = MagicMock(return_value="ok")
        handoff_tools.set_call_agent_fn(dispatch)
        call = _make_call(metadata={
            "_mcp_state": {"sse_queue": None},
        })

        with (
            patch("quimera.runtime.tools.handoff.add_job", return_value=1),
            patch("quimera.runtime.tools.handoff.create_task", return_value=1),
            patch("quimera.runtime.tools.handoff.complete_task",
                  side_effect=RuntimeError("db failure after call")),
            patch("quimera.runtime.tools.handoff.update_job_status"),
        ):
            result = handoff_tools._call_agent_http_async(call, STEPS)
            assert result.ok is True
            time.sleep(0.3)


# ── get_db_path ────────────────────────────────────────────

class TestGetDbPath:
    """HandoffTools._get_db_path(): converte Path opcional para str."""

    def test_db_path_configurado_retorna_string(self, handoff_tools, tmp_path):
        """Path configurado → retorna str do path."""
        handoff_tools.config.db_path = tmp_path / "tasks.db"
        assert handoff_tools._get_db_path() == str(tmp_path / "tasks.db")

    def test_db_path_none_retorna_none(self, handoff_tools):
        """db_path = None → retorna None."""
        handoff_tools.config.db_path = None
        assert handoff_tools._get_db_path() is None


# ── list_agents ─────────────────────────────────────────────

class TestListAgents:
    """Tool list_agents: retorna agentes ativos na sessão como JSON array.

    Usa o provider registrado via set_active_agents_provider().
    Sem provider, retorna lista vazia (não falha).
    """

    def test_list_agents_sem_provider_retorna_lista_vazia(self, handoff_tools):
        """Nenhum provider registrado → []."""
        call = _make_call(args={})
        result = handoff_tools.list_agents(call)
        assert result.ok is True
        assert result.content == "[]"

    def test_list_agents_retorna_agentes_ordenados(self, handoff_tools):
        """Agentes retornados em ordem alfabética."""
        handoff_tools.set_active_agents_provider(
            lambda: ["claude", "opencode-big-pickle", "codex"],
        )
        call = _make_call(args={})
        result = handoff_tools.list_agents(call)
        assert result.ok is True
        agents = json.loads(result.content)
        assert agents == ["claude", "codex", "opencode-big-pickle"]

    def test_list_agents_ignora_agentes_vazios(self, handoff_tools):
        """Strings vazias e None são filtrados da lista."""
        handoff_tools.set_active_agents_provider(
            lambda: ["codex", "", None, "claude"],
        )
        call = _make_call(args={})
        result = handoff_tools.list_agents(call)
        agents = json.loads(result.content)
        assert agents == ["claude", "codex"]

    def test_list_agents_provider_falha_retorna_vazio(self, handoff_tools):
        """Provider lança exceção → retorna [] (graceful degradation)."""
        def failing_provider():
            raise RuntimeError("pool unavailable")

        handoff_tools.set_active_agents_provider(failing_provider)
        call = _make_call(args={})
        result = handoff_tools.list_agents(call)
        assert result.ok is True
        assert result.content == "[]"


# ── cleanup_callback ────────────────────────────────────────

class TestCleanupCallback:
    """Testes para o callback de limpeza (_cleanup_sub_agent_stream).

    O cleanup_callback é invocado após cada execução de step (sucesso ou
    falha total de fallbacks) para remover o estado transitório de render
    do agente chamado (_stream_states, _rolling_buffers, transient agents).
    """

    def test_cleanup_chamado_no_sucesso(self):
        """Step bem-sucedido → cleanup_callback chamado com o nome do agente."""
        cleanup = MagicMock()
        result = HandoffTools._execute_steps_inner(
            STEPS,
            MagicMock(return_value="ok"),
            progress_callback=None,
            resolve_active_agents_fn=lambda: {"codex"},
            normalize_agent_fn=lambda s: s.lower().lstrip("/"),
            cleanup_callback=cleanup,
        )
        assert result.ok is True
        cleanup.assert_called_once_with("codex")

    def test_cleanup_chamado_apos_fallback_bem_sucedido(self):
        """Agente principal falha, fallback responde → cleanup para o fallback."""
        cleanup = MagicMock()
        call_count = [0]

        def dispatch(agent, **_kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # codex falha
            return "resposta do fallback"  # claude responde

        steps = [
            {"agent_name": "codex", "task": "t1", "context": "", "fallback_agents": ["claude"]},
        ]
        result = HandoffTools._execute_steps_inner(
            steps,
            dispatch,
            progress_callback=None,
            resolve_active_agents_fn=lambda: {"codex", "claude"},
            normalize_agent_fn=lambda s: s.lower().lstrip("/"),
            cleanup_callback=cleanup,
        )
        assert result.ok is True
        assert result.content == "resposta do fallback"
        cleanup.assert_called_once_with("claude")

    def test_cleanup_chamado_quando_todos_fallbacks_retornam_none(self):
        """Todos os targets retornam None → cleanup_callback chamado para o último."""
        cleanup = MagicMock()

        def dispatch_all_none(agent, **_kw):
            return None

        result = HandoffTools._execute_steps_inner(
            STEPS,
            dispatch_all_none,
            progress_callback=None,
            resolve_active_agents_fn=lambda: {"codex"},
            normalize_agent_fn=lambda s: s.lower().lstrip("/"),
            cleanup_callback=cleanup,
        )
        assert result.ok is False
        cleanup.assert_called_once_with("codex")

    def test_cleanup_chamado_quando_todos_fallbacks_lancam_excecao(self):
        """Todos os targets lançam exceção → cleanup_callback chamado para o último."""
        cleanup = MagicMock()

        def dispatch_raise(agent, **_kw):
            raise RuntimeError(f"falha: {agent}")

        result = HandoffTools._execute_steps_inner(
            STEPS,
            dispatch_raise,
            progress_callback=None,
            resolve_active_agents_fn=lambda: {"codex"},
            normalize_agent_fn=lambda s: s.lower().lstrip("/"),
            cleanup_callback=cleanup,
        )
        assert result.ok is False
        cleanup.assert_called_once_with("codex")

    def test_cleanup_chamado_quando_todos_fallbacks_com_fallback_total(self):
        """Fallback chain inteira falha (None + exceção) → cleanup para o último."""
        cleanup = MagicMock()
        call_count = [0]

        def dispatch_mixed(agent, **_kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # codex: None
            raise RuntimeError(f"falha: {agent}")  # claude: exception

        steps = [
            {"agent_name": "codex", "task": "t1", "context": "", "fallback_agents": ["claude"]},
        ]
        result = HandoffTools._execute_steps_inner(
            steps,
            dispatch_mixed,
            progress_callback=None,
            resolve_active_agents_fn=lambda: {"codex", "claude"},
            normalize_agent_fn=lambda s: s.lower().lstrip("/"),
            cleanup_callback=cleanup,
        )
        assert result.ok is False
        cleanup.assert_called_once_with("claude")

    def test_cleanup_nao_chamado_sem_callback(self):
        """Nenhum cleanup_callback registrado → não levanta exceção."""
        result = HandoffTools._execute_steps_inner(
            STEPS,
            MagicMock(return_value="ok"),
            progress_callback=None,
            resolve_active_agents_fn=lambda: {"codex"},
            normalize_agent_fn=lambda s: s.lower().lstrip("/"),
            cleanup_callback=None,
        )
        assert result.ok is True

    def test_cleanup_falha_nao_quebra_fluxo(self):
        """cleanup_callback lança exceção → não interrompe o fluxo de sucesso."""
        def failing_cleanup(agent):
            raise RuntimeError("limpeza falhou")

        result = HandoffTools._execute_steps_inner(
            STEPS,
            MagicMock(return_value="ok"),
            progress_callback=None,
            resolve_active_agents_fn=lambda: {"codex"},
            normalize_agent_fn=lambda s: s.lower().lstrip("/"),
            cleanup_callback=failing_cleanup,
        )
        assert result.ok is True
        assert result.content == "ok"

    def test_cleanup_chamado_em_cada_step_multi_step(self):
        """Múltiplos steps → cleanup_callback chamado após cada step bem-sucedido."""
        cleanup = MagicMock()
        results = iter(["r1", "r2"])

        def multi_fn(agent, **_kw):
            return next(results)

        steps = [
            {"agent_name": "codex", "task": "t1", "context": "", "fallback_agents": []},
            {"agent_name": "claude", "task": "t2", "context": "", "fallback_agents": []},
        ]
        result = HandoffTools._execute_steps_inner(
            steps,
            multi_fn,
            progress_callback=None,
            resolve_active_agents_fn=lambda: {"codex", "claude"},
            normalize_agent_fn=lambda s: s.lower().lstrip("/"),
            cleanup_callback=cleanup,
        )
        assert result.ok is True
        assert cleanup.call_count == 2
        cleanup.assert_any_call("codex")
        cleanup.assert_any_call("claude")

    def test_cleanup_chamado_no_step_que_falha_dentro_de_multi_step(self):
        """Step 1 falha totalmente → cleanup para o último tentado antes de retornar erro."""
        cleanup = MagicMock()

        def dispatch_all_none(agent, **_kw):
            return None

        steps = [
            {"agent_name": "codex", "task": "t1", "context": "", "fallback_agents": ["claude"]},
            {"agent_name": "gemini", "task": "t2", "context": "", "fallback_agents": []},
        ]
        result = HandoffTools._execute_steps_inner(
            steps,
            dispatch_all_none,
            progress_callback=None,
            resolve_active_agents_fn=lambda: {"codex", "claude", "gemini"},
            normalize_agent_fn=lambda s: s.lower().lstrip("/"),
            cleanup_callback=cleanup,
        )
        assert result.ok is False
        # O step 1 tentou codex (None) → claude (None) → cleanup(claude)
        cleanup.assert_called_once_with("claude")


class TestGetCallingAgent:
    """Testes para HandoffTools._get_calling_agent().

    Verifica a extração do nome do agente chamador a partir do metadata
    de uma ToolCall, cobrindo os dois caminhos suportados: via campo
    direto 'calling_agent' (preenchido pelo openai_compat) e via
    TrustedToolExecutionContext (MCP).
    """

    def test_retorna_calling_agent_do_metadata_direto(self):
        """Campo 'calling_agent' no metadata é retornado normalizado em lowercase."""
        call = _make_call(metadata={"calling_agent": "Codex"})
        assert HandoffTools._get_calling_agent(call) == "codex"

    def test_retorna_calling_agent_com_espacos_trimados(self):
        """Espaços em torno do nome são removidos."""
        call = _make_call(metadata={"calling_agent": "  deepseek  "})
        assert HandoffTools._get_calling_agent(call) == "deepseek"

    def test_retorna_none_sem_metadata(self):
        """Sem metadata retorna None."""
        call = _make_call(metadata={})
        assert HandoffTools._get_calling_agent(call) is None

    def test_retorna_calling_agent_via_trusted_context_objeto(self):
        """Lê agent_name de TrustedToolExecutionContext quando não há campo direto."""
        ctx = TrustedToolExecutionContext(
            agent_name="claude",
            transport="native_tool_call",
            session_id="s1",
        )
        call = _make_call(metadata={"trusted_context": ctx})
        assert HandoffTools._get_calling_agent(call) == "claude"

    def test_retorna_calling_agent_via_trusted_context_dict(self):
        """Lê agent_name de dict trusted_context quando não há campo direto."""
        call = _make_call(metadata={"trusted_context": {"agent_name": "Gemini", "transport": "http_mcp"}})
        assert HandoffTools._get_calling_agent(call) == "gemini"

    def test_campo_direto_tem_prioridade_sobre_trusted_context(self):
        """'calling_agent' direto tem precedência sobre trusted_context."""
        ctx = TrustedToolExecutionContext(
            agent_name="claude",
            transport="native_tool_call",
            session_id="s1",
        )
        call = _make_call(metadata={"calling_agent": "codex", "trusted_context": ctx})
        assert HandoffTools._get_calling_agent(call) == "codex"

    def test_calling_agent_vazio_ignorado_retorna_via_trusted_context(self):
        """String vazia em 'calling_agent' não é considerada — cai para trusted_context."""
        call = _make_call(metadata={"calling_agent": "", "trusted_context": {"agent_name": "gemini"}})
        assert HandoffTools._get_calling_agent(call) == "gemini"

    def test_retorna_none_quando_trusted_context_dict_sem_agent_name(self):
        """trusted_context como dict sem 'agent_name' retorna None."""
        call = _make_call(metadata={"trusted_context": {"transport": "http_mcp"}})
        assert HandoffTools._get_calling_agent(call) is None


class TestCallAgentAutoReferencia:
    """Testes para o bloqueio de auto-referência em call_agent().

    Garante que um agente não pode delegar para si mesmo, seja no step
    principal, nos fallback_agents ou nos handoffs.
    """

    def _make_tools(self, tmp_path) -> HandoffTools:
        config = ToolRuntimeConfig(workspace_root=tmp_path)
        tools = HandoffTools(config)
        tools.set_call_agent_fn(MagicMock(return_value="ok"))
        tools.set_active_agents_provider(lambda: ["codex", "claude", "deepseek"])
        return tools

    def test_bloqueia_main_step_auto_referencia(self, tmp_path):
        """Agente não pode delegar o step principal para si mesmo."""
        tools = self._make_tools(tmp_path)
        call = _make_call(
            metadata={"calling_agent": "deepseek"},
            args={"agent_name": "deepseek", "task": "faz algo"},
        )
        result = tools.call_agent(call)
        assert result.ok is False
        assert "cannot delegate to itself" in result.error

    def test_bloqueia_main_step_case_insensitive(self, tmp_path):
        """Comparação de auto-referência é case-insensitive."""
        tools = self._make_tools(tmp_path)
        call = _make_call(
            metadata={"calling_agent": "codex"},
            args={"agent_name": "CODEX", "task": "faz algo"},
        )
        result = tools.call_agent(call)
        assert result.ok is False
        assert "cannot delegate to itself" in result.error

    def test_permite_main_step_agente_diferente(self, tmp_path):
        """Delegação para agente diferente do chamador é permitida."""
        tools = self._make_tools(tmp_path)
        call = _make_call(
            metadata={"calling_agent": "deepseek"},
            args={"agent_name": "codex", "task": "faz algo"},
        )
        result = tools.call_agent(call)
        assert result.ok is True

    def test_sem_calling_agent_nao_bloqueia(self, tmp_path):
        """Sem informação de agente chamador, não há bloqueio."""
        tools = self._make_tools(tmp_path)
        call = _make_call(
            metadata={},
            args={"agent_name": "codex", "task": "faz algo"},
        )
        result = tools.call_agent(call)
        assert result.ok is True

    def test_filtra_self_de_fallback_agents(self, tmp_path):
        """Entradas auto-referentes em fallback_agents são removidas silenciosamente."""
        dispatched: list[str] = []

        def dispatch(agent, **kw):
            dispatched.append(agent)
            return "ok"

        config = ToolRuntimeConfig(workspace_root=tmp_path)
        tools = HandoffTools(config)
        tools.set_call_agent_fn(dispatch)
        tools.set_active_agents_provider(lambda: ["codex", "claude", "deepseek"])

        call = _make_call(
            metadata={"calling_agent": "deepseek"},
            args={
                "agent_name": "codex",
                "task": "faz algo",
                "fallback_agents": ["deepseek", "claude"],
            },
        )
        result = tools.call_agent(call)
        assert result.ok is True
        # deepseek filtrado dos fallbacks; dispatch ocorreu para codex
        assert dispatched == ["codex"]

    def test_bloqueia_handoff_step_auto_referencia(self, tmp_path):
        """Handoff step com agent_name igual ao chamador é bloqueado com erro."""
        tools = self._make_tools(tmp_path)
        call = _make_call(
            metadata={"calling_agent": "deepseek"},
            args={
                "agent_name": "codex",
                "task": "step 1",
                "handoffs": [
                    {"agent_name": "deepseek", "task": "step 2"},
                ],
            },
        )
        result = tools.call_agent(call)
        assert result.ok is False
        assert "cannot delegate to itself" in result.error

    def test_filtra_self_de_handoff_fallback_agents(self, tmp_path):
        """Self em fallback_agents de um handoff step é removido silenciosamente."""
        dispatched: list[str] = []

        def dispatch(agent, **kw):
            dispatched.append(agent)
            return "ok"

        config = ToolRuntimeConfig(workspace_root=tmp_path)
        tools = HandoffTools(config)
        tools.set_call_agent_fn(dispatch)
        tools.set_active_agents_provider(lambda: ["codex", "claude", "deepseek"])

        call = _make_call(
            metadata={"calling_agent": "deepseek"},
            args={
                "agent_name": "codex",
                "task": "step 1",
                "handoffs": [
                    {
                        "agent_name": "claude",
                        "task": "step 2",
                        "fallback_agents": ["deepseek", "codex"],
                    }
                ],
            },
        )
        result = tools.call_agent(call)
        assert result.ok is True
        # step 1 → codex, step 2 → claude (deepseek filtrado dos fallbacks)
        assert dispatched == ["codex", "claude"]

    def test_bloqueia_com_prefixo_slash_no_agent_name(self, tmp_path):
        """Agent_name com prefixo / equivale ao mesmo agente (calling_agent)."""
        tools = self._make_tools(tmp_path)
        call = _make_call(
            metadata={"calling_agent": "deepseek"},
            args={"agent_name": "/deepseek", "task": "faz algo"},
        )
        result = tools.call_agent(call)
        assert result.ok is False
        assert "cannot delegate to itself" in result.error

    def test_bloqueia_via_trusted_context(self, tmp_path):
        """Self-reference funciona com calling_agent vindo de TrustedToolExecutionContext."""
        config = ToolRuntimeConfig(workspace_root=tmp_path)
        tools = HandoffTools(config)
        tools.set_call_agent_fn(MagicMock(return_value="ok"))
        tools.set_active_agents_provider(lambda: ["codex", "deepseek"])
        ctx = TrustedToolExecutionContext(
            agent_name="deepseek",
            transport="native_tool_call",
            session_id="s1",
        )
        call = _make_call(
            metadata={"trusted_context": ctx},
            args={"agent_name": "deepseek", "task": "faz algo"},
        )
        result = tools.call_agent(call)
        assert result.ok is False
        assert "cannot delegate to itself" in result.error

    def test_fallback_agents_todos_self_referencia(self, tmp_path):
        """Todos fallback_agents são auto-referência → removidos sem erro."""
        dispatched: list[str] = []

        def dispatch(agent, **kw):
            dispatched.append(agent)
            return "ok"

        config = ToolRuntimeConfig(workspace_root=tmp_path)
        tools = HandoffTools(config)
        tools.set_call_agent_fn(dispatch)
        tools.set_active_agents_provider(lambda: ["codex", "deepseek"])

        call = _make_call(
            metadata={"calling_agent": "deepseek"},
            args={
                "agent_name": "codex",
                "task": "faz algo",
                "fallback_agents": ["deepseek", "/deepseek", "  deepseek  "],
            },
        )
        result = tools.call_agent(call)
        assert result.ok is True
        assert dispatched == ["codex"]

    def test_handoff_fallback_agents_todos_self_referencia(self, tmp_path):
        """Todos handoff fallback_agents são auto-referência → removidos sem erro."""
        dispatched: list[str] = []

        def dispatch(agent, **kw):
            dispatched.append(agent)
            return "ok"

        config = ToolRuntimeConfig(workspace_root=tmp_path)
        tools = HandoffTools(config)
        tools.set_call_agent_fn(dispatch)
        tools.set_active_agents_provider(lambda: ["codex", "claude", "deepseek"])

        call = _make_call(
            metadata={"calling_agent": "deepseek"},
            args={
                "agent_name": "codex",
                "task": "step 1",
                "handoffs": [
                    {
                        "agent_name": "claude",
                        "task": "step 2",
                        "fallback_agents": ["deepseek", "/deepseek"],
                    }
                ],
            },
        )
        result = tools.call_agent(call)
        assert result.ok is True
        assert dispatched == ["codex", "claude"]

    def test_bloqueia_main_step_com_prefixo_no_calling_agent(self, tmp_path):
        """calling_agent com prefixo / normaliza e bloqueia corretamente."""
        tools = self._make_tools(tmp_path)
        call = _make_call(
            metadata={"calling_agent": "/CODEX"},
            args={"agent_name": "codex", "task": "faz algo"},
        )
        result = tools.call_agent(call)
        assert result.ok is False
        assert "cannot delegate to itself" in result.error

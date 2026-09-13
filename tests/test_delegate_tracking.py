"""Testes para o acompanhamento unificado da tool delegate.

Todos os transportes registram a delegação como job/task no banco:
  - wait=true (padrão): execução bloqueante; task fica in_progress durante a
    execução e completed/failed ao final, com o resultado persistido.
  - wait=false: retorna imediatamente job_id/task_id (mesmo contrato do
    HTTP MCP sem SSE) e o chamador acompanha via list_tasks/get_job.

Sem db_path configurado, o caminho síncrono segue sem registro (best-effort)
e o assíncrono retorna erro explícito.

Execute com:
  pytest tests/test_delegate_tracking.py -v
"""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.delegate import DelegateTools
from quimera.runtime.approval_broker import TrustedToolExecutionContext
from quimera.tasks import api as task_api


def _make_call(args: dict | None = None, metadata: dict | None = None) -> ToolCall:
    return ToolCall(
        name="delegate",
        arguments=args or {"target_agent": "codex", "request": "faz algo"},
        metadata=metadata or {},
    )


@pytest.fixture
def tools_com_db(tmp_path):
    db_path = tmp_path / "tasks.db"
    task_api.init_db(str(db_path))
    config = ToolRuntimeConfig(workspace_root=tmp_path, db_path=db_path)
    tools = DelegateTools(config)
    return tools, str(db_path)


@pytest.fixture
def tools_sem_db(tmp_path):
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    return DelegateTools(config)


def _wait_task_status(db_path: str, task_id: int, status: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = task_api.list_tasks({"id": task_id}, db_path=db_path)
        if rows and rows[0]["status"] == status:
            return rows[0]
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} não atingiu status '{status}' em {timeout}s")


class TestSyncTracking:
    """wait=true (padrão): delegação bloqueante registrada como task."""

    def test_sync_registra_task_e_persiste_resultado(self, tools_com_db):
        tools, db_path = tools_com_db
        tools.set_delegate_fn(MagicMock(return_value="resposta do agente"))

        result = tools.delegate(_make_call())

        assert result.ok is True
        assert result.content.startswith("resposta do agente")
        assert result.data["task_status"] == "completed"
        task_id = result.data["task_id"]
        job_id = result.data["job_id"]
        assert f"task {task_id}" in result.content

        rows = task_api.list_tasks({"id": task_id}, db_path=db_path)
        assert rows and rows[0]["status"] == "completed"
        assert rows[0]["result"] == "resposta do agente"
        assert rows[0]["assigned_to"] == "codex"
        assert rows[0]["origin"] == "delegate"
        job = task_api.get_job(job_id, db_path=db_path)
        assert job["status"] == "completed"

    def test_sync_falha_marca_task_failed(self, tools_com_db):
        tools, db_path = tools_com_db
        tools.set_delegate_fn(MagicMock(return_value=None))

        result = tools.delegate(_make_call())

        assert result.ok is False
        rows = task_api.list_tasks({"id": result.data["task_id"]}, db_path=db_path)
        assert rows and rows[0]["status"] == "failed"
        job = task_api.get_job(result.data["job_id"], db_path=db_path)
        assert job["status"] == "failed"

    def test_sync_sem_db_preserva_comportamento(self, tools_sem_db):
        tools_sem_db.set_delegate_fn(MagicMock(return_value="resposta pura"))

        result = tools_sem_db.delegate(_make_call())

        assert result.ok is True
        assert result.content == "resposta pura"
        assert "task_id" not in result.data

    def test_sync_paralelo_tambem_registra(self, tools_com_db):
        tools, db_path = tools_com_db
        respostas = {"codex": "r1", "claude": "r2"}
        tools.set_delegate_fn(
            MagicMock(side_effect=lambda agent, **_kw: respostas[agent])
        )
        call = _make_call(args={
            "target_agent": "codex",
            "request": "t1",
            "steps": [{"target_agent": "claude", "request": "t2"}],
            "parallel": True,
        })

        result = tools.delegate(call)

        assert result.ok is True
        assert "[codex] r1" in result.content
        assert "[claude] r2" in result.content
        rows = task_api.list_tasks({"id": result.data["task_id"]}, db_path=db_path)
        assert rows and rows[0]["status"] == "completed"
        body = json.loads(rows[0]["body"])
        assert [s["target_agent"] for s in body] == ["codex", "claude"]

    def test_sync_falha_no_banco_nao_impede_delegacao(self, tmp_path):
        # db_path aponta para um diretório (sqlite não consegue abrir): o
        # rastreio falha em best-effort e a delegação síncrona segue normal.
        config = ToolRuntimeConfig(workspace_root=tmp_path, db_path=tmp_path)
        tools = DelegateTools(config)
        tools.set_delegate_fn(MagicMock(return_value="ok mesmo sem tracking"))

        result = tools.delegate(_make_call())

        assert result.ok is True
        assert result.content == "ok mesmo sem tracking"


class TestWaitFalse:
    """wait=false: contrato idêntico ao HTTP MCP sem SSE em qualquer transporte."""

    def test_wait_false_retorna_ids_e_conclui_em_background(self, tools_com_db):
        tools, db_path = tools_com_db
        liberar = threading.Event()

        def dispatch(agent, **_kw):
            liberar.wait(timeout=5)
            return "resultado tardio"

        tools.set_delegate_fn(dispatch)
        call = _make_call(args={
            "target_agent": "codex", "request": "faz algo", "wait": False,
        })

        result = tools.delegate(call)

        assert result.ok is True
        payload = json.loads(result.content)
        assert payload["status"] == "in_progress"
        assert payload["task_status"] == "in_progress"
        assert "list_tasks" in payload["hint"]
        rows = task_api.list_tasks({"id": payload["task_id"]}, db_path=db_path)
        assert rows and rows[0]["status"] == "in_progress"

        liberar.set()
        row = _wait_task_status(db_path, payload["task_id"], "completed")
        assert row["result"] == "resultado tardio"
        job = task_api.get_job(payload["job_id"], db_path=db_path)
        assert job["status"] == "completed"

    def test_wait_false_usa_background_delegate_fn(self, tools_com_db):
        tools, db_path = tools_com_db
        main_fn = MagicMock(return_value="main")
        bg_fn = MagicMock(return_value="background")
        tools.set_delegate_fn(main_fn)
        tools.set_background_delegate_fn(bg_fn)
        call = _make_call(args={
            "target_agent": "codex", "request": "faz algo", "wait": False,
        })

        result = tools.delegate(call)

        assert result.ok is True
        task_id = json.loads(result.content)["task_id"]
        row = _wait_task_status(db_path, task_id, "completed")
        assert row["result"] == "background"
        bg_fn.assert_called_once()
        main_fn.assert_not_called()

    def test_wait_false_paralelo_executa_todos_os_steps(self, tools_com_db):
        tools, db_path = tools_com_db
        respostas = {"codex": "r1", "claude": "r2"}
        tools.set_delegate_fn(
            MagicMock(side_effect=lambda agent, **_kw: respostas[agent])
        )
        call = _make_call(args={
            "target_agent": "codex",
            "request": "t1",
            "steps": [{"target_agent": "claude", "request": "t2"}],
            "parallel": True,
            "wait": False,
        })

        result = tools.delegate(call)

        assert result.ok is True
        task_id = json.loads(result.content)["task_id"]
        row = _wait_task_status(db_path, task_id, "completed")
        assert "[codex] r1" in row["result"]
        assert "[claude] r2" in row["result"]

    def test_wait_false_sem_db_retorna_erro(self, tools_sem_db):
        tools_sem_db.set_delegate_fn(MagicMock(return_value="nunca"))
        call = _make_call(args={
            "target_agent": "codex", "request": "faz algo", "wait": False,
        })

        result = tools_sem_db.delegate(call)

        assert result.ok is False
        assert "db_path not configured" in (result.error or "")

    def test_wait_nao_booleano_retorna_erro(self, tools_sem_db):
        tools_sem_db.set_delegate_fn(MagicMock(return_value="nunca"))
        call = _make_call(args={
            "target_agent": "codex", "request": "faz algo", "wait": "sim",
        })

        result = tools_sem_db.delegate(call)

        assert result.ok is False
        assert "'wait' must be a boolean" in (result.error or "")


class TestParidadeHttp:
    """HTTP MCP segue o mesmo contrato de acompanhamento dos demais transportes."""

    def _http_call(self, args: dict | None = None, sse: bool = False) -> ToolCall:
        metadata = {
            "trusted_context": TrustedToolExecutionContext(
                transport="http_mcp", run_id="r1", agent_name="claude",
            ),
            "_mcp_state": {"sse_queue": MagicMock() if sse else None},
        }
        return _make_call(args=args, metadata=metadata)

    def test_http_sem_sse_tem_mesmo_contrato_de_wait_false(self, tools_com_db):
        tools, db_path = tools_com_db
        tools.set_delegate_fn(MagicMock(return_value="via http"))

        result_http = tools.delegate(self._http_call())
        result_native = tools.delegate(_make_call(args={
            "target_agent": "codex", "request": "faz algo", "wait": False,
        }))

        payload_http = json.loads(result_http.content)
        payload_native = json.loads(result_native.content)
        assert set(payload_http) == set(payload_native)
        assert payload_http["status"] == payload_native["status"] == "in_progress"
        for payload in (payload_http, payload_native):
            row = _wait_task_status(db_path, payload["task_id"], "completed")
            assert row["result"] == "via http"
            assert row["origin"] == "delegate"

    def test_http_sem_sse_respeita_parallel(self, tools_com_db):
        tools, db_path = tools_com_db
        iniciados: list[str] = []
        barreira = threading.Barrier(2, timeout=5)

        def dispatch(agent, **_kw):
            iniciados.append(agent)
            barreira.wait()  # só passa se os dois steps rodarem juntos
            return f"resp-{agent}"

        tools.set_delegate_fn(dispatch)
        call = self._http_call(args={
            "target_agent": "codex",
            "request": "t1",
            "steps": [{"target_agent": "gemini", "request": "t2"}],
            "parallel": True,
        })

        result = tools.delegate(call)

        assert result.ok is True
        task_id = json.loads(result.content)["task_id"]
        row = _wait_task_status(db_path, task_id, "completed")
        assert "[codex] resp-codex" in row["result"]
        assert "[gemini] resp-gemini" in row["result"]
        assert sorted(iniciados) == ["codex", "gemini"]

    def test_http_com_sse_bloqueia_e_registra_task(self, tools_com_db):
        tools, db_path = tools_com_db
        tools.set_delegate_fn(MagicMock(return_value="inline via sse"))

        result = tools.delegate(self._http_call(sse=True))

        assert result.ok is True
        assert result.content.startswith("inline via sse")
        rows = task_api.list_tasks({"id": result.data["task_id"]}, db_path=db_path)
        assert rows and rows[0]["status"] == "completed"
        assert rows[0]["result"] == "inline via sse"

    def test_http_com_sse_e_wait_false_vira_assincrono(self, tools_com_db):
        tools, db_path = tools_com_db
        tools.set_delegate_fn(MagicMock(return_value="assíncrono via sse"))
        call = self._http_call(
            args={"target_agent": "codex", "request": "faz algo", "wait": False},
            sse=True,
        )

        result = tools.delegate(call)

        assert result.ok is True
        payload = json.loads(result.content)
        assert payload["status"] == "in_progress"
        row = _wait_task_status(db_path, payload["task_id"], "completed")
        assert row["result"] == "assíncrono via sse"

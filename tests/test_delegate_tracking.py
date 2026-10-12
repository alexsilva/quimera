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
from quimera.workspace import Workspace
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
    workspace = Workspace(tmp_path)
    db_path = workspace.tasks_db
    task_api.init_db(str(db_path))
    config = ToolRuntimeConfig(workspace=workspace)
    tools = DelegateTools(config)
    return tools, str(db_path)


@pytest.fixture
def tools_sem_db(tmp_path):
    config = ToolRuntimeConfig(workspace=Workspace(tmp_path))
    tools = DelegateTools(config)
    tools._get_db_path = lambda: None
    return tools


def _wait_task_status(db_path: str, task_id: int, status: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = task_api.list_tasks({"id": task_id}, db_path=db_path)
        if rows and rows[0]["status"] == status:
            return rows[0]
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} não atingiu status '{status}' em {timeout}s")


def _wait_job_status(db_path: str, job_id: int, status: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = task_api.get_job(job_id, db_path=db_path)
        if job and job["status"] == status:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} não atingiu status '{status}' em {timeout}s")


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
        config = ToolRuntimeConfig(workspace=Workspace(tmp_path))
        tools = DelegateTools(config)
        tools._get_db_path = lambda: str(tmp_path)
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
            # Margem generosa: sob carga, expirar antes do assert de
            # in_progress completa a task cedo demais e o teste flake-a.
            liberar.wait(timeout=30)
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
        job = _wait_job_status(db_path, payload["job_id"], "completed")
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


class TestFailureReceipt:
    """Falha de delegação produz recibo estruturado para o delegador."""

    @staticmethod
    def _failing_fn(receipt: dict):
        from quimera.runtime.errors import DelegationDispatchError

        def _fn(agent, **opts):
            raise DelegationDispatchError(
                f"Agent '{agent}' failed ({receipt.get('error_kind')})",
                receipt=receipt,
            )
        return _fn

    def test_sync_falha_anexa_failure_receipt_no_result(self, tools_com_db):
        tools, db_path = tools_com_db
        receipt = {
            "error_kind": "process_died",
            "exit_code": -9,
            "signal": "SIGKILL",
            "stderr_tail": "Killed",
            "duration_s": 12.3,
        }
        tools.set_delegate_fn(self._failing_fn(receipt))

        result = tools.delegate(_make_call())

        assert result.ok is False
        assert "process_died" in (result.error or "")
        failure = result.data["failure_receipt"]
        step = failure["steps"][0]
        assert step["target_agent"] == "codex"
        attempt = step["attempts"][0]
        assert attempt["agent"] == "codex"
        assert attempt["error_kind"] == "process_died"
        assert attempt["exit_code"] == -9
        assert attempt["signal"] == "SIGKILL"
        assert attempt["stderr_tail"] == "Killed"

    def test_falha_persiste_recibo_nas_notes_da_task(self, tools_com_db):
        tools, db_path = tools_com_db
        receipt = {"error_kind": "rate_limited", "retry_after": 30}
        tools.set_delegate_fn(self._failing_fn(receipt))

        result = tools.delegate(_make_call())

        rows = task_api.list_tasks({"id": result.data["task_id"]}, db_path=db_path)
        assert rows and rows[0]["status"] == "failed"
        notes = json.loads(rows[0]["notes"])
        assert notes["steps"][0]["attempts"][0]["error_kind"] == "rate_limited"

    def test_fallback_registra_uma_tentativa_por_agente(self, tools_com_db):
        tools, _ = tools_com_db
        from quimera.runtime.errors import DelegationDispatchError

        kinds = {"codex": "process_died", "gemini": "rate_limited"}

        def _fn(agent, **opts):
            raise DelegationDispatchError(
                f"falhou {agent}", receipt={"error_kind": kinds[agent]},
            )

        tools.set_delegate_fn(_fn)
        call = _make_call(args={
            "target_agent": "codex",
            "request": "faz algo",
            "fallback_agents": ["gemini"],
        })

        result = tools.delegate(call)

        assert result.ok is False
        attempts = result.data["failure_receipt"]["steps"][0]["attempts"]
        assert [a["error_kind"] for a in attempts] == ["process_died", "rate_limited"]
        assert [a["agent"] for a in attempts] == ["codex", "gemini"]

    def test_empty_response_sem_recibo_tem_error_kind_generico(self, tools_com_db):
        tools, _ = tools_com_db
        tools.set_delegate_fn(MagicMock(return_value=None))

        result = tools.delegate(_make_call())

        attempt = result.data["failure_receipt"]["steps"][0]["attempts"][0]
        assert attempt["error_kind"] == "empty_response"

    def test_async_persiste_recibo_na_task(self, tools_com_db):
        tools, db_path = tools_com_db
        receipt = {"error_kind": "wall_timeout", "duration_s": 601.0}
        tools.set_delegate_fn(self._failing_fn(receipt))
        call = _make_call(args={
            "target_agent": "codex", "request": "faz algo", "wait": False,
        })

        result = tools.delegate(call)

        assert result.ok is True
        task_id = json.loads(result.content)["task_id"]
        row = _wait_task_status(db_path, task_id, "failed")
        notes = json.loads(row["notes"])
        assert notes["steps"][0]["attempts"][0]["error_kind"] == "wall_timeout"


class TestFallbackReceipt:
    """Sucesso via fallback fica visível: recibo, notes e assigned_to.

    Regressão do teste ao vivo de 2026-10-11: o alvo primário morto 3× por
    SIGKILL sumia do registro quando o fallback completava — a task ficava
    completed com assigned_to do agente errado e notes vazias.
    """

    @staticmethod
    def _fn_falha_codex_responde_outros():
        from quimera.runtime.errors import DelegationDispatchError

        def _fn(agent, **_opts):
            if agent == "codex":
                raise DelegationDispatchError(
                    "morreu", receipt={"error_kind": "process_died", "exit_code": -9},
                )
            return "feito pelo fallback"

        return _fn

    def test_sucesso_apos_fallback_anexa_fallback_receipt(self, tools_com_db):
        tools, _ = tools_com_db
        tools.set_delegate_fn(self._fn_falha_codex_responde_outros())
        call = _make_call(args={
            "target_agent": "codex",
            "request": "faz algo",
            "fallback_agents": ["gemini"],
        })

        result = tools.delegate(call)

        assert result.ok is True
        receipt = result.data["fallback_receipt"]
        step = receipt["steps"][0]
        assert step["target_agent"] == "codex"
        assert step["selected_agent"] == "gemini"
        assert step["attempts"][0]["error_kind"] == "process_died"
        assert step["attempts"][0]["exit_code"] == -9
        # Sem snapshot de host: o estado no sucesso não é o das falhas.
        assert "host" not in receipt

    def test_sucesso_apos_fallback_persiste_notes_e_assigned_to(self, tools_com_db):
        tools, db_path = tools_com_db
        tools.set_delegate_fn(self._fn_falha_codex_responde_outros())
        call = _make_call(args={
            "target_agent": "codex",
            "request": "faz algo",
            "fallback_agents": ["gemini"],
        })

        result = tools.delegate(call)

        rows = task_api.list_tasks({"id": result.data["task_id"]}, db_path=db_path)
        assert rows and rows[0]["status"] == "completed"
        assert rows[0]["assigned_to"] == "gemini"
        notes = json.loads(rows[0]["notes"])
        assert notes["steps"][0]["selected_agent"] == "gemini"

    def test_sucesso_direto_nao_anexa_recibo_nem_notes(self, tools_com_db):
        tools, db_path = tools_com_db
        tools.set_delegate_fn(MagicMock(return_value="direto"))

        result = tools.delegate(_make_call())

        assert result.ok is True
        assert "fallback_receipt" not in result.data
        rows = task_api.list_tasks({"id": result.data["task_id"]}, db_path=db_path)
        assert rows[0]["assigned_to"] == "codex"
        assert rows[0]["notes"] is None

    def test_async_sucesso_apos_fallback_persiste_na_task(self, tools_com_db):
        tools, db_path = tools_com_db
        tools.set_delegate_fn(self._fn_falha_codex_responde_outros())
        call = _make_call(args={
            "target_agent": "codex",
            "request": "faz algo",
            "fallback_agents": ["gemini"],
            "wait": False,
        })

        result = tools.delegate(call)

        task_id = json.loads(result.content)["task_id"]
        row = _wait_task_status(db_path, task_id, "completed")
        assert row["assigned_to"] == "gemini"
        notes = json.loads(row["notes"])
        assert notes["steps"][0]["attempts"][0]["error_kind"] == "process_died"

    def test_paralelo_sucesso_apos_fallback_anexa_recibo(self, tools_com_db):
        tools, db_path = tools_com_db
        tools.set_delegate_fn(self._fn_falha_codex_responde_outros())
        call = _make_call(args={
            "target_agent": "codex",
            "request": "t1",
            "fallback_agents": ["gemini"],
            "steps": [{"target_agent": "claude", "request": "t2"}],
            "parallel": True,
        })

        result = tools.delegate(call)

        assert result.ok is True
        steps = result.data["fallback_receipt"]["steps"]
        assert len(steps) == 1
        assert steps[0]["step"] == 0
        assert steps[0]["selected_agent"] == "gemini"
        rows = task_api.list_tasks({"id": result.data["task_id"]}, db_path=db_path)
        assert rows[0]["assigned_to"] == "gemini"

    def test_step_posterior_falha_mantem_recibos_de_falha_e_fallback(self, tools_com_db):
        tools, _ = tools_com_db
        from quimera.runtime.errors import DelegationDispatchError

        def _fn(agent, **_opts):
            if agent == "codex":
                raise DelegationDispatchError(
                    "morreu", receipt={"error_kind": "process_died"},
                )
            if agent == "claude":
                raise DelegationDispatchError(
                    "limite", receipt={"error_kind": "rate_limited"},
                )
            return "ok"

        tools.set_delegate_fn(_fn)
        call = _make_call(args={
            "target_agent": "codex",
            "request": "t1",
            "fallback_agents": ["gemini"],
            "steps": [{"target_agent": "claude", "request": "t2"}],
        })

        result = tools.delegate(call)

        assert result.ok is False
        failure = result.data["failure_receipt"]["steps"][0]
        assert failure["step"] == 1
        assert failure["attempts"][0]["error_kind"] == "rate_limited"
        fallback = result.data["fallback_receipt"]["steps"][0]
        assert fallback["step"] == 0
        assert fallback["selected_agent"] == "gemini"

    def test_complete_task_persiste_notes_e_executed_by(self, tools_com_db):
        _, db_path = tools_com_db
        job_id = task_api.add_job("job de teste", db_path=db_path)
        task_id = task_api.create_task(
            job_id, "tarefa", assigned_to="codex", status="in_progress",
            db_path=db_path,
        )

        ok = task_api.complete_task(
            task_id,
            result="feito",
            notes='{"origem": "fallback"}',
            executed_by="gemini",
            db_path=db_path,
        )

        assert ok is True
        row = task_api.list_tasks({"id": task_id}, db_path=db_path)[0]
        assert row["status"] == "completed"
        assert row["assigned_to"] == "gemini"
        assert json.loads(row["notes"]) == {"origem": "fallback"}

    def test_complete_task_sem_notes_preserva_limpeza(self, tools_com_db):
        _, db_path = tools_com_db
        job_id = task_api.add_job("job de teste", db_path=db_path)
        task_id = task_api.create_task(
            job_id, "tarefa", assigned_to="codex", status="in_progress",
            notes="rascunho", db_path=db_path,
        )

        assert task_api.complete_task(task_id, result="feito", db_path=db_path)
        row = task_api.list_tasks({"id": task_id}, db_path=db_path)[0]
        assert row["assigned_to"] == "codex"
        assert row["notes"] is None

    def test_complete_task_posicional_antigo_mantem_db_path(self, tools_com_db):
        _, db_path = tools_com_db
        job_id = task_api.add_job("job de teste", db_path=db_path)
        task_id = task_api.create_task(
            job_id, "tarefa", assigned_to="codex", status="in_progress",
            db_path=db_path,
        )

        # Contrato pré-existente: db_path como quarto argumento posicional.
        assert task_api.complete_task(task_id, "feito", None, db_path) is True
        row = task_api.list_tasks({"id": task_id}, db_path=db_path)[0]
        assert row["status"] == "completed"

    def test_async_falha_parcial_persiste_ambos_recibos_nas_notes(self, tools_com_db):
        tools, db_path = tools_com_db
        from quimera.runtime.errors import DelegationDispatchError

        def _fn(agent, **_opts):
            if agent == "codex":
                raise DelegationDispatchError(
                    "morreu", receipt={"error_kind": "process_died"},
                )
            if agent == "claude":
                raise DelegationDispatchError(
                    "limite", receipt={"error_kind": "rate_limited"},
                )
            return "ok"

        tools.set_delegate_fn(_fn)
        call = _make_call(args={
            "target_agent": "codex",
            "request": "t1",
            "fallback_agents": ["gemini"],
            "steps": [{"target_agent": "claude", "request": "t2"}],
            "wait": False,
        })

        result = tools.delegate(call)

        task_id = json.loads(result.content)["task_id"]
        row = _wait_task_status(db_path, task_id, "failed")
        notes = json.loads(row["notes"])
        assert notes["steps"][0]["attempts"][0]["error_kind"] == "rate_limited"
        assert notes["fallback_receipt"]["steps"][0]["selected_agent"] == "gemini"

    def test_cancel_entre_steps_preserva_recibo_de_fallback(self):
        from quimera.runtime.errors import DelegationDispatchError

        done_first = threading.Event()

        def _fn(agent, **_opts):
            if agent == "codex":
                raise DelegationDispatchError(
                    "morreu", receipt={"error_kind": "process_died"},
                )
            done_first.set()
            return "ok do fallback"

        steps = [
            {"target_agent": "codex", "request": "t1", "context": "",
             "fallback_agents": ["gemini"], "role": "", "access_list": []},
            {"target_agent": "claude", "request": "t2", "context": "",
             "fallback_agents": [], "role": "", "access_list": []},
        ]

        result = DelegateTools._execute_steps_inner(
            steps, _fn, None, lambda: set(), lambda a: a or "",
            cancel_checker=done_first.is_set,
        )

        assert result.ok is False
        assert "cancelada" in (result.error or "")
        assert result.data["fallback_receipt"]["steps"][0]["selected_agent"] == "gemini"

    def test_cancelamento_apos_fallback_persiste_recibo_nas_notes(self, tools_com_db):
        tools, db_path = tools_com_db
        from quimera.runtime.errors import DelegationDispatchError
        from quimera.runtime.tools.tasks import TaskTools

        started = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()

        def _fn(agent, **opts):
            if agent == "codex":
                raise DelegationDispatchError(
                    "morreu", receipt={"error_kind": "process_died"},
                )
            if agent == "gemini":
                return "ok do fallback"
            handle = opts.get("cancel_handle")
            if handle is not None:
                handle.register(cancelled.set)
            started.set()
            release.wait(timeout=10)
            return None if cancelled.is_set() else "resposta tardia"

        tools.set_delegate_fn(_fn)
        metadata = {
            "trusted_context": TrustedToolExecutionContext(
                transport="http_mcp", run_id="r-fb", agent_name="claude",
            ),
        }
        call = ToolCall(name="delegate", arguments={
            "target_agent": "codex",
            "request": "t1",
            "fallback_agents": ["gemini"],
            "steps": [{"target_agent": "sonnet", "request": "t2"}],
            "wait": False,
        }, metadata=metadata)
        result = tools.delegate(call)
        task_id = json.loads(result.content)["task_id"]
        assert started.wait(timeout=5)

        task_tools = TaskTools(ToolRuntimeConfig(workspace=tools.workspace))
        cancel_call = ToolCall(
            name="tasks",
            arguments={"action": "cancel", "task_id": task_id},
            metadata=metadata,
        )
        cancel_result = task_tools.tasks(cancel_call)
        assert cancel_result.ok is True
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not cancelled.is_set():
            time.sleep(0.02)
        release.set()

        row = _wait_task_status(db_path, task_id, "cancelled")
        notes = json.loads(row["notes"])
        assert notes["cancelled_by"] == "claude"
        assert notes["fallback_receipt"]["steps"][0]["selected_agent"] == "gemini"

    def test_cancelamento_durante_fallback_registra_executor_ativo(self, tools_com_db):
        """Regressão live 4: fallback cancelado não pode sumir do recibo/assigned_to."""
        tools, db_path = tools_com_db
        from quimera.runtime.errors import DelegationDispatchError
        from quimera.runtime.tools.tasks import TaskTools

        fallback_started = threading.Event()
        fallback_cancelled = threading.Event()
        release = threading.Event()

        def _fn(agent, **opts):
            if agent == "codex":
                raise DelegationDispatchError(
                    "morreu",
                    receipt={"error_kind": "process_died", "exit_code": -9},
                )
            handle = opts.get("cancel_handle")
            assert handle is not None
            handle.register(fallback_cancelled.set)
            fallback_started.set()
            release.wait(timeout=10)
            return None if fallback_cancelled.is_set() else "resposta tardia"

        tools.set_delegate_fn(_fn)
        metadata = {
            "trusted_context": TrustedToolExecutionContext(
                transport="http_mcp",
                run_id="r-active-fallback",
                agent_name="claude",
            ),
        }
        result = tools.delegate(ToolCall(
            name="delegate",
            arguments={
                "target_agent": "codex",
                "request": "t1",
                "fallback_agents": ["gemini"],
                "wait": False,
            },
            metadata=metadata,
        ))
        task_id = json.loads(result.content)["task_id"]
        assert fallback_started.wait(timeout=5)

        task_tools = TaskTools(ToolRuntimeConfig(workspace=tools.workspace))
        cancel_result = task_tools.tasks(ToolCall(
            name="tasks",
            arguments={"action": "cancel", "task_id": task_id},
            metadata=metadata,
        ))
        assert cancel_result.ok is True
        assert fallback_cancelled.wait(timeout=2)
        release.set()

        row = _wait_task_status(db_path, task_id, "cancelled")
        notes = json.loads(row["notes"])
        attempts = notes["steps"][0]["attempts"]
        assert [attempt["agent"] for attempt in attempts] == ["codex", "gemini"]
        assert attempts[-1]["error_kind"] == "cancelled"
        assert notes["steps"][0]["error"] == "Execução cancelada"
        assert notes["cancelled_by"] == "claude"
        assert row["assigned_to"] == "gemini"


class TestFinalizeContraEscritoresConcorrentes:
    """O desfecho da delegação vence escritores concorrentes da task.

    Regressão do teste ao vivo de 2026-10-11: o AgentFailureTracker removia o
    agente do pool no meio da delegação e release_agent_tasks resetava a task
    de rastreamento para pending; o finalize então perdia o recibo porque
    pending→failed não é transição válida.
    """

    @staticmethod
    def _failing_fn_com_efeito(receipt: dict, side_effect):
        from quimera.runtime.errors import DelegationDispatchError

        def _fn(agent, **opts):
            side_effect()
            raise DelegationDispatchError(
                f"Agent '{agent}' failed ({receipt.get('error_kind')})",
                receipt=receipt,
            )
        return _fn

    def test_release_durante_delegacao_nao_orfana_task(self, tools_com_db):
        tools, db_path = tools_com_db
        receipt = {"error_kind": "process_died", "exit_code": -9, "signal": "SIGKILL"}
        tools.set_delegate_fn(self._failing_fn_com_efeito(
            receipt,
            lambda: task_api.release_agent_tasks("codex", db_path=db_path),
        ))

        result = tools.delegate(_make_call())

        rows = task_api.list_tasks({"id": result.data["task_id"]}, db_path=db_path)
        assert rows and rows[0]["status"] == "failed"
        assert rows[0]["assigned_to"] == "codex"
        notes = json.loads(rows[0]["notes"])
        assert notes["steps"][0]["attempts"][0]["error_kind"] == "process_died"
        job = task_api.get_job(result.data["job_id"], db_path=db_path)
        assert job["status"] == "failed"

    @staticmethod
    def _reset_tracking_task(db_path: str):
        def _reset():
            rows = task_api.list_tasks({"status": "in_progress"}, db_path=db_path)
            assert rows, "task de rastreamento deveria estar in_progress"
            task_api.transition_task(rows[0]["id"], "pending", db_path=db_path)
        return _reset

    def test_finalize_recupera_task_resetada_por_escritor_concorrente(self, tools_com_db):
        tools, db_path = tools_com_db
        receipt = {"error_kind": "process_died", "exit_code": -9}
        tools.set_delegate_fn(self._failing_fn_com_efeito(
            receipt, self._reset_tracking_task(db_path),
        ))

        result = tools.delegate(_make_call())

        rows = task_api.list_tasks({"id": result.data["task_id"]}, db_path=db_path)
        assert rows and rows[0]["status"] == "failed"
        notes = json.loads(rows[0]["notes"])
        assert notes["steps"][0]["attempts"][0]["error_kind"] == "process_died"
        job = task_api.get_job(result.data["job_id"], db_path=db_path)
        assert job["status"] == "failed"

    def test_finalize_recupera_sucesso_resetado_por_escritor_concorrente(self, tools_com_db):
        tools, db_path = tools_com_db
        reset = self._reset_tracking_task(db_path)

        def _fn(agent, **opts):
            reset()
            return "resposta do delegado"

        tools.set_delegate_fn(_fn)

        result = tools.delegate(_make_call())

        assert result.ok is True
        rows = task_api.list_tasks({"id": result.data["task_id"]}, db_path=db_path)
        assert rows and rows[0]["status"] == "completed"
        assert rows[0]["result"] == "resposta do delegado"
        job = task_api.get_job(result.data["job_id"], db_path=db_path)
        assert job["status"] == "completed"


class TestCancelamentoPeloDelegador:
    """Delegações rastreadas podem ser canceladas por quem delegou."""

    @staticmethod
    def _blocking_fn(started: threading.Event, release: threading.Event, observed: dict):
        def _fn(agent, **opts):
            handle = opts.get("cancel_handle")
            if handle is not None:
                handle.register(lambda: observed.setdefault("handle_cancelled", True))
            started.set()
            release.wait(timeout=10)
            return None if observed.get("handle_cancelled") else "resposta tardia"
        return _fn

    @staticmethod
    def _task_tools(workspace):
        from quimera.runtime.tools.tasks import TaskTools
        return TaskTools(ToolRuntimeConfig(workspace=workspace))

    @staticmethod
    def _cancel_call(task_id: int, agent_name: str | None, reason: str | None = None) -> ToolCall:
        metadata = {}
        if agent_name is not None:
            metadata["trusted_context"] = TrustedToolExecutionContext(
                transport="http_mcp", run_id="r-cancel", agent_name=agent_name,
            )
        args: dict = {"action": "cancel", "task_id": task_id}
        if reason:
            args["reason"] = reason
        return ToolCall(name="tasks", arguments=args, metadata=metadata)

    def _start_async_delegation(self, tools, source_agent: str = "claude"):
        started = threading.Event()
        release = threading.Event()
        observed: dict = {}
        tools.set_delegate_fn(self._blocking_fn(started, release, observed))
        metadata = {
            "trusted_context": TrustedToolExecutionContext(
                transport="http_mcp", run_id="r1", agent_name=source_agent,
            ),
        }
        call = _make_call(
            args={"target_agent": "codex", "request": "tarefa longa", "wait": False},
            metadata=metadata,
        )
        result = tools.delegate(call)
        assert result.ok is True
        task_id = json.loads(result.content)["task_id"]
        assert started.wait(timeout=5), "delegate_fn não iniciou"
        return task_id, release, observed

    def test_delegador_cancela_e_task_fica_cancelled(self, tools_com_db):
        tools, db_path = tools_com_db
        task_id, release, observed = self._start_async_delegation(tools)
        task_tools = self._task_tools(tools.workspace)

        result = task_tools.tasks(self._cancel_call(task_id, "claude", reason="mudei de plano"))

        assert result.ok is True
        assert result.data["status"] == "cancelling"
        assert result.data["cancelled_by"] == "claude"
        # O watcher propaga o cancel ao handle do step em ciclos de 50ms;
        # espere a propagação antes de liberar o agente fake.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not observed.get("handle_cancelled"):
            time.sleep(0.02)
        assert observed.get("handle_cancelled") is True
        release.set()
        row = _wait_task_status(db_path, task_id, "cancelled")
        notes = json.loads(row["notes"])
        assert notes["cancelled_by"] == "claude"
        assert notes["cancel_reason"] == "mudei de plano"
        assert "cancelada por claude" in row["result"]
        job = task_api.list_jobs(db_path=db_path)[-1]
        assert job["status"] == "cancelled"

    def test_agente_alheio_nao_pode_cancelar(self, tools_com_db):
        tools, db_path = tools_com_db
        task_id, release, _ = self._start_async_delegation(tools, source_agent="claude")
        task_tools = self._task_tools(tools.workspace)

        result = task_tools.tasks(self._cancel_call(task_id, "gemini"))

        assert result.ok is False
        assert "apenas quem delegou" in (result.error or "")
        release.set()
        _wait_task_status(db_path, task_id, "completed")

    def test_humano_sem_identidade_pode_cancelar(self, tools_com_db):
        tools, db_path = tools_com_db
        task_id, release, _ = self._start_async_delegation(tools)
        task_tools = self._task_tools(tools.workspace)

        result = task_tools.tasks(self._cancel_call(task_id, None))

        assert result.ok is True
        assert result.data["cancelled_by"] == "humano"
        release.set()
        _wait_task_status(db_path, task_id, "cancelled")

    def test_cancelar_task_inexistente_ou_terminada_retorna_erro(self, tools_com_db):
        tools, db_path = tools_com_db
        tools.set_delegate_fn(MagicMock(return_value="ok"))
        done = tools.delegate(_make_call())
        task_tools = self._task_tools(tools.workspace)

        result = task_tools.tasks(self._cancel_call(done.data["task_id"], "claude"))

        assert result.ok is False
        assert "não é uma delegação em execução" in (result.error or "")
        assert "completed" in (result.error or "")

    def test_transicao_cancelled_valida_somente_de_in_progress(self):
        from quimera.domain.task_states import can_transition

        assert can_transition("in_progress", "cancelled") is True
        assert can_transition("completed", "cancelled") is False
        assert can_transition("cancelled", "in_progress") is False


class TestCancelamentoCorridasEAutorizacao:
    """Casos adversariais da revisão: corrida, autorização e propagação."""

    _blocking_fn = staticmethod(TestCancelamentoPeloDelegador._blocking_fn)
    _task_tools = staticmethod(TestCancelamentoPeloDelegador._task_tools)
    _cancel_call = staticmethod(TestCancelamentoPeloDelegador._cancel_call)

    def _start(self, tools, source_agent: str | None = "claude"):
        started = threading.Event()
        release = threading.Event()
        observed: dict = {}
        tools.set_delegate_fn(self._blocking_fn(started, release, observed))
        metadata = {}
        if source_agent is not None:
            metadata["trusted_context"] = TrustedToolExecutionContext(
                transport="http_mcp", run_id="r1", agent_name=source_agent,
            )
        call = _make_call(
            args={"target_agent": "codex", "request": "tarefa longa", "wait": False},
            metadata=metadata,
        )
        result = tools.delegate(call)
        assert result.ok is True
        task_id = json.loads(result.content)["task_id"]
        assert started.wait(timeout=5)
        return task_id, release, observed

    def test_cancel_apos_finalizacao_e_rejeitado(self, tools_com_db):
        """Entry finalizada rejeita cancelamento tardio (corrida fechada)."""
        from quimera.runtime.tools.delegate import get_active_delegation

        tools, db_path = tools_com_db
        task_id, release, _ = self._start(tools)
        entry = get_active_delegation(task_id)
        assert entry is not None
        # Simula a corrida: finalização vence antes de o cancel chegar.
        assert entry.mark_finalized() is False
        task_tools = self._task_tools(tools.workspace)

        result = task_tools.tasks(self._cancel_call(task_id, "claude"))

        assert result.ok is False
        assert "já foi finalizada" in (result.error or "")
        assert entry.cancelled is False
        release.set()

    def test_request_cancel_apos_mark_finalized_retorna_finalized(self):
        from quimera.runtime.tools.delegate import ActiveDelegationEntry

        entry = ActiveDelegationEntry(1, source_agent="claude", targets=("codex",))
        assert entry.mark_finalized() is False
        assert entry.request_cancel("claude") == "finalized"
        assert entry.cancelled is False

    def test_delegacao_sem_dono_nao_pode_ser_cancelada_por_agente(self, tools_com_db):
        """source_agent vazio: só o humano cancela — agente identificado é rejeitado."""
        tools, db_path = tools_com_db
        task_id, release, _ = self._start(tools, source_agent=None)
        task_tools = self._task_tools(tools.workspace)

        result = task_tools.tasks(self._cancel_call(task_id, "gemini"))

        assert result.ok is False
        assert "apenas quem delegou" in (result.error or "")
        release.set()
        _wait_task_status(db_path, task_id, "completed")

    def test_cancel_http_sem_identidade_e_rejeitado(self, tools_com_db):
        """Via HTTP MCP, omitir o header de identidade não concede privilégio."""
        tools, db_path = tools_com_db
        task_id, release, _ = self._start(tools)
        task_tools = self._task_tools(tools.workspace)
        call = ToolCall(
            name="tasks",
            arguments={"action": "cancel", "task_id": task_id},
            metadata={
                "trusted_context": TrustedToolExecutionContext(
                    transport="http_mcp", run_id="r-anon", agent_name=None,
                ),
            },
        )

        result = task_tools.tasks(call)

        assert result.ok is False
        assert "requer identidade" in (result.error or "")
        release.set()
        _wait_task_status(db_path, task_id, "completed")

    def test_cancel_do_job_marca_completed_at(self, tools_com_db):
        tools, db_path = tools_com_db
        task_id, release, _ = self._start(tools)
        task_tools = self._task_tools(tools.workspace)

        assert task_tools.tasks(self._cancel_call(task_id, "claude")).ok is True
        release.set()
        _wait_task_status(db_path, task_id, "cancelled")
        job = task_api.list_jobs(db_path=db_path)[-1]
        assert job["status"] == "cancelled"
        assert job["completed_at"] is not None

    def test_http_sse_propaga_cancel_da_request_ao_handle(self, tools_com_db):
        """notifications/cancelled do MCP chega ao cancel_handle no caminho SSE."""
        from unittest.mock import MagicMock as _MagicMock

        tools, db_path = tools_com_db
        started = threading.Event()
        release = threading.Event()
        observed: dict = {}
        tools.set_delegate_fn(self._blocking_fn(started, release, observed))
        mcp_cancel = threading.Event()
        call = _make_call(
            args={"target_agent": "codex", "request": "tarefa longa"},
            metadata={
                "trusted_context": TrustedToolExecutionContext(
                    transport="http_mcp", run_id="r-sse", agent_name="claude",
                ),
                "_mcp_state": {"sse_queue": _MagicMock()},
                "_mcp_cancel_event": mcp_cancel,
            },
        )
        holder: dict = {}

        def _run():
            holder["result"] = tools.delegate(call)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        assert started.wait(timeout=5)

        mcp_cancel.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not observed.get("handle_cancelled"):
            time.sleep(0.02)
        assert observed.get("handle_cancelled") is True
        release.set()
        worker.join(timeout=5)
        assert holder["result"].ok is False

    def test_falha_no_start_da_thread_nao_vaza_registry(self, tools_com_db):
        """Thread.start com erro desfaz o registro e marca a task como failed."""
        from unittest.mock import patch
        from quimera.runtime.tools.delegate import get_active_delegation

        tools, db_path = tools_com_db
        tools.set_delegate_fn(MagicMock(return_value="nunca roda"))
        thread_mock = MagicMock()
        thread_mock.start.side_effect = RuntimeError("can't start new thread")
        call = _make_call(args={
            "target_agent": "codex", "request": "faz algo", "wait": False,
        })

        with patch(
            "quimera.runtime.tools.delegate.threading.Thread",
            return_value=thread_mock,
        ):
            result = tools.delegate(call)

        assert result.ok is False
        assert "failed to start delegate thread" in (result.error or "")
        rows = task_api.list_tasks({"status": "failed"}, db_path=db_path)
        assert rows and "failed to start delegate thread" in rows[-1]["result"]
        assert get_active_delegation(rows[-1]["id"]) is None

"""Regressão: BrokenPipe em progresso MCP não pode abortar a tool nem vazar subprocessos.

Cenário real (sessão 2026-09-20-104855): o delegador disparou delegação
paralela para 15 agentes e finalizou; seu socket MCP fechou. O tick de
progresso seguinte de cada branch levantou BrokenPipeError, que subia por
on_tick → watch → run e abortava as 15 chamadas — transient "processando"
preso na tela e subprocessos CLI órfãos, culminando no LMK do Android
matando todas as sessões.
"""
from __future__ import annotations

import io
import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from quimera.agents.client import AgentClient
from quimera.agents.process_runner import ProcessRunner
from quimera.runtime.models import ToolResult

from tests.helpers import make_mcp_executor as _make_executor
from tests.helpers import make_mcp_server as _make_server


class _ProgressBrokenOut(io.StringIO):
    """Stream que simula socket morto apenas para notificações de progresso."""

    def __init__(self):
        super().__init__()
        self.progress_attempts = 0

    def write(self, s):
        if "notifications/progress" in s:
            self.progress_attempts += 1
            raise BrokenPipeError(32, "Broken pipe")
        return super().write(s)


def _serve_tool_call_with_progress(progress_messages):
    """Executa um tools/call cujo executor emite progresso num socket quebrado."""
    executor = _make_executor()
    finished = []

    def _execute(tool_call, progress_cb=None):
        if progress_cb:
            for msg in progress_messages:
                progress_cb(msg)
        finished.append(True)
        return ToolResult(ok=True, tool_name="read_file", content="done")

    executor.execute.side_effect = _execute
    server = _make_server(executor)

    inp = io.StringIO(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "read_file", "arguments": {},
                   "_meta": {"progressToken": "tok_1"}},
    }) + "\n")
    out = _ProgressBrokenOut()
    server.serve(stdin=inp, stdout=out)
    return finished, out


class TestProgressBrokenPipeServidor:
    """Progresso é fire-and-forget: socket morto não aborta a tool."""

    def test_broken_pipe_nao_aborta_tool_e_resposta_e_entregue(self):
        finished, out = _serve_tool_call_with_progress(["um", "dois", "três"])

        assert finished == [True], "executor deve completar apesar do BrokenPipe"
        responses = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        results = [r for r in responses if r.get("id") == 1 and "result" in r]
        assert results, f"resposta da tool deve ser entregue: {responses}"

    def test_broken_pipe_desativa_envios_seguintes(self):
        _, out = _serve_tool_call_with_progress(["um", "dois", "três"])

        assert out.progress_attempts == 1, (
            "após a primeira falha de escrita, progresso deve ser silenciado "
            f"(tentativas: {out.progress_attempts})"
        )


class TestWatchCallbackException:
    """Exceção de callback no watchdog não pode deixar o subprocesso órfão."""

    def test_on_tick_exception_termina_processo_e_propaga(self):
        proc = MagicMock()
        stdout_thread = MagicMock()
        stderr_thread = MagicMock()
        stdout_thread.is_alive.return_value = True
        stderr_thread.is_alive.return_value = False
        runner = ProcessRunner(
            proc, stdout_thread, stderr_thread,
            {"stderr": []}, threading.Event(), idle_timeout=None,
        )

        def _boom(elapsed):
            raise BrokenPipeError(32, "Broken pipe")

        with patch("quimera.agents.process_runner.terminate_process_group") as mock_term:
            with patch("time.sleep"), patch("time.monotonic") as mock_mono:
                mock_mono.side_effect = [100.0, 101.0]
                with pytest.raises(BrokenPipeError):
                    runner.watch(on_tick=_boom)

        mock_term.assert_called_once_with(proc)
        stdout_thread.join.assert_called_with(2)
        stderr_thread.join.assert_called_with(2)

    def test_on_item_exception_termina_processo_e_propaga(self):
        import queue as queue_mod

        proc = MagicMock()
        stdout_thread = MagicMock()
        stderr_thread = MagicMock()
        stdout_thread.is_alive.return_value = True
        stderr_thread.is_alive.return_value = False
        runner = ProcessRunner(
            proc, stdout_thread, stderr_thread,
            {"stderr": []}, threading.Event(), idle_timeout=None,
        )
        log_queue = queue_mod.Queue()
        log_queue.put(("stdout", "linha\n"))

        def _boom(stream_type, line):
            raise RuntimeError("callback quebrado")

        with patch("quimera.agents.process_runner.terminate_process_group") as mock_term:
            with patch("time.sleep"):
                with pytest.raises(RuntimeError):
                    runner.watch(log_queue=log_queue, on_item=_boom)

        mock_term.assert_called_once_with(proc)


class TestRunCleanupOnException:
    """run() em desenrolar por exceção limpa transient e mata o subprocesso."""

    def test_run_limpa_transient_e_termina_processo(self, renderer):
        client = AgentClient(renderer)
        with patch("subprocess.Popen") as mock_popen, \
                patch("quimera.agents.client.ProcessRunner") as mock_runner_cls, \
                patch("quimera.agents.client.terminate_process_group") as mock_term:
            mock_proc = MagicMock()
            mock_proc.stdout = iter(["ok\n"])
            mock_proc.stderr = iter([])
            mock_proc.stdin = MagicMock()
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc
            runner = mock_runner_cls.return_value
            runner.watch.side_effect = BrokenPipeError(32, "Broken pipe")

            with pytest.raises(BrokenPipeError):
                client.run(["codex", "exec"], silent=False, show_status=False, agent="codex")

        mock_term.assert_called_once_with(mock_proc)
        renderer.clear_agent_transient.assert_called()

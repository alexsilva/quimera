"""Testes para eventos assíncronos do MCP (concorrência, cancelamento, progresso)."""
from __future__ import annotations

import concurrent.futures
import http.client
import io
import json
import os
import socket
import threading
import time
from unittest.mock import patch

import pytest

from quimera.runtime.mcp import MCPServer, MCP_HTTPServer
from quimera.runtime.models import ToolCall, ToolResult

from tests.test_runtime_mcp_server import _make_executor, _make_server, _wait_for_socket


def _exchange(server, *msgs):
    """Envia msgs ao server e retorna lista de respostas JSON."""
    lines = "\n".join(json.dumps(m) for m in msgs) + "\n"
    inp = io.StringIO(lines)
    out = io.StringIO()
    server.serve(stdin=inp, stdout=out)
    responses = []
    for line in out.getvalue().splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = json.loads(line)
        if isinstance(parsed, list):
            responses.extend(parsed)
        else:
            responses.append(parsed)
    return responses


def _recv_line(sock: socket.socket, timeout: float = 5) -> bytes:
    """Lê uma linha de um socket."""
    sock.settimeout(timeout)
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = sock.recv(1)
        except socket.timeout:
            break
        if not data:
            break
        buf += data
        if data == b"\n":
            break
    return buf


class TestConcurrentToolsCall:
    """Testa chamadas concorrentes de ferramentas via thread pool."""

    def test_two_concurrent_calls(self, tmp_path):
        """Duas tools/call concorrentes: ambas completam."""
        executor = _make_executor()
        executor.execute.side_effect = lambda *a, **kw: (
            time.sleep(0.2) or ToolResult(ok=True, tool_name="read_file", content="ok")
        )
        server = _make_server(executor)
        sock_path = str(tmp_path / "mcp_conc.sock")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        results = []

        def _client(idx):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            msg = json.dumps({"jsonrpc": "2.0", "id": idx, "method": "tools/call",
                              "params": {"name": "read_file", "arguments": {}}}) + "\n"
            s.sendall(msg.encode())
            s.shutdown(socket.SHUT_WR)
            data = _recv_line(s, timeout=3)
            line = data.decode().strip()
            if line:
                results.append(json.loads(line))
            s.close()

        start = time.time()
        threads = [threading.Thread(target=_client, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = time.time() - start

        assert len(results) == 2, f"expected 2 responses, got {len(results)}"
        assert elapsed < 0.35, "deve executar concorrentemente (thread pool)"

    def test_response_ids_match_requests(self, tmp_path):
        """IDs das respostas correspondem aos IDs das requisições."""
        executor = _make_executor()
        executor.execute.side_effect = lambda *a, **kw: (
            time.sleep(0.1) or ToolResult(ok=True, tool_name="read_file", content="ok")
        )
        server = _make_server(executor)
        sock_path = str(tmp_path / "mcp_conc_ids.sock")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        results = {}

        def _client(idx):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            msg = json.dumps({"jsonrpc": "2.0", "id": idx, "method": "tools/call",
                              "params": {"name": "read_file", "arguments": {}}}) + "\n"
            s.sendall(msg.encode())
            s.shutdown(socket.SHUT_WR)
            data = _recv_line(s, timeout=3)
            line = data.decode().strip()
            if line:
                results[idx] = json.loads(line)
            s.close()

        threads = [threading.Thread(target=_client, args=(i,)) for i in [10, 20]]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results[10]["id"] == 10
        assert results[20]["id"] == 20
        assert "result" in results[10] or "error" in results[10]

    def test_does_not_block_each_other(self, tmp_path):
        """_handle_tools_call não bloqueia entre conexões (thread pool)."""
        executor = _make_executor()
        executor.execute.side_effect = lambda *a, **kw: (
            time.sleep(0.2) or ToolResult(ok=True, tool_name="read_file", content="ok")
        )
        server = _make_server(executor)
        sock_path = str(tmp_path / "mcp_conc_nb.sock")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        results = []

        def _client(idx):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            msg = json.dumps({"jsonrpc": "2.0", "id": idx, "method": "tools/call",
                              "params": {"name": "read_file", "arguments": {}}}) + "\n"
            s.sendall(msg.encode())
            s.shutdown(socket.SHUT_WR)
            data = _recv_line(s, timeout=3)
            line = data.decode().strip()
            if line:
                results.append(json.loads(line))
            s.close()

        start = time.perf_counter()
        threads = [threading.Thread(target=_client, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = time.perf_counter() - start

        assert len(results) == 2, f"expected 2 responses, got {len(results)}"
        assert elapsed < 0.38, "thread pool deve executar em paralelo"


class TestCancellation:
    """Testa notificações de cancelamento (notifications/cancelled)."""

    def test_cancel_sets_event(self):
        """Cancelar define o evento e descarta registro."""
        executor = _make_executor()
        server = _make_server(executor)

        event = threading.Event()
        server._cancel_events[1] = event
        server._handle_cancelled({"requestId": 1})

        assert event.is_set()
        assert 1 not in server._cancel_events

    def test_cancel_nonexistent_id_no_error(self):
        """Cancelar id inexistente não causa erro."""
        executor = _make_executor()
        server = _make_server(executor)

        server._handle_cancelled({"requestId": 999})

    def test_cancelled_tool_returns_no_response(self):
        """Tool cancelada não produz resposta (spec: 'Not send a response for the cancelled request')."""
        executor = _make_executor()
        executor.execute.side_effect = lambda *a, **kw: (
            time.sleep(0.3) or ToolResult(ok=True, tool_name="read_file", content="done")
        )
        server = _make_server(executor)

        lines = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "read_file", "arguments": {}}}) + "\n" +
            json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": 1}}) + "\n"
        )
        inp = io.StringIO(lines)
        out = io.StringIO()
        server.serve(stdin=inp, stdout=out)

        responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(responses) == 0, "cancelamento não deve gerar resposta"


class TestProgressNotifications:
    """Testa notificações de progresso (notifications/progress)."""

    def test_progress_with_token(self):
        """Com progressToken, notificações de progresso são emitidas."""
        executor = _make_executor()

        def _execute_with_progress(tool_call, progress_cb=None):
            if progress_cb:
                progress_cb("working")
                progress_cb("almost done")
            return ToolResult(ok=True, tool_name="read_file", content="done")

        executor.execute.side_effect = _execute_with_progress
        server = _make_server(executor)

        inp = io.StringIO(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {}, "_meta": {"progressToken": "tok_1"}},
        }) + "\n")
        out = io.StringIO()
        server.serve(stdin=inp, stdout=out)

        raw = out.getvalue()
        assert '"notifications/progress"' in raw
        assert '"progressToken":"tok_1"' in raw or '"progressToken": "tok_1"' in raw

    def test_progress_counter_increments(self):
        """Cada notificação de progresso incrementa o contador."""
        executor = _make_executor()

        def _execute_with_progress(tool_call, progress_cb=None):
            if progress_cb:
                progress_cb("step 1")
                progress_cb("step 2")
                progress_cb("step 3")
            return ToolResult(ok=True, tool_name="read_file", content="done")

        executor.execute.side_effect = _execute_with_progress
        server = _make_server(executor)

        inp = io.StringIO(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {}, "_meta": {"progressToken": "tok_1"}},
        }) + "\n")
        out = io.StringIO()
        server.serve(stdin=inp, stdout=out)

        raw = out.getvalue()
        progress_values = []
        for line in raw.splitlines():
            if '"notifications/progress"' in line:
                obj = json.loads(line)
                progress_values.append(obj["params"]["progress"])

        assert len(progress_values) >= 2, f"esperado >=2 progress, got {progress_values}"
        assert progress_values == [1, 2, 3], f"progress deve incrementar: {progress_values}"

    def test_no_progress_without_token(self):
        """Sem progressToken, nenhuma notificação de progresso é emitida."""
        executor = _make_executor()

        def _execute_with_progress(tool_call, progress_cb=None):
            if progress_cb:
                progress_cb("working")
            return ToolResult(ok=True, tool_name="read_file", content="done")

        executor.execute.side_effect = _execute_with_progress
        server = _make_server(executor)

        inp = io.StringIO(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {}},
        }) + "\n")
        out = io.StringIO()
        server.serve(stdin=inp, stdout=out)

        raw = out.getvalue()
        assert "notifications/progress" not in raw


class TestBatchAsync:
    """Testa requisições batch (lote) JSON-RPC 2.0."""

    def test_batch_two_pings_returns_array(self):
        """Batch com 2 requisições retorna array de respostas."""
        server = _make_server()
        responses = _exchange(
            server,
            [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
             {"jsonrpc": "2.0", "id": 2, "method": "ping"}],
        )
        assert isinstance(responses, list)
        assert len(responses) == 2
        ids = {r["id"] for r in responses}
        assert ids == {1, 2}
        for r in responses:
            assert r["result"] == {}

    def test_batch_one_item_returns_array(self):
        """Batch com 1 item retorna array com 1 elemento."""
        server = _make_server()
        responses = _exchange(
            server,
            [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
        )
        assert isinstance(responses, list)
        assert len(responses) == 1
        assert responses[0]["id"] == 1
        assert responses[0]["result"] == {}

    def test_batch_tools_call_does_not_block(self):
        """Batch com tools/call não bloqueia sequencialmente (executa em paralelo)."""
        executor = _make_executor()
        executor.execute.side_effect = lambda *a, **kw: (
            time.sleep(0.2) or ToolResult(ok=True, tool_name="read_file", content="ok")
        )
        server = _make_server(executor)

        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "read_file", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "read_file", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]

        start = time.time()
        responses = _exchange(server, batch)
        elapsed = time.time() - start

        assert elapsed < 0.3, f"batch tools/call deve executar em paralelo, levou {elapsed:.2f}s"
        assert isinstance(responses, list)
        assert len(responses) == 3
        ids = {r["id"] for r in responses if "result" in r}
        assert 1 in ids and 2 in ids and 3 in ids

    def test_batch_one_tool_call_returns_content(self):
        """Batch com 1 tools/call retorna content corretamente."""
        result = ToolResult(ok=True, tool_name="read_file", content="batch content")
        executor = _make_executor(call_result=result)
        server = _make_server(executor)

        responses = _exchange(server, [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "read_file", "arguments": {}}},
        ])
        assert len(responses) == 1
        assert responses[0]["result"]["content"][0]["text"] == "batch content"


class TestTimeout:
    """Testa timeout de execução de ferramentas."""

    def test_completed_tool_result_is_not_discarded_by_call_age(self):
        """Resultado concluído não deve virar timeout por idade da chamada."""
        executor = _make_executor()
        executor.execute.side_effect = lambda *a, **kw: (
            ToolResult(ok=True, tool_name="read_file", content="ok")
        )
        server = _make_server(executor)

        out = io.StringIO()
        server._handle_tools_call(1, {"name": "read_file", "arguments": {}}, out=out)

        with server._pending_lock:
            for c in server._pending_calls:
                if c["msg_id"] == 1:
                    c["started_at"] = time.perf_counter() - 601

        time.sleep(0.1)
        server._flush_pending(out)

        raw = out.getvalue()
        responses = [json.loads(l) for l in raw.splitlines() if l.strip()]
        assert len(responses) == 1
        assert responses[0]["result"]["isError"] is False
        assert responses[0]["result"]["content"][0]["text"] == "ok"

    def test_blocking_timeout_signals_cancel_and_removes_tracking(self):
        """Timeout bloqueante deve solicitar cancelamento da execução real."""
        server = _make_server(_make_executor())
        future = concurrent.futures.Future()
        cancel_event = threading.Event()
        call = {
            "msg_id": 77,
            "future": future,
            "out": io.StringIO(),
            "started_at": time.perf_counter(),
            "tool_name": "read_file",
            "cancel_event": cancel_event,
        }
        server._cancel_events[77] = cancel_event
        server._pending_calls.append(call)

        response = server._resolve_tool_response(call, wait_timeout=0.01)

        assert response["error"]["message"] == "Tool execution timed out"
        assert cancel_event.is_set()
        assert 77 not in server._cancel_events
        assert call not in server._pending_calls

    def test_batch_timeout_uses_single_deadline(self):
        """Batch não deve aguardar novamente depois de esgotar seu deadline."""
        server = _make_server(_make_executor())
        future = concurrent.futures.Future()
        cancel_event = threading.Event()
        out = io.StringIO()
        call = {
            "msg_id": 88,
            "future": future,
            "out": out,
            "started_at": time.perf_counter(),
            "tool_name": "read_file",
            "cancel_event": cancel_event,
        }
        server._cancel_events[88] = cancel_event
        server._pending_calls.append(call)
        responses = [None]

        with patch.object(server, "_configured_tool_timeout", return_value=0.01):
            started_at = time.perf_counter()
            server._resolve_batch_calls(out, responses, {88: 0})

        assert time.perf_counter() - started_at < 0.2
        assert cancel_event.is_set()
        assert responses[0]["error"]["message"] == "Tool execution timed out"

    def test_timeout_does_not_crash_server(self):
        """Timeout não crasha o servidor — chamadas seguintes funcionam."""
        server = _make_server(_make_executor())

        pending = concurrent.futures.Future()
        first_call = {
            "msg_id": 89,
            "future": pending,
            "out": io.StringIO(),
            "started_at": time.perf_counter(),
            "tool_name": "read_file",
            "cancel_event": threading.Event(),
        }
        first_response = server._resolve_tool_response(first_call, wait_timeout=0.01)

        completed = concurrent.futures.Future()
        completed.set_result(
            ToolResult(ok=True, tool_name="read_file", content="second")
        )
        second_call = {
            "msg_id": 90,
            "future": completed,
            "out": io.StringIO(),
            "started_at": time.perf_counter(),
            "tool_name": "read_file",
            "cancel_event": threading.Event(),
        }
        second_response = server._resolve_tool_response(
            second_call, wait_timeout=0.01
        )

        assert first_response["error"]["message"] == "Tool execution timed out"
        assert second_response["result"]["content"][0]["text"] == "second"

    def test_flush_pending_expires_stalled_call(self):
        """_flush_pending deve expirar tool call que nunca conclui."""
        release = threading.Event()
        executor = _make_executor()
        executor.config.mcp_tool_timeout_seconds = 1.0

        def _blocking_execute(call, progress=None):
            release.wait(10)
            return ToolResult(ok=True, tool_name="read_file", content="late")

        executor.execute.side_effect = _blocking_execute
        server = _make_server(executor)
        out = io.StringIO()

        try:
            server._handle_tools_call(11, {"name": "read_file", "arguments": {}}, out=out)
            with server._pending_lock:
                call = next(c for c in server._pending_calls if c["msg_id"] == 11)

            server._flush_pending(out)
            assert out.getvalue() == ""  # ainda dentro do deadline

            with server._pending_lock:
                call["deadline_at"] = time.perf_counter() - 0.01
            server._flush_pending(out)

            responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
            assert len(responses) == 1
            assert responses[0]["id"] == 11
            assert responses[0]["error"]["message"] == "Tool execution timed out"
            assert call["cancel_event"].is_set()
            with server._pending_lock:
                assert all(c["msg_id"] != 11 for c in server._pending_calls)
            assert 11 not in server._cancel_events

            # Não reemite a resposta em flushes posteriores.
            server._flush_pending(out)
            responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
            assert len(responses) == 1
        finally:
            release.set()

    def test_flush_pending_delivers_completed_call_past_deadline(self):
        """Chamada concluída após o deadline ainda entrega o resultado real."""
        executor = _make_executor()
        executor.config.mcp_tool_timeout_seconds = 1.0
        executor.execute.side_effect = lambda *a, **kw: ToolResult(
            ok=True, tool_name="read_file", content="ok-late"
        )
        server = _make_server(executor)
        out = io.StringIO()

        server._handle_tools_call(12, {"name": "read_file", "arguments": {}}, out=out)
        with server._pending_lock:
            call = next(c for c in server._pending_calls if c["msg_id"] == 12)
        call["future"].result(timeout=5)
        with server._pending_lock:
            call["deadline_at"] = time.perf_counter() - 0.01

        server._flush_pending(out)

        responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(responses) == 1
        assert responses[0]["result"]["isError"] is False
        assert responses[0]["result"]["content"][0]["text"] == "ok-late"
        assert not call["cancel_event"].is_set()

    def test_drain_all_pending_expires_stalled_call(self):
        """_drain_all_pending não bloqueia indefinidamente em tool travada."""
        release = threading.Event()
        executor = _make_executor()
        executor.config.mcp_tool_timeout_seconds = 1.0

        def _blocking_execute(call, progress=None):
            release.wait(10)
            return ToolResult(ok=True, tool_name="read_file", content="late")

        executor.execute.side_effect = _blocking_execute
        server = _make_server(executor)
        out = io.StringIO()

        try:
            server._handle_tools_call(13, {"name": "read_file", "arguments": {}}, out=out)
            with server._pending_lock:
                call = next(c for c in server._pending_calls if c["msg_id"] == 13)
                call["deadline_at"] = time.perf_counter() + 0.2

            started = time.perf_counter()
            server._drain_all_pending(out)
            elapsed = time.perf_counter() - started

            assert elapsed < 3
            responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
            assert len(responses) == 1
            assert responses[0]["error"]["message"] == "Tool execution timed out"
            assert call["cancel_event"].is_set()
            with server._pending_lock:
                assert server._pending_calls == []
        finally:
            release.set()

    def test_serve_expires_stalled_call_before_returning(self):
        """Fluxo real de serve(): tool travada expira e o cliente recebe erro."""
        release = threading.Event()
        executor = _make_executor()
        executor.config.mcp_tool_timeout_seconds = 1.0

        def _blocking_execute(call, progress=None):
            release.wait(10)
            return ToolResult(ok=True, tool_name="read_file", content="late")

        executor.execute.side_effect = _blocking_execute
        server = _make_server(executor)

        try:
            started = time.perf_counter()
            responses = _exchange(server, {
                "jsonrpc": "2.0", "id": 14, "method": "tools/call",
                "params": {"name": "read_file", "arguments": {}},
            })
            elapsed = time.perf_counter() - started

            assert elapsed < 5
            assert len(responses) == 1
            assert responses[0]["id"] == 14
            assert responses[0]["error"]["message"] == "Tool execution timed out"
            with server._pending_lock:
                assert server._pending_calls == []
        finally:
            release.set()


class TestHighThroughput:
    """Testa muitas requisições sequenciais."""

    def test_ten_pings(self):
        """10 pings sequenciais sem perda de respostas."""
        server = _make_server()
        msgs = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(10)]
        responses = _exchange(server, *msgs)
        assert len(responses) == 10
        ids = {r["id"] for r in responses}
        assert ids == set(range(10))

    def test_fifty_pings(self):
        """50 pings sequenciais sem perda de respostas."""
        server = _make_server()
        msgs = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(50)]
        responses = _exchange(server, *msgs)
        assert len(responses) == 50
        ids = {r["id"] for r in responses}
        assert ids == set(range(50))


class TestErrorIsolation:
    """Erro em uma tool não afeta chamadas subsequentes."""

    def test_error_isolation(self):
        """Tool que levanta exceção não afeta tool seguinte."""
        executor = _make_executor()
        call_count = 0

        def _execute(tool_call, progress_cb=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first tool failed")
            return ToolResult(ok=True, tool_name="read_file", content="second ok")

        executor.execute.side_effect = _execute
        server = _make_server(executor)

        responses = _exchange(
            server,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "read_file", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "read_file", "arguments": {}}},
        )

        assert len(responses) == 2
        assert "error" in responses[0]
        assert responses[0]["error"]["code"] == -32603
        assert "result" in responses[1]
        assert responses[1]["result"]["content"][0]["text"] == "second ok"


class TestProtocolVersion:
    """Testa negociação de protocol version (initialize)."""

    def test_accepts_any_client_version(self):
        """Initialize com qualquer versão de cliente é aceito; servidor retorna a sua versão."""
        server = _make_server()
        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        })
        assert "result" in resp, f"esperado result, obteve: {resp}"
        assert resp["result"]["protocolVersion"] == MCPServer.PROTOCOL_VERSION

    def test_accepts_older_client_version(self):
        """Initialize com versão mais antiga também é aceito."""
        server = _make_server()
        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-01-01"},
        })
        assert "result" in resp
        assert resp["result"]["protocolVersion"] == MCPServer.PROTOCOL_VERSION


class TestDuplicateRequestID:
    """Requisições com ID duplicado devem ser rejeitadas."""

    def test_duplicate_id_returns_error(self):
        """Segunda requisição com mesmo ID retorna erro."""
        server = _make_server()
        responses = _exchange(
            server,
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert len(responses) == 2
        ok_count = sum(1 for r in responses if "result" in r)
        err_count = sum(1 for r in responses if "error" in r)
        assert ok_count == 1
        assert err_count == 1


class TestBatchHTTP:
    """Testa requisições batch via HTTP POST /message."""

    def test_batch_http_returns_array(self):
        """POST /message com array JSON retorna array de respostas."""
        mcp = _make_server()
        httpd = MCP_HTTPServer(mcp, host="127.0.0.1", port=0)
        httpd.start_background()
        try:
            for _ in range(50):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(0.5)
                    s.connect((httpd.host, httpd.port))
                    s.close()
                    break
                except (OSError, ConnectionRefusedError):
                    time.sleep(0.05)

            body = json.dumps([
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            ]).encode("utf-8")

            conn = http.client.HTTPConnection(httpd.host, httpd.port, timeout=10)
            try:
                conn.request("POST", "/message", body=body, headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                data = json.loads(resp.read())
                assert resp.status == 200
                assert isinstance(data, list)
                assert len(data) == 2
                ids = {r["id"] for r in data}
                assert ids == {1, 2}
            finally:
                conn.close()
        finally:
            httpd.shutdown()


def test_same_request_id_is_isolated_per_connection():
    server = _make_server(_make_executor())
    out_a = io.StringIO()
    out_b = io.StringIO()
    call_a = {"msg_id": 1, "out": out_a}
    call_b = {"msg_id": 1, "out": out_b}
    call_a["request_key"] = server._request_key(out_a, 1)
    call_b["request_key"] = server._request_key(out_b, 1)
    server._pending_calls.extend([call_a, call_b])

    server._discard_pending_call(call_a)

    assert server._pending_calls == [call_b]

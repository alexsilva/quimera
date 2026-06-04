from __future__ import annotations

import io
import threading
import time
from unittest.mock import MagicMock

from quimera.runtime.approval_broker import ApprovalScope, RiskLevel
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.mcp import MCPServer
from quimera.runtime.models import ToolCall, ToolResult


def test_call_agent_internal_auto_approved_with_budget(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    dispatch = MagicMock(return_value="ok")
    executor.set_call_agent_fn(dispatch)

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={"agent_name": "codex", "task": "x", "approval_budget": 1},
            metadata={"transport": "internal_mcp", "run_id": "run-1", "agent_name": "claude"},
        )
    )

    assert result.ok is True
    approval.approve.assert_not_called()
    assert executor.approval_broker.audit_log[-1]["event"] == "auto_approved"
    assert executor.approval_broker.audit_log[-1]["run_id"] == "run-1"


def test_call_agent_http_external_requires_approval(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.set_call_agent_fn(MagicMock(return_value="ok"))

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={"agent_name": "codex", "task": "x"},
            metadata={"transport": "http_mcp", "run_id": "external-run"},
        )
    )

    assert result.ok is False
    approval.approve.assert_called_once()
    assert "risco: delegation" in approval.approve.call_args.kwargs["summary"]
    assert executor.approval_broker.audit_log[-1]["event"] == "denied"


def test_mcp_http_transport_metadata_reaches_executor():
    result = ToolResult(ok=True, tool_name="call_agent", content="denied")
    executor = MagicMock()
    executor.registry.names.return_value = ["call_agent"]
    executor.execute.return_value = result
    server = MCPServer(executor)
    out = io.StringIO()

    server._process_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "call_agent", "arguments": {"agent_name": "codex", "task": "x"}},
        },
        out,
        transport="http_mcp",
    )
    server._drain_all_pending(out)

    call = executor.execute.call_args.args[0]
    assert call.metadata["transport"] == "http_mcp"


def test_apply_patch_concurrent_same_file_is_serialized(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    intervals = []
    guard = threading.Lock()

    def handler(call):
        start = time.monotonic()
        time.sleep(0.05)
        end = time.monotonic()
        with guard:
            intervals.append((start, end))
        return ToolResult(ok=True, tool_name=call.name, content="ok")

    executor.registry.register("apply_patch", handler)
    patch = "*** Begin Patch\n--- a/a.txt\n+++ b/a.txt\n@@\n-old\n+new\n*** End Patch"
    calls = [ToolCall(name="apply_patch", arguments={"patch": patch}) for _ in range(2)]
    threads = [threading.Thread(target=executor.execute, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(intervals) == 2
    first, second = sorted(intervals)
    assert first[1] <= second[0]


def test_run_shell_concurrent_same_workspace_is_serialized(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    intervals = []
    guard = threading.Lock()

    def handler(call):
        start = time.monotonic()
        time.sleep(0.05)
        end = time.monotonic()
        with guard:
            intervals.append((start, end))
        return ToolResult(ok=True, tool_name=call.name, content="ok")

    executor.registry.register("run_shell", handler)
    calls = [ToolCall(name="run_shell", arguments={"command": "echo ok"}) for _ in range(2)]
    threads = [threading.Thread(target=executor.execute, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(intervals) == 2
    first, second = sorted(intervals)
    assert first[1] <= second[0]


def test_approval_scope_expires(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.approval_broker.approve_scope(
        ApprovalScope(
            id="short",
            run_id="run-1",
            tool_name="write_file",
            risk=RiskLevel.WRITE,
            expires_at=time.time() - 0.01,
        )
    )

    result = executor.execute(
        ToolCall(
            name="write_file",
            arguments={"path": "new.txt", "content": "x"},
            metadata={"run_id": "run-1"},
        )
    )

    assert result.ok is False
    approval.approve.assert_called_once()


def test_approve_all_scope_does_not_leak_to_another_run(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.approval_broker.approve_scope(
        ApprovalScope(id="run-only", run_id="run-1", approve_all_in_run=True)
    )

    ok = executor.execute(
        ToolCall(
            name="write_file",
            arguments={"path": "a.txt", "content": "x"},
            metadata={"run_id": "run-1"},
        )
    )
    denied = executor.execute(
        ToolCall(
            name="write_file",
            arguments={"path": "b.txt", "content": "x"},
            metadata={"run_id": "run-2"},
        )
    )

    assert ok.ok is True
    assert denied.ok is False
    approval.approve.assert_called_once()


def test_read_tool_inside_workspace_has_no_prompt(tmp_path):
    target = tmp_path / "x.txt"
    target.write_text("ok")
    approval = MagicMock()
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)

    result = executor.execute(ToolCall(name="read_file", arguments={"path": "x.txt"}))

    assert result.ok is True
    approval.approve.assert_not_called()


def test_dangerous_command_still_blocked(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)

    result = executor.execute(ToolCall(name="run_shell", arguments={"command": "sudo rm -rf /"}))

    assert result.ok is False
    assert result.error_type == "policy"
    approval.approve.assert_not_called()


def test_git_push_requires_strong_confirmation_and_is_blocked_in_mcp_shell(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)

    result = executor.execute(ToolCall(name="run_shell", arguments={"command": "git push"}))

    assert result.ok is False
    assert "git push" in str(result.error)
    approval.approve.assert_not_called()

from __future__ import annotations

import io
import threading
import time
from unittest.mock import MagicMock

from quimera.runtime.approval_broker import (
    ApprovalScope,
    RiskLevel,
    TrustedToolExecutionContext,
)
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.mcp import MCPServer
from quimera.runtime.models import ToolCall, ToolResult


def _trusted(**overrides):
    values = {
        "transport": "internal_mcp",
        "run_id": "run-1",
        "agent_name": "claude",
        "delegation_budget": 8,
    }
    values.update(overrides)
    return {"trusted_context": TrustedToolExecutionContext(**values)}


def test_call_agent_internal_auto_approved_with_server_side_budget(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    dispatch = MagicMock(return_value="ok")
    executor.set_call_agent_fn(dispatch)

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={"agent_name": "codex", "task": "x"},
            metadata=_trusted(delegation_budget=1),
        )
    )

    assert result.ok is True
    approval.approve.assert_not_called()
    assert executor.approval_broker.audit_log[-1]["event"] == "auto_approved"
    assert executor.approval_broker.audit_log[-1]["run_id"] == "run-1"


def test_call_agent_http_external_requires_user_approval(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.set_call_agent_fn(MagicMock(return_value="ok"))

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={"agent_name": "codex", "task": "x"},
            metadata=_trusted(transport="http_mcp", run_id="external-run"),
        )
    )

    assert result.ok is False
    approval.approve.assert_called_once()
    assert "risco: delegation" in approval.approve.call_args.kwargs["summary"]
    assert executor.approval_broker.audit_log[-1]["event"] == "denied"


def test_call_agent_http_external_requires_approval_even_with_allowlisted_argument(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.set_call_agent_fn(MagicMock(return_value="ok"))

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={"agent_name": "codex", "task": "x", "allowlisted": True},
            metadata=_trusted(transport="http_mcp", run_id="external-run"),
        )
    )

    assert result.ok is False
    # Reserved field validation rejects before any user approval prompt.
    approval.approve.assert_not_called()
    assert "reservados" in str(result.error)


def test_http_mcp_cannot_spoof_internal_transport_via_meta():
    executor = MagicMock()
    executor.registry.names.return_value = ["call_agent"]
    executor.execute.return_value = ToolResult(ok=True, tool_name="call_agent", content="ok")
    server = MCPServer(executor)
    out = io.StringIO()

    server._process_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "call_agent",
                "arguments": {"agent_name": "codex", "task": "x"},
                "_meta": {"transport": "internal_mcp", "run_id": "evil", "approval_scope_id": "evil"},
            },
        },
        out,
        transport="http_mcp",
    )
    server._drain_all_pending(out)

    call = executor.execute.call_args.args[0]
    trusted = call.metadata["trusted_context"]
    assert trusted.transport == "http_mcp"
    assert trusted.run_id != "evil"
    assert trusted.approval_scope_id is None


def test_http_mcp_allowlisted_argument_does_not_bypass_approval(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.set_call_agent_fn(MagicMock(return_value="ok"))

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={"agent_name": "codex", "task": "x", "allowlisted": True},
            metadata=_trusted(transport="http_mcp", run_id="external-run"),
        )
    )

    assert result.ok is False
    assert "reservados" in str(result.error)
    approval.approve.assert_not_called()


def test_caller_cannot_increase_approval_budget(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    config = ToolRuntimeConfig(workspace_root=tmp_path, delegation_budget_per_run=1)
    executor = ToolExecutor(config, approval)
    executor.set_call_agent_fn(MagicMock(return_value="ok"))

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={"agent_name": "codex", "task": "x", "approval_budget": 1000},
            metadata=_trusted(delegation_budget=1),
        )
    )

    assert result.ok is False
    assert "approval_budget" in str(result.error)
    approval.approve.assert_not_called()


def test_caller_cannot_pass_approval_scope_id_argument(tmp_path):
    approval = MagicMock()
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.set_call_agent_fn(MagicMock(return_value="ok"))

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={"agent_name": "codex", "task": "x", "approval_scope_id": "evil"},
        )
    )

    assert result.ok is False
    assert "approval_scope_id" in str(result.error)
    approval.approve.assert_not_called()


def test_delegation_budget_is_consumed_atomically_for_parallel_calls(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    config = ToolRuntimeConfig(workspace_root=tmp_path, delegation_budget_per_run=1)
    executor = ToolExecutor(config, approval)
    executor.set_call_agent_fn(MagicMock(return_value="ok"))
    barrier = threading.Barrier(6)
    results = []
    guard = threading.Lock()

    def run_call():
        barrier.wait()
        result = executor.execute(
            ToolCall(
                name="call_agent",
                arguments={"agent_name": "codex", "task": "x"},
                metadata=_trusted(delegation_budget=1),
            )
        )
        with guard:
            results.append(result.ok)

    threads = [threading.Thread(target=run_call) for _ in range(5)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 4


def test_approval_scope_remaining_uses_is_consumed_atomically(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    target_path = str((tmp_path / "scoped.txt").resolve())
    executor.registry.register(
        "write_file",
        lambda call: ToolResult(ok=True, tool_name=call.name, content="ok"),
    )
    executor.approval_broker.approve_scope(
        ApprovalScope(
            id="one-shot",
            run_id="run-1",
            tool_name="write_file",
            path=target_path,
            risk=RiskLevel.WRITE,
            expires_at=time.time() + 60,
            remaining_uses=1,
        )
    )
    barrier = threading.Barrier(3)
    results = []
    guard = threading.Lock()

    def run_call():
        barrier.wait()
        result = executor.execute(
            ToolCall(
                name="write_file",
                arguments={"path": "scoped.txt", "content": "x"},
                metadata=_trusted(run_id="run-1"),
            )
        )
        with guard:
            results.append(result.ok)

    threads = [threading.Thread(target=run_call) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 1


def test_apply_patch_concurrent_same_file_is_serialized_with_real_quimera_patch(tmp_path):
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
    patch = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch"
    calls = [ToolCall(name="apply_patch", arguments={"patch": patch}) for _ in range(2)]
    threads = [threading.Thread(target=executor.execute, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(intervals) == 2
    first, second = sorted(intervals)
    assert first[1] <= second[0]


def test_apply_patch_multi_file_lock_key_is_deterministic(tmp_path):
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    patch_a = """*** Begin Patch
*** Update File: b.txt
@@
-old
+new
*** Move to: c.txt
*** Add File: a.txt
+x
*** Delete File: d.txt
*** End Patch"""
    patch_b = """*** Begin Patch
*** Delete File: d.txt
*** Add File: a.txt
+x
*** Update File: b.txt
*** Move to: c.txt
@@
-old
+new
*** End Patch"""

    key_a = executor.approval_broker._serialization_key(ToolCall(name="apply_patch", arguments={"patch": patch_a}))
    key_b = executor.approval_broker._serialization_key(ToolCall(name="apply_patch", arguments={"patch": patch_b}))

    assert key_a == key_b
    assert str((tmp_path / "a.txt").resolve()) in key_a
    assert str((tmp_path / "b.txt").resolve()) in key_a
    assert str((tmp_path / "c.txt").resolve()) in key_a
    assert str((tmp_path / "d.txt").resolve()) in key_a


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
    target_path = str((tmp_path / "new.txt").resolve())
    try:
        executor.approval_broker.approve_scope(
            ApprovalScope(
                id="short",
                run_id="run-1",
                tool_name="write_file",
                path=target_path,
                risk=RiskLevel.WRITE,
                expires_at=time.time() - 0.01,
                remaining_uses=1,
            )
        )
    except ValueError:
        pass

    result = executor.execute(
        ToolCall(
            name="write_file",
            arguments={"path": "new.txt", "content": "x"},
            metadata=_trusted(run_id="run-1"),
        )
    )

    assert result.ok is False
    approval.approve.assert_called_once()


def test_approve_all_scope_does_not_leak_to_another_run(tmp_path):
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.registry.register(
        "run_shell",
        lambda call: ToolResult(ok=True, tool_name=call.name, content="ok"),
    )
    executor.approval_broker.approve_scope(
        ApprovalScope(
            id="run-only",
            run_id="run-1",
            risk=RiskLevel.SHELL,
            expires_at=time.time() + 60,
            remaining_uses=1,
            approve_all_in_run=True,
        )
    )

    ok = executor.execute(
        ToolCall(
            name="run_shell",
            arguments={"command": "echo ok"},
            metadata=_trusted(run_id="run-1"),
        )
    )
    denied = executor.execute(
        ToolCall(
            name="run_shell",
            arguments={"command": "echo ok"},
            metadata=_trusted(run_id="run-2"),
        )
    )

    assert ok.ok is True
    assert denied.ok is False
    approval.approve.assert_called_once()


def test_point_approval_does_not_create_broad_scope_for_later_mutations(tmp_path):
    approval = MagicMock()
    approval.approve.side_effect = [True, False, False, False]
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.registry.register(
        "write_file",
        lambda call: ToolResult(ok=True, tool_name=call.name, content="ok"),
    )
    executor.registry.register(
        "apply_patch",
        lambda call: ToolResult(ok=True, tool_name=call.name, content="ok"),
    )
    executor.registry.register(
        "run_shell",
        lambda call: ToolResult(ok=True, tool_name=call.name, content="ok"),
    )

    first = executor.execute(ToolCall(name="write_file", arguments={"path": "a.txt", "content": "x"}))
    second = executor.execute(ToolCall(name="write_file", arguments={"path": "b.txt", "content": "x"}))
    third = executor.execute(ToolCall(name="apply_patch", arguments={"patch": "*** Begin Patch\n*** Add File: c.txt\n+x\n*** End Patch"}))
    fourth = executor.execute(ToolCall(name="run_shell", arguments={"command": "echo ok"}))

    assert first.ok is True
    assert second.ok is False
    assert third.ok is False
    assert fourth.ok is False
    assert approval.approve.call_count == 4


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


def test_call_agent_rejects_all_reserved_fields(tmp_path):
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    executor.set_call_agent_fn(MagicMock(return_value="ok"))

    for field in ("allowlisted", "approval_budget", "approval_scope_id", "transport", "run_id", "parent_run_id"):
        result = executor.execute(
            ToolCall(
                name="call_agent",
                arguments={"agent_name": "codex", "task": "x", field: "evil"},
            )
        )
        assert result.ok is False
        assert field in str(result.error)

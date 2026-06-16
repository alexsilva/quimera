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


def _patch_for_paths(*paths: str) -> str:
    sections = []
    for path in paths:
        sections.append(f"*** Update File: {path}\n@@\n-old\n+new")
    return "*** Begin Patch\n" + "\n".join(sections) + "\n*** End Patch"


def _register_timed_handlers(executor, *tool_names: str, sleep_seconds: float = 0.05):
    active = 0
    active_lock = threading.Lock()
    intervals = []
    overlap_seen = threading.Event()

    def handler(call):
        nonlocal active
        start = time.monotonic()
        with active_lock:
            active += 1
            if active > 1:
                overlap_seen.set()
        time.sleep(sleep_seconds)
        end = time.monotonic()
        with active_lock:
            active -= 1
            intervals.append((call.name, start, end))
        return ToolResult(ok=True, tool_name=call.name, content="ok")

    for tool_name in tool_names:
        executor.registry.register(tool_name, handler)
    return intervals, overlap_seen


def _execute_concurrently(executor, calls: list[ToolCall]) -> list[ToolResult]:
    barrier = threading.Barrier(len(calls) + 1)
    results: list[ToolResult] = []
    results_lock = threading.Lock()

    def run(call: ToolCall) -> None:
        barrier.wait(timeout=2)
        result = executor.execute(call)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
    return results


def test_delegate_internal_auto_approved_with_server_side_budget(tmp_path):
    """Verifica que Test call agent internal auto approved with server side budget."""
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    dispatch = MagicMock(return_value="ok")
    executor.set_delegate_fn(dispatch)

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={"target_agent": "codex", "request": "x"},
            metadata=_trusted(delegation_budget=1),
        )
    )

    assert result.ok is True
    approval.approve.assert_not_called()
    assert executor.approval_broker.audit_log[-1]["event"] == "auto_approved"
    assert executor.approval_broker.audit_log[-1]["run_id"] == "run-1"


def test_delegate_http_external_requires_user_approval(tmp_path):
    """Verifica que Test call agent http external requires user approval."""
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.set_delegate_fn(MagicMock(return_value="ok"))

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={"target_agent": "codex", "request": "x"},
            metadata=_trusted(transport="http_mcp", run_id="external-run"),
        )
    )

    assert result.ok is False
    approval.approve.assert_called_once()
    assert "risco: delegation" in approval.approve.call_args.kwargs["summary"]
    assert executor.approval_broker.audit_log[-1]["event"] == "denied"


def test_exec_command_approval_summary_does_not_duplicate_command(tmp_path):
    """Summary de approval não deve repetir o comando já destacado pelo broker."""
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)

    result = executor.execute(
        ToolCall(
            name="exec_command",
            arguments={"cmd": "pwd", "tty": True},
            metadata=_trusted(transport="http_mcp", run_id="external-run"),
        )
    )

    assert result.ok is False
    summary = approval.approve.call_args.kwargs["summary"]
    assert "comando: pwd" in summary
    assert summary.count("comando: pwd") == 1
    assert "flags: tty" in summary


def test_delegate_http_external_requires_approval_even_with_allowlisted_argument(tmp_path):
    """Verifica que Test call agent http external requires approval even with allowlisted argument."""
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.set_delegate_fn(MagicMock(return_value="ok"))

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={"target_agent": "codex", "request": "x", "allowlisted": True},
            metadata=_trusted(transport="http_mcp", run_id="external-run"),
        )
    )

    assert result.ok is False
    # Reserved field validation rejects before any user approval prompt.
    approval.approve.assert_not_called()
    assert "reservados" in str(result.error)


def test_http_mcp_cannot_spoof_internal_transport_via_meta():
    """Verifica que Test http mcp cannot spoof internal transport via meta."""
    executor = MagicMock()
    executor.registry.names.return_value = ["delegate"]
    executor.execute.return_value = ToolResult(ok=True, tool_name="delegate", content="ok")
    server = MCPServer(executor)
    out = io.StringIO()

    server._process_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "delegate",
                "arguments": {"target_agent": "codex", "request": "x"},
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
    """Verifica que Test http mcp allowlisted argument does not bypass approval."""
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.set_delegate_fn(MagicMock(return_value="ok"))

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={"target_agent": "codex", "request": "x", "allowlisted": True},
            metadata=_trusted(transport="http_mcp", run_id="external-run"),
        )
    )

    assert result.ok is False
    assert "reservados" in str(result.error)
    approval.approve.assert_not_called()


def test_caller_cannot_increase_approval_budget(tmp_path):
    """Verifica que Test caller cannot increase approval budget."""
    approval = MagicMock()
    approval.approve.return_value = False
    config = ToolRuntimeConfig(workspace_root=tmp_path, delegation_budget_per_run=1)
    executor = ToolExecutor(config, approval)
    executor.set_delegate_fn(MagicMock(return_value="ok"))

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={"target_agent": "codex", "request": "x", "approval_budget": 1000},
            metadata=_trusted(delegation_budget=1),
        )
    )

    assert result.ok is False
    assert "approval_budget" in str(result.error)
    approval.approve.assert_not_called()


def test_caller_cannot_pass_approval_scope_id_argument(tmp_path):
    """Verifica que Test caller cannot pass approval scope id argument."""
    approval = MagicMock()
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.set_delegate_fn(MagicMock(return_value="ok"))

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={"target_agent": "codex", "request": "x", "approval_scope_id": "evil"},
        )
    )

    assert result.ok is False
    assert "approval_scope_id" in str(result.error)
    approval.approve.assert_not_called()


def test_delegation_budget_is_consumed_atomically_for_parallel_calls(tmp_path):
    """Verifica que Test delegation budget is consumed atomically for parallel calls."""
    approval = MagicMock()
    approval.approve.return_value = False
    config = ToolRuntimeConfig(workspace_root=tmp_path, delegation_budget_per_run=1)
    executor = ToolExecutor(config, approval)
    executor.set_delegate_fn(MagicMock(return_value="ok"))
    barrier = threading.Barrier(6)
    results = []
    guard = threading.Lock()

    def run_call():
        barrier.wait()
        result = executor.execute(
            ToolCall(
                name="delegate",
                arguments={"target_agent": "codex", "request": "x"},
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
    """Verifica que Test approval scope remaining uses is consumed atomically."""
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
            transport="internal_mcp",
            server_origin="tool_executor",
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
    """Verifica que Test apply patch concurrent same file is serialized with real quimera patch."""
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    intervals, overlap_seen = _register_timed_handlers(executor, "apply_patch")

    patch = _patch_for_paths("a.txt")
    results = _execute_concurrently(
        executor,
        [ToolCall(name="apply_patch", arguments={"patch": patch}) for _ in range(2)],
    )

    assert all(result.ok for result in results)
    assert len(intervals) == 2
    first, second = sorted((start, end) for _, start, end in intervals)
    assert first[1] <= second[0]
    assert not overlap_seen.is_set()


def test_apply_patch_multi_file_lock_keys_are_deterministic_per_path(tmp_path):
    """Verifica que Test apply patch multi file lock keys are deterministic per path."""
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

    keys_a = executor.approval_broker._serialization_keys(ToolCall(name="apply_patch", arguments={"patch": patch_a}))
    keys_b = executor.approval_broker._serialization_keys(ToolCall(name="apply_patch", arguments={"patch": patch_b}))

    expected = [
        f"path:{(tmp_path / name).resolve()}"
        for name in ("a.txt", "b.txt", "c.txt", "d.txt")
    ]
    assert keys_a == expected
    assert keys_b == expected
    assert executor.approval_broker._serialization_key(
        ToolCall(name="apply_patch", arguments={"patch": patch_a})
    ) == "|".join(expected)


def test_apply_patch_overlapping_multi_file_patch_is_serialized(tmp_path):
    """Verifica que Test apply patch overlapping multi file patch is serialized."""
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    intervals, overlap_seen = _register_timed_handlers(executor, "apply_patch")

    results = _execute_concurrently(
        executor,
        [
            ToolCall(name="apply_patch", arguments={"patch": _patch_for_paths("a.txt", "b.txt")}),
            ToolCall(name="apply_patch", arguments={"patch": _patch_for_paths("b.txt", "c.txt")}),
        ],
    )

    assert all(result.ok for result in results)
    assert len(intervals) == 2
    first, second = sorted((start, end) for _, start, end in intervals)
    assert first[1] <= second[0]
    assert not overlap_seen.is_set()


def test_apply_patch_multi_file_is_serialized_with_write_file_on_same_path(tmp_path):
    """Verifica que Test apply patch multi file is serialized with write file on same path."""
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    intervals, overlap_seen = _register_timed_handlers(executor, "apply_patch", "write_file")

    results = _execute_concurrently(
        executor,
        [
            ToolCall(name="apply_patch", arguments={"patch": _patch_for_paths("a.txt", "b.txt")}),
            ToolCall(name="write_file", arguments={"path": "a.txt", "content": "x"}),
        ],
    )

    assert all(result.ok for result in results)
    assert len(intervals) == 2
    first, second = sorted((start, end) for _, start, end in intervals)
    assert first[1] <= second[0]
    assert not overlap_seen.is_set()


def test_apply_patch_multi_file_is_serialized_with_remove_file_on_same_path(tmp_path):
    """Verifica que Test apply patch multi file is serialized with remove file on same path."""
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    intervals, overlap_seen = _register_timed_handlers(executor, "apply_patch", "remove_file")

    results = _execute_concurrently(
        executor,
        [
            ToolCall(name="apply_patch", arguments={"patch": _patch_for_paths("a.txt", "b.txt")}),
            ToolCall(name="remove_file", arguments={"path": "b.txt", "dry_run": False}),
        ],
    )

    assert all(result.ok for result in results)
    assert len(intervals) == 2
    first, second = sorted((start, end) for _, start, end in intervals)
    assert first[1] <= second[0]
    assert not overlap_seen.is_set()


def test_apply_patch_multi_file_can_run_parallel_with_disjoint_write_file(tmp_path):
    """Verifica que Test apply patch multi file can run parallel with disjoint write file."""
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    intervals, overlap_seen = _register_timed_handlers(executor, "apply_patch", "write_file")

    results = _execute_concurrently(
        executor,
        [
            ToolCall(name="apply_patch", arguments={"patch": _patch_for_paths("a.txt", "b.txt")}),
            ToolCall(name="write_file", arguments={"path": "c.txt", "content": "x"}),
        ],
    )

    assert all(result.ok for result in results)
    assert len(intervals) == 2
    assert overlap_seen.is_set()


def test_multi_path_lock_acquisition_is_deadlock_free_with_reversed_patch_order(tmp_path):
    """Verifica que Test multi path lock acquisition is deadlock free with reversed patch order."""
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    _register_timed_handlers(executor, "apply_patch")

    results = _execute_concurrently(
        executor,
        [
            ToolCall(name="apply_patch", arguments={"patch": _patch_for_paths("a.txt", "b.txt")}),
            ToolCall(name="apply_patch", arguments={"patch": _patch_for_paths("b.txt", "a.txt")}),
        ],
    )

    assert len(results) == 2
    assert all(result.ok for result in results)


def test_run_shell_concurrent_same_workspace_is_serialized(tmp_path):
    """Verifica que Test run shell concurrent same workspace is serialized."""
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
    """Verifica que Test approval scope expires."""
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    target_path = str((tmp_path / "new.txt").resolve())
    try:
        executor.approval_broker.approve_scope(
            ApprovalScope(
                id="short",
                run_id="run-1",
                transport="internal_mcp",
                server_origin="tool_executor",
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
    """Verifica que Test approve all scope does not leak to another run."""
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
            transport="internal_mcp",
            server_origin="tool_executor",
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
    """Verifica que Test point approval does not create broad scope for later mutations."""
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
    """Verifica que Test read tool inside workspace has no prompt."""
    target = tmp_path / "x.txt"
    target.write_text("ok")
    approval = MagicMock()
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)

    result = executor.execute(ToolCall(name="read_file", arguments={"path": "x.txt"}))

    assert result.ok is True
    approval.approve.assert_not_called()


def test_dangerous_command_still_blocked(tmp_path):
    """Verifica que Test dangerous command still blocked."""
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)

    result = executor.execute(ToolCall(name="run_shell", arguments={"command": "sudo rm -rf /"}))

    assert result.ok is False
    assert result.error_type == "policy"
    approval.approve.assert_not_called()


def test_git_push_requires_strong_confirmation_and_is_blocked_in_mcp_shell(tmp_path):
    """Verifica que Test git push requires strong confirmation and is blocked in mcp shell."""
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)

    result = executor.execute(ToolCall(name="run_shell", arguments={"command": "git push"}))

    assert result.ok is False
    assert "git push" in str(result.error)
    approval.approve.assert_not_called()


def test_delegate_rejects_all_reserved_fields(tmp_path):
    """Verifica que Test call agent rejects all reserved fields."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    executor.set_delegate_fn(MagicMock(return_value="ok"))

    for field in ("allowlisted", "approval_budget", "approval_scope_id", "transport", "run_id", "parent_run_id"):
        result = executor.execute(
            ToolCall(
                name="delegate",
                arguments={"target_agent": "codex", "request": "x", field: "evil"},
            )
        )
        assert result.ok is False
        assert field in str(result.error)


def test_write_stdin_same_session_is_serialized(tmp_path):
    """Verifica que Test write stdin same session is serialized."""
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
            intervals.append((call.name, start, end))
        return ToolResult(ok=True, tool_name=call.name, content="ok")

    executor.registry.register("write_stdin", handler)
    calls = [ToolCall(name="write_stdin", arguments={"session_id": 7, "chars": "x"}) for _ in range(2)]
    threads = [threading.Thread(target=executor.execute, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(intervals) == 2
    first, second = sorted((start, end) for _, start, end in intervals)
    assert first[1] <= second[0]


def test_close_command_session_does_not_run_parallel_with_write_stdin(tmp_path):
    """Verifica que Test close command session does not run parallel with write stdin."""
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
            intervals.append((call.name, start, end))
        return ToolResult(ok=True, tool_name=call.name, content="ok")

    executor.registry.register("write_stdin", handler)
    executor.registry.register("close_command_session", handler)
    calls = [
        ToolCall(name="write_stdin", arguments={"session_id": 9, "chars": "x"}),
        ToolCall(name="close_command_session", arguments={"session_id": 9}),
    ]
    threads = [threading.Thread(target=executor.execute, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(intervals) == 2
    first, second = sorted((start, end) for _, start, end in intervals)
    assert first[1] <= second[0]


def test_delegate_scope_limits_caller_and_target_agent(tmp_path):
    """Verifica que Test call agent scope limits caller and target agent."""
    approval = MagicMock()
    approval.approve.return_value = False
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.set_delegate_fn(MagicMock(return_value="ok"))
    executor.approval_broker.approve_scope(
        ApprovalScope(
            id="claude-to-codex",
            run_id="run-1",
            transport="internal_mcp",
            server_origin="tool_executor",
            tool_name="delegate",
            agent_name="claude",
            target_agent_name="codex",
            risk=RiskLevel.DELEGATION,
            expires_at=time.time() + 60,
            remaining_uses=1,
        )
    )

    codex = executor.execute(
        ToolCall(
            name="delegate",
            arguments={"target_agent": "codex", "request": "x"},
            metadata=_trusted(run_id="run-1", agent_name="claude", delegation_budget=0),
        )
    )
    gemini = executor.execute(
        ToolCall(
            name="delegate",
            arguments={"target_agent": "gemini", "request": "x"},
            metadata=_trusted(run_id="run-1", agent_name="claude", delegation_budget=0),
        )
    )

    assert codex.ok is True
    assert gemini.ok is False
    approval.approve.assert_called_once()

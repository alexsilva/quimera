from unittest.mock import MagicMock, patch

from quimera.runtime.process_supervisor import ManagedProcess, ProcessSupervisor


def _managed_process(pid: int, scope_id: str) -> ManagedProcess:
    proc = MagicMock()
    proc.pid = pid
    return ManagedProcess(
        proc=proc,
        pid=pid,
        pgid=pid,
        owner="test-agent",
        scope_id=scope_id,
    )


def test_terminate_scope_selects_only_owned_processes():
    supervisor = ProcessSupervisor()
    parent = _managed_process(1001, "parent")
    delegate = _managed_process(1002, "delegate")
    supervisor._processes = {parent.pid: parent, delegate.pid: delegate}

    with patch.object(supervisor, "_terminate_snapshot") as terminate_snapshot:
        supervisor.terminate_scope("delegate")

    terminate_snapshot.assert_called_once_with([delegate], clear_registry=True)


def test_terminate_scope_ignores_unknown_scope():
    supervisor = ProcessSupervisor()
    parent = _managed_process(1001, "parent")
    supervisor._processes = {parent.pid: parent}

    with patch.object(supervisor, "_terminate_snapshot") as terminate_snapshot:
        supervisor.terminate_scope("missing")

    terminate_snapshot.assert_not_called()

import time
from unittest.mock import MagicMock, patch

import pytest

from quimera.runtime.task_executor import TaskExecutor, create_executor


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "tasks.db"


def test_task_executor_init_error():
    with pytest.raises(ValueError, match="db_path is required"):
        TaskExecutor("agent", None)


def test_task_executor_start_stop(db_path):
    # Line 33, 39-41 coverage
    executor = TaskExecutor("agent", db_path)
    executor.start()
    assert executor._running is True
    executor.start()  # Should return immediately
    executor.stop()
    assert executor._running is False


@patch("quimera.runtime.task_executor.claim_task")
@patch("quimera.runtime.task_executor.list_tasks")
def test_task_executor_poll_loop(mock_list, mock_claim, db_path):
    # Line 48-51 coverage
    executor = TaskExecutor("agent", db_path, poll_interval=0.1)
    mock_handler = MagicMock(return_value=True)
    executor.set_handler(mock_handler)

    mock_claim.side_effect = [1, None]  # Claim one, then None
    mock_list.return_value = [{"id": 1, "description": "test"}]

    executor.start()
    time.sleep(0.3)
    executor.stop()

    mock_handler.assert_called_with({"id": 1, "description": "test"})


@patch("quimera.runtime.task_executor.claim_task")
@patch("quimera.runtime.task_executor.list_tasks")
def test_task_executor_process_pending(mock_list, mock_claim, db_path):
    # Line 60-68 coverage
    executor = TaskExecutor("agent", db_path)
    mock_handler = MagicMock(return_value=True)
    executor.set_handler(mock_handler)

    # Case 1: No task
    mock_claim.return_value = None
    assert executor.process_pending() is None

    # Case 2: Task found
    mock_claim.return_value = 1
    mock_list.return_value = [{"id": 1}]
    assert executor.process_pending() == 1
    mock_handler.assert_called_with({"id": 1})


def test_create_executor(db_path):
    handler = lambda x: True
    executor = create_executor("agent", handler, db_path)
    assert executor.agent_name == "agent"
    assert executor._handler == handler


def test_task_executor_stop_ignores_keyboard_interrupt(db_path):
    executor = TaskExecutor("agent", db_path)
    mock_thread = MagicMock()
    mock_thread.join.side_effect = KeyboardInterrupt()
    executor._thread = mock_thread

    executor.stop()

    assert executor._running is False
    mock_thread.join.assert_called_once_with(timeout=5)

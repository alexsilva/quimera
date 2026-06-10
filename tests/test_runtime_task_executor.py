import time
from unittest.mock import MagicMock

import pytest

from quimera.runtime.models import TaskRecord
from quimera.runtime.task_executor import TaskExecutor, create_executor


class RepositoryStub:
    def __init__(self):
        self.claim_sequence = []
        self.claim_review_sequence = []
        self.tasks_by_id = {}
        self.failed = []

    def claim_task(self, _agent_name, job_id=None):
        if not self.claim_sequence:
            return None
        return self.claim_sequence.pop(0)

    def claim_review_task(self, _agent_name, job_id=None):
        if not self.claim_review_sequence:
            return None
        return self.claim_review_sequence.pop(0)

    def list_tasks(self, filt=None):
        task_id = (filt or {}).get("id")
        if task_id is None:
            return []
        task = self.tasks_by_id.get(task_id)
        return [task] if task else []

    def fail_task(self, task_id, reason=None):
        self.failed.append((task_id, reason))
        return True


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "tasks.db"


@pytest.fixture
def repository():
    return RepositoryStub()


def test_task_executor_init_error():
    """Verifica que o executor levanta erro quando repository é None."""
    with pytest.raises(ValueError, match="repository is required"):
        TaskExecutor("agent", None)


def test_task_executor_start_stop(db_path, repository):
    """Verifica que o executor inicia e para corretamente."""
    executor = TaskExecutor("agent", db_path, repository=repository)
    executor.start()
    assert executor._running is True
    executor.start()
    executor.stop()
    assert executor._running is False


def test_task_executor_poll_loop(db_path, repository):
    """Verifica que o executor faz polling e processa tasks."""
    executor = TaskExecutor("agent", db_path, poll_interval=0.1, repository=repository)
    mock_handler = MagicMock(return_value=True)
    executor.set_handler(mock_handler)

    task = TaskRecord(id=1, job_id=0, description="test", status="in_progress")
    repository.tasks_by_id[1] = task
    repository.claim_sequence = [1, None]

    executor.start()
    time.sleep(0.3)
    executor.stop()

    mock_handler.assert_called_with(task)


def test_task_executor_process_pending(db_path, repository):
    """Verifica que o executor processa tasks pendentes."""
    executor = TaskExecutor("agent", db_path, repository=repository)
    mock_handler = MagicMock(return_value=True)
    executor.set_handler(mock_handler)

    assert executor.process_pending() is None

    task = TaskRecord(id=1, job_id=0, description="", status="in_progress")
    repository.tasks_by_id[1] = task
    repository.claim_sequence = [1]
    assert executor.process_pending() == 1
    mock_handler.assert_called_with(task)


def test_create_executor(db_path, repository):
    """Verifica que create_executor cria um executor corretamente."""
    handler = lambda x: True
    executor = create_executor("agent", handler, db_path, repository=repository)
    assert executor.agent_name == "agent"
    assert executor._handler == handler


def test_create_executor_requires_repository(db_path):
    """Verifica que create_executor exige repository."""
    with pytest.raises(ValueError, match="repository is required"):
        create_executor("agent", lambda _task: True, db_path)


def test_task_executor_stop_ignores_keyboard_interrupt(db_path, repository):
    """Verifica que o executor ignora KeyboardInterrupt ao parar."""
    executor = TaskExecutor("agent", db_path, repository=repository)
    mock_thread = MagicMock()
    mock_thread.join.side_effect = KeyboardInterrupt()
    executor._thread = mock_thread

    executor.stop()

    assert executor._running is False
    mock_thread.join.assert_called_once_with(timeout=5)


def test_task_executor_stop_interrupts_long_poll_interval(db_path, repository):
    """Verifica que o executor interrompe polling longo ao parar."""
    executor = TaskExecutor("agent", db_path, poll_interval=60, repository=repository)

    executor.start()
    time.sleep(0.05)

    started_at = time.monotonic()
    executor.stop()

    assert time.monotonic() - started_at < 1

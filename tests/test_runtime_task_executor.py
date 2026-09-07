import time
import threading
from unittest.mock import MagicMock

import pytest

from quimera.constants import TaskStatus
from quimera.runtime.models import TaskRecord
from quimera.tasks.executor import TaskExecutor, create_executor


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


def test_task_executor_skips_task_assigned_to_other_agent(db_path, repository):
    """Executor não deve rodar handler quando a task carregada pertence a outro agente."""
    executor = TaskExecutor("agent", db_path, repository=repository)
    mock_handler = MagicMock(return_value=True)
    executor.set_handler(mock_handler)

    task = TaskRecord(id=1, job_id=0, description="", status=TaskStatus.IN_PROGRESS, assigned_to="other")
    repository.tasks_by_id[1] = task
    repository.claim_sequence = [1]

    assert executor.process_pending() is None
    mock_handler.assert_not_called()
    assert repository.failed == []


def test_task_executor_skips_task_not_in_progress(db_path, repository):
    """Executor não deve rodar handler para task stale fora de in_progress."""
    executor = TaskExecutor("agent", db_path, repository=repository)
    mock_handler = MagicMock(return_value=True)
    executor.set_handler(mock_handler)

    task = TaskRecord(id=1, job_id=0, description="", status=TaskStatus.PENDING, assigned_to="agent")
    repository.tasks_by_id[1] = task
    repository.claim_sequence = [1]

    assert executor.process_pending() is None
    mock_handler.assert_not_called()
    assert repository.failed == []


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


@pytest.mark.parametrize("workers", [0, -1, True, 1.5])
def test_invalid_worker_capacity_is_rejected(repository, workers):
    with pytest.raises(ValueError, match="max_workers"):
        TaskExecutor("agent", repository=repository, max_workers=workers)


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_invalid_poll_interval_is_rejected(repository, interval):
    with pytest.raises(ValueError, match="poll_interval"):
        TaskExecutor("agent", repository=repository, poll_interval=interval)


def test_claims_wait_for_free_worker_and_stop_preserves_pending_work(repository):
    started = threading.Event()
    release = threading.Event()
    repository.claim_sequence = [1, 2, 3]
    repository.tasks_by_id = {
        i: TaskRecord(id=i, job_id=0, description="", status=TaskStatus.IN_PROGRESS, assigned_to="agent")
        for i in range(1, 4)
    }
    executor = TaskExecutor("agent", max_workers=1, poll_interval=60, repository=repository)

    def handler(task):
        started.set()
        assert release.wait(5)
        return True

    executor.set_handler(handler)
    executor.start()
    try:
        assert started.wait(3)
        assert executor.process_pending() is None
        assert repository.claim_sequence == [2, 3]
        executor.stop()
        assert executor.process_pending() is None
        assert repository.claim_sequence == [2, 3]
        with pytest.raises(RuntimeError, match="workers are still running"):
            executor.start()
    finally:
        release.set()
        executor.stop()
        executor._executor.shutdown(wait=True)


def test_completed_worker_wakes_poller_without_waiting_poll_interval(repository):
    first_started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    repository.claim_sequence = [1, 2]
    repository.tasks_by_id = {
        i: TaskRecord(id=i, job_id=0, description="", status=TaskStatus.IN_PROGRESS)
        for i in (1, 2)
    }
    executor = TaskExecutor("agent", max_workers=1, poll_interval=60, repository=repository)

    def handler(task):
        if task.id == 1:
            first_started.set()
            assert release.wait(5)
        else:
            second_started.set()
        return True

    executor.set_handler(handler)
    executor.start()
    try:
        assert first_started.wait(3)
        release.set()
        assert second_started.wait(3)
    finally:
        release.set()
        executor.stop()
        executor._executor.shutdown(wait=True)
    executor.start()
    executor.stop()


@pytest.mark.parametrize("review", [False, True])
def test_handler_exception_fails_owned_task(repository, review):
    task = TaskRecord(id=1, job_id=0, description="",
                      status=TaskStatus.REVIEWING if review else TaskStatus.IN_PROGRESS,
                      assigned_to="author" if review else "agent", reviewed_by="agent" if review else None)
    repository.tasks_by_id[1] = task
    executor = TaskExecutor("agent", repository=repository)
    handler = MagicMock(side_effect=RuntimeError("handler crashed"))
    if review:
        repository.claim_review_sequence = [1]
        executor.set_review_handler(handler)
    else:
        repository.claim_sequence = [1]
        executor.set_handler(handler)
    assert executor._process_next(include_reviews=review) == 1
    assert repository.failed == [(1, "handler crashed")]


@pytest.mark.parametrize("status,owner,reviewer", [
    (TaskStatus.COMPLETED, "agent", None),
    (TaskStatus.IN_PROGRESS, "other", None),
    (TaskStatus.REVIEWING, "agent", "other"),
])
def test_handler_exception_does_not_fail_completed_or_reassigned_task(repository, status, owner, reviewer):
    task = TaskRecord(id=1, job_id=0, description="", status=TaskStatus.IN_PROGRESS, assigned_to="agent")
    repository.tasks_by_id[1] = task
    repository.claim_sequence = [1]
    executor = TaskExecutor("agent", repository=repository)

    def handler(_task):
        repository.tasks_by_id[1] = TaskRecord(id=1, job_id=0, description="", status=status,
                                               assigned_to=owner, reviewed_by=reviewer)
        raise RuntimeError("late error")

    executor.set_handler(handler)
    assert executor.process_pending() == 1
    assert repository.failed == []


def test_manual_polling_respects_gate_and_requires_handler(repository):
    repository.claim_sequence = [1]
    executor = TaskExecutor("agent", repository=repository)
    assert executor.process_pending() is None
    executor.set_handler(MagicMock())
    executor.set_claim_gate(lambda: False)
    assert executor.process_pending() is None
    assert repository.claim_sequence == [1]

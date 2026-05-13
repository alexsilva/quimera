"""
Task executor for Stage 5 - autonomous task consumption and execution.
"""
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from .models import TaskRecord

_logger = logging.getLogger("quimera.task_executor")


class TaskExecutor:
    """Implementa `TaskExecutor`."""

    def __init__(
        self,
        agent_name: str,
        db_path=None,
        max_workers: int = 2,
        poll_interval: float = 5.0,
        job_id=None,
        repository: Any = None,
    ):
        """Inicializa uma instância de TaskExecutor.

        ``TaskExecutor`` sempre opera via objeto de repositório explícito.
        """
        if repository is None:
            raise ValueError("repository is required")
        self.agent_name = agent_name
        self.db_path = db_path
        self._repository = repository
        self.job_id = job_id
        self.max_workers = max_workers
        self.poll_interval = poll_interval
        self._running = False
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._task_queue: queue.Queue = queue.Queue()
        self._handler: Optional[Callable] = None
        self._review_handler: Optional[Callable] = None
        self._review_eligibility: Optional[Callable[[], bool]] = None

    def set_handler(self, handler: Callable[[TaskRecord], bool]):
        """Set the task execution handler. Handler receives TaskRecord and returns True on success."""
        self._handler = handler

    def set_review_handler(self, handler: Callable[[TaskRecord], bool]):
        """Set the review handler. Called with tasks in 'pending_review' state from other agents."""
        self._review_handler = handler

    def set_review_eligibility(self, predicate: Callable[[], bool]):
        """Set a dynamic predicate that decides whether this agent may claim review work."""
        self._review_eligibility = predicate

    def start(self):
        """Executa start."""
        if self._running:
            return
        self._running = True
        self._wake_event.clear()
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix=f"task-{self.agent_name}")
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Executa stop."""
        self._running = False
        self._wake_event.set()
        if self._executor:
            self._executor.shutdown(wait=False)
        if self._thread:
            try:
                self._thread.join(timeout=5)
            except KeyboardInterrupt:
                _logger.debug("task executor stop interrupted for agent=%s", self.agent_name)

    def _claim_task(self) -> Optional[int]:
        return self._repository.claim_task(self.agent_name, job_id=self.job_id)

    def _claim_review_task(self) -> Optional[int]:
        return self._repository.claim_review_task(self.agent_name, job_id=self.job_id)

    def _load_task(self, task_id: int) -> Optional[TaskRecord]:
        tasks = self._repository.list_tasks({"id": task_id})
        return tasks[0] if tasks else None

    def _fail_task(self, task_id: int, reason: str) -> None:
        self._repository.fail_task(task_id, reason=reason)

    def _poll_loop(self):
        """Executa poll loop — tasks despachadas em paralelo via ThreadPoolExecutor quando ativo."""
        while self._running:
            task_id = None
            try:
                task_id = self._claim_task()
                if task_id:
                    task = self._load_task(task_id)
                    if task and self._handler:
                        if self._executor is not None:
                            self._executor.submit(self._handler, task)
                        else:
                            self._handler(task)
                    else:
                        self._fail_task(task_id, "handler unavailable or task not found")
                        _logger.warning("task %s claimed by %s but could not be dispatched", task_id, self.agent_name)
                    task_id = None
                    if self._wait_or_stop(1):
                        break
                    continue
                can_review = self._review_eligibility() if self._review_eligibility else True
                if self._review_handler and can_review:
                    review_id = self._claim_review_task()
                    if review_id:
                        task = self._load_task(review_id)
                        if task:
                            self._review_handler(task)
                        else:
                            self._fail_task(review_id, "review task not found")
                            _logger.warning(
                                "review task %s claimed by %s but could not be loaded",
                                review_id,
                                self.agent_name,
                            )
                        if self._wait_or_stop(1):
                            break
                        continue
                if self._wait_or_stop(self.poll_interval):
                    break
            except Exception as exc:
                _logger.exception("poll loop error agent=%s task_id=%s: %s", self.agent_name, task_id, exc)
                if task_id:
                    try:
                        self._fail_task(task_id, str(exc))
                    except Exception:
                        pass
                if self._wait_or_stop(self.poll_interval):
                    break

    def _wait_or_stop(self, timeout: float) -> bool:
        """Wait for timeout unless stop() wakes the poll loop."""
        self._wake_event.wait(timeout)
        if self._wake_event.is_set():
            self._wake_event.clear()
        return not self._running

    def wake(self):
        """Wake the poll loop to check for new tasks immediately."""
        self._wake_event.set()

    def process_pending(self):
        """Process one iteration of pending tasks (for manual/batch execution)."""
        task_id = self._claim_task()
        if not task_id:
            return None
        task = self._load_task(task_id)
        if task and self._handler:
            self._handler(task)
            # Note: handler already calls complete_task/fail_task with result
        return task_id


def create_executor(
    agent_name: str,
    handler: Callable[[TaskRecord], bool],
    db_path=None,
    job_id=None,
    repository=None,
) -> TaskExecutor:
    """Factory function to create and configure a task executor."""
    if repository is None:
        raise ValueError("repository is required")
    executor = TaskExecutor(agent_name, db_path=db_path, job_id=job_id, repository=repository)
    executor.set_handler(handler)
    return executor

"""
Task executor for Stage 5 - autonomous task consumption and execution.
"""
import logging
import threading
import time
import queue
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .tasks import list_tasks, claim_task, complete_task, fail_task, get_conn, claim_review_task

_logger = logging.getLogger("quimera.task_executor")


class TaskExecutor:
    """Implementa `TaskExecutor`."""
    def __init__(self, agent_name: str, db_path, max_workers: int = 2, poll_interval: float = 5.0, job_id=None):
        """Inicializa uma instância de TaskExecutor."""
        if not db_path:
            raise ValueError("db_path is required — use workspace.tasks_db")
        self.agent_name = agent_name
        self.db_path = db_path
        self.job_id = job_id
        self.max_workers = max_workers
        self.poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._task_queue: queue.Queue = queue.Queue()
        self._handler: Optional[Callable] = None
        self._review_handler: Optional[Callable] = None
        self._review_eligibility: Optional[Callable[[], bool]] = None

    def set_handler(self, handler: Callable[[dict], bool]):
        """Set the task execution handler. Handler receives task dict and returns True on success."""
        self._handler = handler

    def set_review_handler(self, handler: Callable[[dict], bool]):
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
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Executa stop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
    
    def _poll_loop(self):
        """Executa poll loop."""
        while self._running:
            task_id = None
            try:
                task_id = claim_task(self.agent_name, job_id=self.job_id, db_path=self.db_path)
                if task_id:
                    tasks = list_tasks({"id": task_id}, db_path=self.db_path)
                    if tasks and self._handler:
                        self._handler(tasks[0])
                    else:
                        fail_task(task_id, reason="handler unavailable or task not found", db_path=self.db_path)
                        _logger.warning("task %s claimed by %s but could not be dispatched", task_id, self.agent_name)
                    task_id = None
                    # Brief pause after execution so other agents can pick up requeued tasks
                    time.sleep(1)
                    continue
                # No regular task — check for review tasks from other agents
                can_review = self._review_eligibility() if self._review_eligibility else True
                if self._review_handler and can_review:
                    review_id = claim_review_task(self.agent_name, job_id=self.job_id, db_path=self.db_path)
                    if review_id:
                        tasks = list_tasks({"id": review_id}, db_path=self.db_path)
                        if tasks:
                            self._review_handler(tasks[0])
                        else:
                            fail_task(review_id, reason="review task not found", db_path=self.db_path)
                            _logger.warning(
                                "review task %s claimed by %s but could not be loaded",
                                review_id,
                                self.agent_name,
                            )
                        time.sleep(1)
                        continue
                time.sleep(self.poll_interval)
            except Exception as exc:
                _logger.exception("poll loop error agent=%s task_id=%s: %s", self.agent_name, task_id, exc)
                if task_id:
                    try:
                        fail_task(task_id, reason=str(exc), db_path=self.db_path)
                    except Exception:
                        pass
                time.sleep(self.poll_interval)
    
    def process_pending(self):
        """Process one iteration of pending tasks (for manual/batch execution)."""
        task_id = claim_task(self.agent_name, job_id=self.job_id, db_path=self.db_path)
        if not task_id:
            return None
        tasks = list_tasks({"id": task_id}, db_path=self.db_path)
        if tasks and self._handler:
            task = tasks[0]
            success = self._handler(task)
            # Note: handler already calls complete_task/fail_task with result
        return task_id


def create_executor(agent_name: str, handler: Callable[[dict], bool], db_path, job_id=None) -> TaskExecutor:
    """Factory function to create and configure a task executor."""
    executor = TaskExecutor(agent_name, db_path=db_path, job_id=job_id)
    executor.set_handler(handler)
    return executor

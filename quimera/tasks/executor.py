"""
Task executor for Stage 5 - autonomous task consumption and execution.
"""
import logging
import math
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

from ..constants import TaskStatus
from ..runtime.models import TaskRecord

_logger = logging.getLogger("quimera.task_executor")


class TaskExecutor:
    """Executa tasks em background com polling e pool de workers."""

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
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be finite and positive")
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
        self._claim_lock = threading.RLock()
        self._futures_lock = threading.RLock()
        self._futures: set[Future] = set()
        self._stopped = False
        self._handler: Optional[Callable] = None
        self._review_handler: Optional[Callable] = None
        self._review_eligibility: Optional[Callable[[], bool]] = None
        self._claim_gate: Optional[Callable[[], bool]] = None

    def set_claim_gate(self, gate: Callable[[], bool]) -> None:
        """Define predicado que controla quando o executor pode reivindicar tasks."""
        self._claim_gate = gate

    def set_handler(self, handler: Callable[[TaskRecord], bool]):
        """Define o handler de execução que processa cada task reivindicada."""
        self._handler = handler

    def set_review_handler(self, handler: Callable[[TaskRecord], bool]):
        """Define o handler de review para tasks de outros agentes."""
        self._review_handler = handler

    def set_review_eligibility(self, predicate: Callable[[], bool]):
        """Define predicado dinâmico que decide se o agente pode fazer review."""
        self._review_eligibility = predicate

    def start(self):
        """Inicia o loop de polling e o pool de workers em background."""
        with self._claim_lock:
            if self._running:
                return
            with self._futures_lock:
                if self._futures:
                    raise RuntimeError("previous task workers are still running")
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("previous task poller is still running")
            self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix=f"task-{self.agent_name}")
            self._running = True
            self._stopped = False
            self._wake_event.clear()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()

    def stop(self):
        """Interrompe o loop de polling e finaliza o pool de workers."""
        with self._claim_lock:
            self._running = False
            self._stopped = True
            self._wake_event.set()
            if self._executor:
                self._executor.shutdown(wait=False)
        if self._thread and self._thread is not threading.current_thread():
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

    def _can_execute_task(self, task: TaskRecord) -> bool:
        if task.status != TaskStatus.IN_PROGRESS:
            _logger.warning(
                "task %s claimed by %s has invalid status=%s; skipping dispatch",
                task.id,
                self.agent_name,
                task.status,
            )
            return False
        if task.assigned_to and task.assigned_to != self.agent_name:
            _logger.warning(
                "task %s belongs to %s but executor %s loaded it; skipping dispatch",
                task.id,
                task.assigned_to,
                self.agent_name,
            )
            return False
        return True

    def _dispatch_task(self, task: TaskRecord) -> bool:
        if not self._handler or not self._can_execute_task(task):
            return False
        self._submit_handler(self._handler, task)
        return True

    def _submit_handler(self, handler: Callable, task: TaskRecord) -> None:
        if self._executor is None:
            self._run_handler(handler, task)
            return
        with self._futures_lock:
            future = self._executor.submit(self._run_handler, handler, task)
            self._futures.add(future)
            future.add_done_callback(self._worker_done)

    def _run_handler(self, handler: Callable, task: TaskRecord) -> bool:
        try:
            return handler(task)
        except Exception as exc:
            _logger.exception("task handler failed agent=%s task_id=%s", self.agent_name, task.id)
            current = self._load_task(task.id)
            if current and (
                (current.status == TaskStatus.IN_PROGRESS
                 and current.assigned_to in {None, "", self.agent_name})
                or (current.status == TaskStatus.REVIEWING
                    and current.reviewed_by == self.agent_name)
            ):
                self._fail_task(task.id, str(exc))
            return False

    def _worker_done(self, future: Future) -> None:
        with self._futures_lock:
            self._futures.discard(future)
        try:
            future.result()
        except Exception:
            _logger.exception("task worker recovery failed agent=%s", self.agent_name)
        self.wake()

    def _process_next(self, *, include_reviews: bool = False) -> Optional[int]:
        # Keep the capacity check, claim and submission together so manual polling
        # cannot race the background poller and reserve work beyond worker capacity.
        with self._claim_lock:
            if self._stopped or (self._claim_gate is not None and not self._claim_gate()):
                return None
            with self._futures_lock:
                if len(self._futures) >= self.max_workers:
                    return None
            task_id = None
            try:
                if self._handler:
                    task_id = self._claim_task()
                    if task_id:
                        task = self._load_task(task_id)
                        if task is None:
                            self._fail_task(task_id, "task not found")
                        elif self._dispatch_task(task):
                            return task_id
                        return None
                if (include_reviews and self._review_handler
                        and (self._review_eligibility is None or self._review_eligibility())):
                    task_id = self._claim_review_task()
                    if task_id:
                        task = self._load_task(task_id)
                        if task is None:
                            self._fail_task(task_id, "review task not found")
                        elif task.status == TaskStatus.REVIEWING and task.reviewed_by == self.agent_name:
                            self._submit_handler(self._review_handler, task)
                            return task_id
                return None
            except Exception as exc:
                if task_id:
                    self._fail_task(task_id, str(exc))
                raise

    def _poll_loop(self):
        """Executa poll loop — tasks despachadas em paralelo via ThreadPoolExecutor quando ativo."""
        while self._running:
            try:
                if self._process_next(include_reviews=True) is not None:
                    continue
            except Exception:
                _logger.exception("poll loop error agent=%s", self.agent_name)
            if self._wait_or_stop(self.poll_interval):
                break

    def _wait_or_stop(self, timeout: float) -> bool:
        """Wait for timeout unless stop() wakes the poll loop."""
        self._wake_event.wait(timeout)
        if self._wake_event.is_set():
            self._wake_event.clear()
        return not self._running

    def wake(self):
        """Acorda o loop de polling para verificar novas tasks imediatamente."""
        self._wake_event.set()

    def process_pending(self):
        """Processa uma iteração manual de tasks pendentes (execução em lote)."""
        return self._process_next()


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

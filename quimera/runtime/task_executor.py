"""
Task executor for Stage 5 - autonomous task consumption and execution.
"""
import threading
import time
import queue
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .tasks import list_tasks, claim_task, complete_task, fail_task, get_conn


class TaskExecutor:
    def __init__(self, agent_name: str, db_path, max_workers: int = 2, poll_interval: float = 5.0):
        if not db_path:
            raise ValueError("db_path is required — use workspace.tasks_db")
        self.agent_name = agent_name
        self.db_path = db_path
        self.max_workers = max_workers
        self.poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._task_queue: queue.Queue = queue.Queue()
        self._handler: Optional[Callable] = None
    
    def set_handler(self, handler: Callable[[dict], bool]):
        """Set the task execution handler. Handler receives task dict and returns True on success."""
        self._handler = handler
    
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
    
    def _poll_loop(self):
        while self._running:
            try:
                task_id = claim_task(self.agent_name, db_path=self.db_path)
                if task_id:
                    tasks = list_tasks({"id": task_id}, db_path=self.db_path)
                    if tasks and self._handler:
                        task = tasks[0]
                        success = self._handler(task)
                        # Note: handler already calls complete_task/fail_task with result
                else:
                    time.sleep(self.poll_interval)
            except Exception:
                time.sleep(self.poll_interval)
    
    def process_pending(self):
        """Process one iteration of pending tasks (for manual/batch execution)."""
        task_id = claim_task(self.agent_name, db_path=self.db_path)
        if not task_id:
            return None
        tasks = list_tasks({"id": task_id}, db_path=self.db_path)
        if tasks and self._handler:
            task = tasks[0]
            success = self._handler(task)
            # Note: handler already calls complete_task/fail_task with result
        return task_id


def create_executor(agent_name: str, handler: Callable[[dict], bool], db_path) -> TaskExecutor:
    """Factory function to create and configure a task executor."""
    executor = TaskExecutor(agent_name, db_path=db_path)
    executor.set_handler(handler)
    return executor
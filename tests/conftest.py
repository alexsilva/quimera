import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quimera.runtime.task_executor import TaskExecutor


@pytest.fixture(autouse=True)
def cleanup_task_executors(monkeypatch):
    executors = []
    original_init = TaskExecutor.__init__

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        executors.append(self)

    monkeypatch.setattr(TaskExecutor, "__init__", tracked_init)
    yield
    for executor in reversed(executors):
        executor.stop()

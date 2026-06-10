import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quimera.runtime.task_executor import TaskExecutor


@pytest.fixture(autouse=True)
def cleanup_task_executors(monkeypatch):
    """Verifica que os executores de tarefa são limpos após cada teste."""
    executors = []
    original_init = TaskExecutor.__init__

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        executors.append(self)

    monkeypatch.setattr(TaskExecutor, "__init__", tracked_init)
    yield
    for executor in reversed(executors):
        executor.stop()


@pytest.fixture(autouse=True)
def cleanup_env_vars(monkeypatch):
    """Verifica que a variável de ambiente QUIMERA_CURRENT_JOB_ID é removida após cada teste."""
    monkeypatch.delenv("QUIMERA_CURRENT_JOB_ID", raising=False)
    yield


@pytest.fixture(autouse=True)
def reset_stdout_stderr(monkeypatch):
    """Verifica que sys.stdout e sys.stderr são restaurados após cada teste."""
    import sys
    import io
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    yield
    if sys.stdout is not real_stdout:
        sys.stdout = real_stdout
    if sys.stderr is not real_stderr:
        sys.stderr = real_stderr


@pytest.fixture(autouse=True)
def reset_builtins_print(monkeypatch):
    """Verifica que a função builtins.print é restaurada após cada teste."""
    import builtins
    real_print = builtins.print
    yield
    builtins.print = real_print


@pytest.fixture(autouse=True)
def bypass_cli_runtime_dependency_check(monkeypatch):
    """Mantém testes existentes independentes das dependências instaladas no ambiente."""
    try:
        import quimera.cli as cli
    except Exception:
        yield
        return
    monkeypatch.setattr(cli, "_ensure_required_runtime_dependencies", lambda: None)
    yield

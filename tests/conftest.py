import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Impede qualquer teste (inclusive unittest.TestCase) de escrever em ~/.local.
# CANDIDATE_DIRS é uma lista mutável compartilhada por referência em todos os módulos
# que fazem `from quimera.paths import CANDIDATE_DIRS`, então a mutação in-place é suficiente.
import quimera.paths as _quimera_paths
_quimera_paths.CANDIDATE_DIRS[:] = [_quimera_paths.TMP_BASE_DIR]

from quimera.tasks.executor import TaskExecutor


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


@pytest.fixture(autouse=True)
def redirect_workspace_base_to_tmp(monkeypatch, tmp_path):
    """Redireciona find_base_writable e TMP_BASE_DIR para diretórios descartáveis em todos os testes.

    Evita que criações de Workspace escrevam em ~/.local/share/quimera ou em
    /tmp/quimera durante testes. Os arquivos de find_base_writable ficam em
    /tmp/pytest-* e são removidos automaticamente pelo pytest. TMP_BASE_DIR usa
    um diretório próprio e curto (fora da árvore pytest-of-*) porque caminhos de
    socket AF_UNIX derivados dele têm limite de ~108 bytes.
    """
    tmp_base = tmp_path / "quimera_base"
    tmp_base.mkdir(exist_ok=True)

    tmp_workspace_tmp = Path(tempfile.mkdtemp(prefix="qtmp-"))

    import quimera.workspace as _ws
    monkeypatch.setattr(_ws, "find_base_writable", lambda _candidates: tmp_base)
    monkeypatch.setattr(_ws, "TMP_BASE_DIR", tmp_workspace_tmp)

    try:
        import quimera.profiles.base as _pb
        monkeypatch.setattr(_pb, "find_base_writable", lambda _candidates: tmp_base)
    except Exception:
        pass

    try:
        import quimera.runtime.drivers.repl as _repl
        monkeypatch.setattr(_repl, "find_base_writable", lambda _candidates: tmp_base)
    except Exception:
        pass

    yield
    shutil.rmtree(tmp_workspace_tmp, ignore_errors=True)

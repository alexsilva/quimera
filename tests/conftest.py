import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Impede qualquer teste (inclusive unittest.TestCase) de escrever em ~/.local.
# CANDIDATE_DIRS é uma lista mutável compartilhada por referência em todos os módulos
# que fazem `from quimera.paths import CANDIDATE_DIRS`, então a mutação in-place é suficiente.
import quimera.paths as _quimera_paths  # noqa: E402
_quimera_paths.CANDIDATE_DIRS[:] = [_quimera_paths.TMP_BASE_DIR]

from quimera.runtime.config import ToolRuntimeConfig  # noqa: E402
from quimera.workspace import Workspace  # noqa: E402
from quimera.runtime.tools.shell import ShellToolValidator  # noqa: E402
from quimera.tasks.executor import TaskExecutor  # noqa: E402
from quimera.runtime.executor import ToolExecutor  # noqa: E402
from quimera.runtime.approval import AutoApprovalHandler  # noqa: E402
from quimera.runtime.tools.files import FileTools  # noqa: E402
from quimera.tasks.repository import TaskRepository  # noqa: E402
from quimera.tasks import api as tasks_api  # noqa: E402
from tests.helpers import make_policy  # noqa: E402


# ════════════════════════════════════════════════════════════════════════
# Fixtures básicos de mock/renderer
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def renderer():
    """Renderer básico (MagicMock) para testes de UI, agentes e contexto."""
    return MagicMock()


@pytest.fixture
def mock_handler():
    """ApprovalHandler auto-aprovador para testes que não querem interação."""
    return AutoApprovalHandler()


# ════════════════════════════════════════════════════════════════════════
# Fixtures de configuração
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def config():
    """ToolRuntimeConfig padrão com workspace_root em /tmp."""
    return ToolRuntimeConfig(workspace=Workspace(Path("/tmp")))


@pytest.fixture
def config_with_workspace(tmp_path):
    """ToolRuntimeConfig com workspace_root em tmp_path isolado."""
    return ToolRuntimeConfig(workspace=Workspace(tmp_path))


@pytest.fixture
def config_with_approval(tmp_path):
    """ToolRuntimeConfig com aprovação para mutações habilitada."""
    return ToolRuntimeConfig(
        workspace=Workspace(tmp_path),
        require_approval_for_mutations=True,
    )


@pytest.fixture
def config_no_approval(tmp_path):
    """ToolRuntimeConfig sem aprovação para mutações."""
    return ToolRuntimeConfig(
        workspace=Workspace(tmp_path),
        require_approval_for_mutations=False,
    )


# ════════════════════════════════════════════════════════════════════════
# Fixtures de policy e validators
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def policy(config):
    """ToolPolicy com todos os validators registrados (via tests.helpers)."""
    return make_policy(config)


@pytest.fixture
def policy_with_workspace(config_with_workspace):
    """ToolPolicy com validators para workspace isolado."""
    return make_policy(config_with_workspace)


@pytest.fixture
def shell_validator(config):
    """ShellToolValidator configurado com o workspace_root padrão."""
    return ShellToolValidator(config)


@pytest.fixture
def shell_validator_with_workspace(config_with_workspace):
    """ShellToolValidator com workspace isolado."""
    return ShellToolValidator(config_with_workspace)


# ════════════════════════════════════════════════════════════════════════
# Fixtures de banco de dados de tasks
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tasks_db_path(tmp_path):
    """Banco SQLite de tasks inicializado em um diretório descartável."""
    db_path = tmp_path / "tasks.db"
    tasks_api.init_db(str(db_path))
    return str(db_path)


# ════════════════════════════════════════════════════════════════════════
# Fixtures de executor e tools
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def approval_handler():
    """Handler de aprovação mockável (MagicMock) para controlar approve()."""
    return MagicMock()


@pytest.fixture
def executor(config, mock_handler):
    """ToolExecutor padrão auto-aprovado para testes de ferramentas."""
    return ToolExecutor(config, mock_handler)


@pytest.fixture
def executor_with_approval(config_with_approval):
    """ToolExecutor com aprovação habilitada (usa MagicMock para controle)."""
    from unittest.mock import MagicMock
    return ToolExecutor(config_with_approval, MagicMock())


@pytest.fixture
def executor_no_approval(config_no_approval):
    """ToolExecutor sem aprovação para mutações (usa MagicMock para controle)."""
    from unittest.mock import MagicMock
    return ToolExecutor(config_no_approval, MagicMock())


@pytest.fixture
def executor_with_workspace(config_with_workspace, mock_handler):
    """ToolExecutor com workspace isolado."""
    return ToolExecutor(config_with_workspace, mock_handler)


@pytest.fixture
def file_tools(config):
    """FileTools configurado com config padrão."""
    return FileTools(config)


# ════════════════════════════════════════════════════════════════════════
# Fixtures de repositório de tasks
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def task_repository(tmp_path):
    """TaskRepository com banco isolado em tmp_path."""
    db_path = tmp_path / "task_repository.db"
    return TaskRepository(str(db_path))


@pytest.fixture
def task_repository_with_sink(tmp_path):
    """TaskRepository com EventSink para testes de eventos."""
    from quimera.app.event_sink import EventSink
    db_path = tmp_path / "task_repository_sink.db"
    sink = EventSink()
    return TaskRepository(str(db_path), event_sink=sink), sink


# ════════════════════════════════════════════════════════════════════════
# Fixtures autouse existentes (limpeza e isolamento)
# ════════════════════════════════════════════════════════════════════════

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
    tmp_base = Path(tempfile.mkdtemp(prefix="qbase-"))
    tmp_workspace_tmp = Path(tempfile.mkdtemp(prefix="qtmp-"))

    import quimera.workspace as _ws
    monkeypatch.setattr(_ws, "find_base_writable", lambda _candidates: tmp_base)
    import quimera.session_paths as _session_paths
    monkeypatch.setattr(_session_paths, "TMP_BASE_DIR", tmp_workspace_tmp)

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
    shutil.rmtree(tmp_base, ignore_errors=True)
    shutil.rmtree(tmp_workspace_tmp, ignore_errors=True)

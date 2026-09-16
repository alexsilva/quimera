from pathlib import Path

from quimera.runtime.config import ToolRuntimeConfig
from quimera.session_paths import SessionPaths
from quimera.workspace import Workspace


def test_config_post_init_default_read_roots():
    """O workspace atual entra nos roots efetivos sem ser copiado para a config."""
    root = Path("/tmp").resolve()
    workspace = Workspace(root)
    config = ToolRuntimeConfig(workspace=workspace)
    assert config.allowed_read_roots == []
    assert config.read_roots() == (workspace.cwd,)


def test_config_post_init_custom_read_roots(tmp_path):
    """Roots extras complementam o workspace e o diretório de artefatos."""
    root = tmp_path / "project"
    root.mkdir()
    custom = Path("/home/alex").resolve()
    workspace = Workspace(root)
    session_paths = SessionPaths(workspace)
    config = ToolRuntimeConfig(
        workspace=workspace,
        session_paths=session_paths,
        allowed_read_roots=[custom],
    )
    assert config.allowed_read_roots == [custom]
    assert config.read_roots() == (workspace.cwd, custom, session_paths.artifacts_dir)


def test_workspace_paths_are_not_duplicated_in_runtime_config():
    root = Path("/tmp").resolve()
    workspace = Workspace(root)
    config = ToolRuntimeConfig(workspace=workspace)

    assert config.workspace is workspace
    assert not hasattr(config, "workspace_root")
    assert not hasattr(config, "db_path")
    assert not hasattr(config, "memory_file")

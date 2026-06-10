from pathlib import Path

from quimera.runtime.config import ToolRuntimeConfig


def test_config_post_init_default_read_roots():
    """Verifica que allowed_read_roots padrão contém apenas workspace_root."""
    root = Path("/tmp").resolve()
    config = ToolRuntimeConfig(workspace_root=root)
    assert config.allowed_read_roots == [root]


def test_config_post_init_custom_read_roots():
    """Verifica que allowed_read_roots personalizado substitui o padrão."""
    # Line 53 coverage
    root = Path("/tmp").resolve()
    custom = Path("/home/alex").resolve()
    config = ToolRuntimeConfig(workspace_root=root, allowed_read_roots=[custom])
    assert config.allowed_read_roots == [custom]

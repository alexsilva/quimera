from pathlib import Path

from quimera.runtime.config import ToolRuntimeConfig


def test_config_post_init_default_read_roots():
    root = Path("/tmp").resolve()
    config = ToolRuntimeConfig(workspace_root=root)
    assert config.allowed_read_roots == [root]


def test_config_post_init_custom_read_roots():
    # Line 53 coverage
    root = Path("/tmp").resolve()
    custom = Path("/home/alex").resolve()
    config = ToolRuntimeConfig(workspace_root=root, allowed_read_roots=[custom])
    assert config.allowed_read_roots == [custom]

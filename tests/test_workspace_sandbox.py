"""Integração do estado persistido com os wrappers de subprocesso."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from quimera.config import ConfigManager
from quimera.app.core import QuimeraApp
from quimera.sandbox.bwrap import SandboxError
from quimera.sandbox.state import is_sandbox_enabled, wrap_subprocess_cmd
from quimera.workspace import Workspace


def test_sandbox_state_is_persisted_per_workspace(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = Workspace(first_root)
    second = Workspace(second_root)

    ConfigManager(first.workspace_config_file).set_sandbox_enabled(True)

    assert is_sandbox_enabled(first) is True
    assert is_sandbox_enabled(second) is False
    assert "sandbox_enabled" not in ConfigManager(first.config_file)._load()


def test_wrapper_preserves_current_behavior_when_sandbox_is_off(tmp_path):
    workspace = Workspace(tmp_path)
    command = ["echo", "ok"]

    with patch(
        "quimera.sandbox.state.build_secret_mask_cmd",
        return_value=["masked", *command],
    ) as mask, patch("quimera.sandbox.state.build_workspace_sandbox_cmd") as confined:
        result = wrap_subprocess_cmd(workspace, str(tmp_path), command)

    assert result == ["masked", *command]
    mask.assert_called_once()
    confined.assert_not_called()


def test_wrapper_uses_confined_builder_when_sandbox_is_on(tmp_path):
    workspace = Workspace(tmp_path)
    ConfigManager(workspace.workspace_config_file).set_sandbox_enabled(True)
    command = ["echo", "ok"]

    with patch(
        "quimera.sandbox.state.build_workspace_sandbox_cmd",
        return_value=["bwrap", "--", *command],
    ) as confined, patch("quimera.sandbox.state.build_secret_mask_cmd") as mask:
        result = wrap_subprocess_cmd(workspace, str(tmp_path), command)

    assert result == ["bwrap", "--", *command]
    confined.assert_called_once()
    mask.assert_not_called()


def test_invalid_workspace_config_blocks_execution(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.workspace_config_file.write_text("{invalid", encoding="utf-8")

    with pytest.raises(SandboxError, match="execução bloqueada"):
        wrap_subprocess_cmd(workspace, str(tmp_path), ["echo"])


def test_facade_validates_before_persisting_and_uses_workspace_config(tmp_path):
    workspace = Workspace(tmp_path)
    app = QuimeraApp.__new__(QuimeraApp)
    app.workspace = workspace
    app.config = ConfigManager(workspace.config_file)

    with patch.object(app, "is_sandbox_available", return_value=False):
        with pytest.raises(SandboxError):
            app.set_sandbox_enabled(True)
    assert app.get_sandbox_enabled() is False

    with patch.object(app, "is_sandbox_available", return_value=True):
        assert app.set_sandbox_enabled(True) is True
    assert app.get_sandbox_enabled() is True
    assert ConfigManager(workspace.config_file).sandbox_enabled is False

    assert app.set_sandbox_enabled(False) is False
    assert app.get_sandbox_enabled() is False

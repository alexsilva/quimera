"""Tests for quimera.config - simplified to avoid module isolation issues."""
import json

import pytest


class TestConfigManagerBasics:
    """Basic tests for ConfigManager."""

    def test_default_values(self):
        """Test default values are correctly defined."""
        from quimera.config import (
            DEFAULT_USER_NAME,
            DEFAULT_HISTORY_WINDOW,
            DEFAULT_AUTO_SUMMARIZE_THRESHOLD,
            DEFAULT_IDLE_TIMEOUT_SECONDS,
        )

        assert DEFAULT_USER_NAME == ">>>"
        assert DEFAULT_HISTORY_WINDOW == 12
        assert DEFAULT_AUTO_SUMMARIZE_THRESHOLD == 30
        assert DEFAULT_IDLE_TIMEOUT_SECONDS == 360


class TestConfigManagerWithTempDir:
    """Test ConfigManager using temporary directory."""

    def test_load_empty_when_no_file(self, tmp_path):
        """Test _load returns empty dict when no file exists."""
        from quimera.config import ConfigManager

        cm = ConfigManager(tmp_path / "config.json")
        assert cm._load() == {}

    def test_load_reads_existing_file(self, tmp_path):
        """Test _load reads existing config file."""
        from quimera.config import ConfigManager

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"user_name": "Alice"}))

        cm = ConfigManager(config_file)
        assert cm._load()["user_name"] == "Alice"

    def test_load_handles_corrupted_json(self, tmp_path):
        """Test _load handles corrupted JSON gracefully."""
        from quimera.config import ConfigManager

        config_file = tmp_path / "config.json"
        config_file.write_text("{invalid json")

        cm = ConfigManager(config_file)
        assert cm._load() == {}

    def test_save_creates_directory_and_file(self, tmp_path):
        """Test _save creates directory and file."""
        from quimera.config import ConfigManager

        config_file = tmp_path / "config.json"
        cm = ConfigManager(config_file)
        cm._save({"test": "value"})
        assert config_file.exists()
        assert json.loads(config_file.read_text())["test"] == "value"


@pytest.mark.parametrize("prop,value,default", [
    ("user_name", "Bob", ">>>"),
    ("history_window", 20, 12),
    ("idle_timeout_seconds", 120, 360),
    ("auto_summarize_threshold", 48, 24),  # default is history_window * 2 = 24
])
def test_property_reads_from_config(tmp_path, prop, value, default):
    """Test properties read from config and fall back to defaults."""
    from quimera.config import ConfigManager

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({prop: value}))
    cm = ConfigManager(config_file)
    assert getattr(cm, prop) == value

    config_file.write_text(json.dumps({}))
    cm = ConfigManager(config_file)
    assert getattr(cm, prop) == default


@pytest.mark.parametrize("prop,invalid_value,expected", [
    ("history_window", "bad", 12),
    ("history_window", 0, 12),
    ("idle_timeout_seconds", "bad", 360),
    ("idle_timeout_seconds", 0, 360),
    ("auto_summarize_threshold", "bad", 24),  # falls back to history_window * 2
    ("auto_summarize_threshold", 0, 24),      # falls back to history_window * 2
])
def test_property_invalid_type_falls_back(tmp_path, prop, invalid_value, expected):
    """Test properties fall back for invalid type or zero."""
    from quimera.config import ConfigManager

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({prop: invalid_value}))
    cm = ConfigManager(config_file)
    assert getattr(cm, prop) == expected


@pytest.mark.parametrize("prop,setter,value,expected_key_present", [
    ("user_name", "set_user_name", "Charlie", True),
    ("user_name", "set_user_name", "", False),
    ("history_window", "set_history_window", 25, True),
    ("history_window", "set_history_window", None, False),
    ("idle_timeout_seconds", "set_idle_timeout_seconds", 90, True),
    ("idle_timeout_seconds", "set_idle_timeout_seconds", None, False),
    ("auto_summarize_threshold", "set_auto_summarize_threshold", 20, True),
    ("auto_summarize_threshold", "set_auto_summarize_threshold", None, False),
])
def test_setter_writes_config(tmp_path, prop, setter, value, expected_key_present):
    """Test setters write to config and handle removal."""
    from quimera.config import ConfigManager

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "user_name": "Old",
        "history_window": 10,
        "idle_timeout_seconds": 111,
        "auto_summarize_threshold": 22,
    }))
    cm = ConfigManager(config_file)
    getattr(cm, setter)(value)
    data = json.loads(config_file.read_text())
    if expected_key_present:
        assert data[prop] == value
    else:
        assert prop not in data


def test_workspace_policy_property_and_setter(tmp_path):
    """Test workspace_policy reads and persists valid presets."""
    from quimera.config import ConfigManager

    config_file = tmp_path / "config.json"
    cm = ConfigManager(config_file)

    assert cm.workspace_policy == "strict"

    cm.set_workspace_policy("autonomous")
    assert ConfigManager(config_file).workspace_policy == "autonomous"

    cm.set_workspace_policy("invalid")
    assert ConfigManager(config_file).workspace_policy == "strict"


def test_preserves_existing_keys(tmp_path):
    """Test setting one value preserves others."""
    from quimera.config import ConfigManager

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"user_name": "Alice", "history_window": 5}))

    cm = ConfigManager(config_file)
    cm.set_idle_timeout_seconds(90)
    data = json.loads(config_file.read_text())
    assert data["user_name"] == "Alice"
    assert data["history_window"] == 5
    assert data["idle_timeout_seconds"] == 90

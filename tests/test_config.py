"""Tests for quimera.config - simplified to avoid module isolation issues."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

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

        assert DEFAULT_USER_NAME == "Você"
        assert DEFAULT_HISTORY_WINDOW == 8
        assert DEFAULT_AUTO_SUMMARIZE_THRESHOLD == 30
        assert DEFAULT_IDLE_TIMEOUT_SECONDS == 60


class TestConfigManagerWithTempDir:
    """Test ConfigManager using temporary directory."""

    @pytest.fixture
    def temp_dir(self):
        """Create temp directory."""
        with tempfile.TemporaryDirectory() as td:
            yield Path(td)

    def test_load_empty_when_no_file(self, temp_dir):
        """Test _load returns empty dict when no file exists."""
        from quimera.config import ConfigManager

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            result = cm._load()
            assert result == {}

    def test_load_reads_existing_file(self, temp_dir):
        """Test _load reads existing config file."""
        from quimera.config import ConfigManager

        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({"user_name": "Alice"}))

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            result = cm._load()
            assert result["user_name"] == "Alice"

    def test_load_handles_corrupted_json(self, temp_dir):
        """Test _load handles corrupted JSON gracefully."""
        from quimera.config import ConfigManager

        config_file = temp_dir / "config.json"
        config_file.write_text("{invalid json")

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            result = cm._load()
            assert result == {}

    def test_save_creates_directory_and_file(self, temp_dir):
        """Test _save creates directory and file."""
        from quimera.config import ConfigManager

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            cm._save({"test": "value"})
            config_file = temp_dir / "config.json"
            assert config_file.exists()
            data = json.loads(config_file.read_text())
            assert data["test"] == "value"

    def test_user_name_property(self, temp_dir):
        """Test user_name property reads from config."""
        from quimera.config import ConfigManager

        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({"user_name": "Bob"}))

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            assert cm.user_name == "Bob"

    def test_user_name_fallback_to_default(self, temp_dir):
        """Test user_name falls back to default when not in config."""
        from quimera.config import ConfigManager, DEFAULT_USER_NAME

        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({}))

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            assert cm.user_name == DEFAULT_USER_NAME

    def test_history_window_property(self, temp_dir):
        """Test history_window property reads from config."""
        from quimera.config import ConfigManager

        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({"history_window": 20}))

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            assert cm.history_window == 20

    def test_history_window_invalid_type_falls_back(self, temp_dir):
        """Test history_window falls back for invalid type."""
        from quimera.config import ConfigManager, DEFAULT_HISTORY_WINDOW

        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({"history_window": "bad"}))

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            assert cm.history_window == DEFAULT_HISTORY_WINDOW

    def test_history_window_zero_falls_back(self, temp_dir):
        """Test history_window falls back for zero."""
        from quimera.config import ConfigManager, DEFAULT_HISTORY_WINDOW

        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({"history_window": 0}))

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            assert cm.history_window == DEFAULT_HISTORY_WINDOW

    def test_idle_timeout_seconds_property(self, temp_dir):
        """Test idle_timeout_seconds property."""
        from quimera.config import ConfigManager

        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({"idle_timeout_seconds": 120}))

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            assert cm.idle_timeout_seconds == 120

    def test_set_user_name(self, temp_dir):
        """Test set_user_name writes to config."""
        from quimera.config import ConfigManager

        config_file = temp_dir / "config.json"

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            cm.set_user_name("Charlie")
            data = json.loads(config_file.read_text())
            assert data["user_name"] == "Charlie"

    def test_set_user_name_empty_removes(self, temp_dir):
        """Test set_user_name with empty string removes key."""
        from quimera.config import ConfigManager

        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({"user_name": "Old"}))

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            cm.set_user_name("")
            data = json.loads(config_file.read_text())
            assert "user_name" not in data

    def test_set_history_window(self, temp_dir):
        """Test set_history_window writes to config."""
        from quimera.config import ConfigManager

        config_file = temp_dir / "config.json"

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            cm.set_history_window(25)
            data = json.loads(config_file.read_text())
            assert data["history_window"] == 25

    def test_set_history_window_none_removes(self, temp_dir):
        """Test set_history_window with None removes key."""
        from quimera.config import ConfigManager

        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({"history_window": 10}))

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            cm.set_history_window(None)
            data = json.loads(config_file.read_text())
            assert "history_window" not in data

    def test_preserves_existing_keys(self, temp_dir):
        """Test setting one value preserves others."""
        from quimera.config import ConfigManager

        config_file = temp_dir / "config.json"
        config_file.write_text(json.dumps({"user_name": "Alice", "history_window": 5}))

        with patch('quimera.config.QUIMERA_BASE', temp_dir):
            cm = ConfigManager()
            cm.set_idle_timeout_seconds(90)
            data = json.loads(config_file.read_text())
            assert data["user_name"] == "Alice"
            assert data["history_window"] == 5
            assert data["idle_timeout_seconds"] == 90
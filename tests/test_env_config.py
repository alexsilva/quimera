"""Tests for quimera.env_config."""
import os
import tempfile
from pathlib import Path

import pytest


class TestEnvConfig:
    """Test EnvConfig using temporary directory."""

    @pytest.fixture
    def temp_dir(self):
        """Create temp directory."""
        with tempfile.TemporaryDirectory() as td:
            yield Path(td)

    def test_load_empty_when_no_file(self, temp_dir):
        """Test _load returns empty dict when no file exists."""
        from quimera.env_config import EnvConfig

        env = EnvConfig(temp_dir / ".env")
        assert env._load() == {}

    def test_load_reads_existing_file(self, temp_dir):
        """Test _load reads existing env file."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text("API_KEY=secret\n", encoding="utf-8")

        env = EnvConfig(env_file)
        assert env._load() == {"API_KEY": "secret"}

    def test_load_ignores_comments_and_blank_lines(self, temp_dir):
        """Test _load ignores comments and blank lines."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text(
            "\n# comment\nAPI_KEY=secret\n\n  # another comment\nMODEL=gpt-5\n",
            encoding="utf-8",
        )

        env = EnvConfig(env_file)
        assert env._load() == {"API_KEY": "secret", "MODEL": "gpt-5"}

    def test_get_existing_key(self, temp_dir):
        """Test get returns existing key."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text("MODEL=gpt-5\n", encoding="utf-8")

        env = EnvConfig(env_file)
        assert env.get("MODEL") == "gpt-5"

    def test_get_missing_key_returns_default(self, temp_dir):
        """Test get returns default for missing key."""
        from quimera.env_config import EnvConfig

        env = EnvConfig(temp_dir / ".env")
        assert env.get("MISSING", "fallback") == "fallback"

    def test_set_creates_file(self, temp_dir):
        """Test set creates env file."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env = EnvConfig(env_file)

        env.set("API_KEY", "secret")

        assert env_file.exists()
        assert env_file.read_text(encoding="utf-8") == "API_KEY=secret\n"

    def test_set_updates_existing_key(self, temp_dir):
        """Test set updates existing key."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text("API_KEY=old\nMODEL=gpt-5\n", encoding="utf-8")

        env = EnvConfig(env_file)
        env.set("API_KEY", "new")

        assert env.all() == {"API_KEY": "new", "MODEL": "gpt-5"}

    def test_delete_removes_key(self, temp_dir):
        """Test delete removes existing key."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text("API_KEY=secret\nMODEL=gpt-5\n", encoding="utf-8")

        env = EnvConfig(env_file)
        env.delete("API_KEY")

        assert env.all() == {"MODEL": "gpt-5"}

    def test_delete_missing_key_is_noop(self, temp_dir):
        """Test delete of missing key keeps existing data unchanged."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text("MODEL=gpt-5\n", encoding="utf-8")

        env = EnvConfig(env_file)
        env.delete("API_KEY")

        assert env.all() == {"MODEL": "gpt-5"}

    def test_all_returns_copy(self, temp_dir):
        """Test all returns a copy of current state."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text("MODEL=gpt-5\n", encoding="utf-8")

        env = EnvConfig(env_file)
        data = env.all()
        data["MODEL"] = "changed"

        assert env.all() == {"MODEL": "gpt-5"}

    def test_apply_to_environ_uses_setdefault(self, temp_dir, monkeypatch):
        """Test apply_to_environ respects existing environment values."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text("MODEL=from-file\nAPI_KEY=secret\n", encoding="utf-8")
        monkeypatch.setenv("MODEL", "existing")
        monkeypatch.delenv("API_KEY", raising=False)

        env = EnvConfig(env_file)
        env.apply_to_environ()

        assert os.environ["MODEL"] == "existing"
        assert os.environ["API_KEY"] == "secret"

    def test_load_strips_double_quoted_value(self, temp_dir):
        """Test _load strips double quotes surrounding a value."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text('CHATGPT_KEY="sk-abc123"\n', encoding="utf-8")

        env = EnvConfig(env_file)
        assert env._load() == {"CHATGPT_KEY": "sk-abc123"}

    def test_load_strips_single_quoted_value(self, temp_dir):
        """Test _load strips single quotes surrounding a value."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text("CHATGPT_KEY='sk-abc123'\n", encoding="utf-8")

        env = EnvConfig(env_file)
        assert env._load() == {"CHATGPT_KEY": "sk-abc123"}

    def test_load_preserves_value_with_equals_sign(self, temp_dir):
        """Test _load keeps everything after the first = as the value."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text("TOKEN=abc=def=ghi\n", encoding="utf-8")

        env = EnvConfig(env_file)
        assert env._load() == {"TOKEN": "abc=def=ghi"}

    def test_load_preserves_mismatched_quotes(self, temp_dir):
        """Test _load does not strip mismatched quotes."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        env_file.write_text("TOKEN=\"value'\n", encoding="utf-8")

        env = EnvConfig(env_file)
        assert env._load() == {"TOKEN": "\"value'"}

    def test_setenv_persists_and_updates_environ(self, temp_dir, monkeypatch):
        """Test setenv persists to file and updates environment immediately."""
        from quimera.env_config import EnvConfig

        env_file = temp_dir / ".env"
        monkeypatch.delenv("API_KEY", raising=False)
        env = EnvConfig(env_file)

        env.setenv("API_KEY", "secret")

        assert env_file.read_text(encoding="utf-8") == "API_KEY=secret\n"
        assert os.environ["API_KEY"] == "secret"

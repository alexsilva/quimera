"""Configuration persistence under failures and concurrent updates."""
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest

from quimera.config import ConfigManager
from quimera.config_store import read_json_object, update_json_object, write_json_object
from quimera.profiles import base as profiles


def _increment_config(path):
    for _ in range(5):
        def update(data):
            count = data.get("count", 0)
            time.sleep(0.002)
            data["count"] = count + 1
        update_json_object(path, update)


@pytest.mark.parametrize("pool_type", ["thread", "process"])
def test_concurrent_updates_preserve_all_changes(tmp_path, pool_type):
    path = tmp_path / "config.json"
    write_json_object(path, {"unrelated": "preserved"})
    pool = (ThreadPoolExecutor(max_workers=4) if pool_type == "thread" else
            ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")))
    with pool:
        futures = [pool.submit(_increment_config, path) for _ in range(8)]
        for future in futures:
            future.result(timeout=20)
    assert read_json_object(path) == {"count": 40, "unrelated": "preserved"}


@pytest.mark.parametrize("payload", [b"[]", b"null", b'"secret-value"', b'{secret-value', b'\xff'])
def test_damaged_configuration_is_not_overwritten(tmp_path, caplog, payload):
    path = tmp_path / "config.json"
    path.write_bytes(payload)
    config = ConfigManager(path)
    assert config._load() == {}
    with pytest.raises(ValueError, match="antes de salvar") as error:
        config.set_user_name("new name")
    assert path.read_bytes() == payload
    assert "secret-value" not in caplog.text
    assert "secret-value" not in str(error.value)


def test_failed_replace_preserves_file_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    write_json_object(path, {"user_name": "old"})
    previous = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("simulated disk error")

    monkeypatch.setattr("quimera.config_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk error"):
        ConfigManager(path).set_user_name("new")
    assert path.read_bytes() == previous
    assert list(tmp_path.glob(".config.json.*")) == []


def test_failed_serialization_preserves_file(tmp_path):
    path = tmp_path / "config.json"
    write_json_object(path, {"count": 1})
    with pytest.raises(ValueError):
        update_json_object(path, lambda data: data.update(count=float("nan")))
    assert read_json_object(path) == {"count": 1}


def test_mcp_configuration_commits_both_fields_together(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    config = ConfigManager(path)
    config.set_user_name("preserved")
    snapshots = []
    from quimera import config_store
    original_write = config_store.write_json_object

    def capture_write(target, data):
        snapshots.append(dict(data))
        original_write(target, data)

    monkeypatch.setattr(config_store, "write_json_object", capture_write)
    config.set_mcp_configuration(["demo=stdio:demo"], ["demo=TOKEN=test"])
    assert snapshots == [{"user_name": "preserved", "mcp_clients": ["demo=stdio:demo"],
                          "mcp_client_env": ["demo=TOKEN=test"]}]
    config.set_mcp_configuration(None, None)
    assert read_json_object(path) == {"user_name": "preserved"}


@pytest.mark.parametrize("key,default", [
    ("history_window", 12),
    ("idle_timeout_seconds", 360),
    ("max_agent_execution_seconds", 3600),
    ("auto_summarize_threshold", 24),
])
def test_boolean_is_not_accepted_as_numeric_setting(tmp_path, key, default):
    path = tmp_path / "config.json"
    write_json_object(path, {key: True})
    config = ConfigManager(path)
    assert getattr(config, key) == default
    getattr(config, "set_" + key)(True)
    assert key not in read_json_object(path)


def test_connection_override_unchanged_when_persistence_fails(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(profiles, "_get_connections_file", lambda: path)
    previous = profiles.OpenAIConnection(model="old")
    profile = profiles.ExecutionProfile(name="demo", prefix="/demo", style=("blue", "Demo"),
                                        _connection_override=previous)
    with pytest.raises(ValueError, match="antes de salvar"):
        profiles.set_connection("demo", profiles.OpenAIConnection(model="new"), registry={"demo": profile})
    assert profile.effective_connection() is previous
    assert path.read_text() == "{broken"


def test_load_connections_ignores_invalid_entries(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    write_json_object(path, {"valid": {"model": "demo"}, "bad name": {}, "broken": []})
    monkeypatch.setattr(profiles, "_get_connections_file", lambda: path)
    assert profiles.load_connections() == {"valid": {"model": "demo"}}


def test_connection_update_preserves_legacy_profile_reference(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    write_json_object(path, {"demo": {"profile": "opencode"}})
    monkeypatch.setattr(profiles, "_get_connections_file", lambda: path)
    profile = profiles.ExecutionProfile(name="demo", prefix="/demo", style=("blue", "Demo"), dynamic=True)
    profiles.set_connection("demo", profiles.CliConnection(cmd=["demo"]), registry={"demo": profile})
    assert read_json_object(path)["demo"]["profile"]["profile"] == "opencode"

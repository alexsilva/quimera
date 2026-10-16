"""Catálogo MCP compartilhado, overlays e preservação do formato anterior."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from quimera.config import ConfigManager
from quimera.runtime.mcp.client import MCPClientBridge, MCPConnectionPhase, start_mcp_clients
from quimera.runtime.mcp.manager import MCPConnectionManager
from quimera.runtime.mcp.settings import (
    SCOPE_GLOBAL,
    SCOPE_LOCAL,
    MCPConnectionStore,
    MCPHttpSettings,
    MCPHttpSettingsStore,
)
from quimera.runtime.registry import ToolRegistry
from quimera.runtime.tools.mcp_clients import set_bridge


@pytest.fixture
def store(tmp_path):
    return MCPConnectionStore(
        tmp_path / "global" / "mcp-connections.json",
        tmp_path / "workspace" / "mcp-connections.json",
    )


def test_catalog_is_inactive_and_legacy_connections_remain_active(store):
    store.global_config.set_mcp_configuration(
        ["github=stdio:github", "jira=stdio:jira"], ["github=TOKEN=global"]
    )
    store.workspace_config.set_mcp_configuration(
        ["ide=http://localhost:43129/mcp"], ["github=TOKEN=workspace"]
    )
    before = store.workspace_file.read_bytes()

    entries = {entry.name: entry for entry in store.entries()}

    assert entries["github"].is_global and not entries["github"].active
    assert entries["github"].env_text == "TOKEN=workspace"
    assert entries["ide"].scope == "local" and entries["ide"].active
    assert store.active_specs() == (["ide=http://localhost:43129/mcp"], [])
    assert store.workspace_file.read_bytes() == before
    store.set_active("github", True)
    assert store.active_specs() == (
        ["github=stdio:github", "ide=http://localhost:43129/mcp"],
        ["github=TOKEN=workspace"],
    )
    store.set_active("github", False)
    assert store.entry("github").env_text == "TOKEN=workspace"
    with pytest.raises(ValueError, match="local"):
        store.set_active("ide", False)


def test_normalize_preserves_credentials_conflicts_and_existing_activation(store):
    store.global_config.set_mcp_configuration(
        ["github=stdio:github", "ide=http://localhost:43129/mcp", "jira=stdio:jira"],
        ["github=TOKEN=secret=with=equals,DISPLAY=:99", "jira=TOKEN=global"],
    )
    store.workspace_config.set_mcp_configuration(
        ["github=stdio:github", "ide=http://localhost:43135/mcp", "jira=stdio:jira"],
        ["github=DISPLAY=:99,TOKEN=secret=with=equals", "jira=TOKEN=local"],
    )
    store.workspace_config.set_mcp_active(["already-active"])
    global_before = store.global_file.read_bytes()

    assert store.normalize() == ["github"]
    assert store.workspace_config.mcp_active == ["already-active", "github"]
    assert store.entry("github").env_text == "TOKEN=secret=with=equals,DISPLAY=:99"
    assert store.entry("ide").spec == "ide=http://localhost:43135/mcp"
    assert store.entry("jira").env_text == "TOKEN=local"
    overlay_before = store.workspace_file.read_bytes()
    assert store.normalize() == []
    assert store.workspace_file.read_bytes() == overlay_before
    assert store.global_file.read_bytes() == global_before


def test_global_promotion_keeps_environment_and_activates_only_this_workspace(store, tmp_path):
    store.upsert("github=stdio:github", env_spec="github=TOKEN=local-secret")

    entry = store.upsert("github=stdio:github", scope="global")

    assert entry.is_global and entry.active
    assert entry.env_text == "TOKEN=local-secret"
    assert store.workspace_config.mcp_clients is None
    assert store.workspace_config.mcp_client_env is None
    other = MCPConnectionStore(store.global_file, tmp_path / "other" / "mcp-connections.json")
    assert other.entry("github").env_text == "TOKEN=local-secret"
    assert not other.entry("github").active
    assert not other.workspace_file.exists()
    assert store.global_file.stat().st_mode & 0o777 == 0o600


def test_clearing_local_environment_does_not_reintroduce_global_credentials(store):
    store.upsert("github=stdio:github", env_spec="github=TOKEN=global", scope="global")

    entry = store.upsert("github=stdio:github", env_spec="", scope="local")

    assert entry.scope == "local" and entry.env_text == ""
    assert store.workspace_config.mcp_client_env == ["github="]
    assert store.normalize() == []
    assert store.entry("github").env_text == ""
    assert store.global_config.mcp_client_env == ["github=TOKEN=global"]
    restored = store.remove("github")
    assert restored.is_global and restored.active
    assert restored.env_text == "TOKEN=global"


def test_identical_local_save_uses_catalog_and_removal_preserves_other_entries(store):
    store.upsert("github=stdio:github", env_spec="github=TOKEN=global", scope="global")
    store.upsert("ide=http://localhost:43129/mcp")
    entry = store.upsert("github=stdio:github", env_spec="github=TOKEN=global")
    assert entry.is_global
    assert store.workspace_config.mcp_clients == ["ide=http://localhost:43129/mcp"]
    assert store.remove("github") is None
    assert store.entry("github") is None
    assert store.entry("ide").active
    assert store.workspace_config.mcp_active is None


def test_parallel_catalog_edits_preserve_every_connection_and_environment(store, tmp_path):
    def save(index):
        peer = MCPConnectionStore(store.global_file, tmp_path / str(index) / "mcp-connections.json")
        peer.upsert(f"server{index}=stdio:cmd{index}", env_spec=f"server{index}=TOKEN={index}", scope="global")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(24)))

    entries = {entry.name: entry for entry in store.entries()}
    assert len(entries) == 24
    assert all(entries[f"server{i}"].env_text == f"TOKEN={i}" for i in range(24))


def test_invalid_catalog_is_preserved_when_saving(store):
    store.global_file.parent.mkdir()
    store.global_file.write_text("{invalid-json")
    with pytest.raises(ValueError, match="corrija o arquivo"):
        store.upsert("github=stdio:github", scope="global")
    assert store.global_file.read_text() == "{invalid-json"
    assert not store.workspace_file.exists()


def test_single_file_legacy_store_is_never_absorbed_into_itself(tmp_path):
    path = tmp_path / "mcp-connections.json"
    legacy = MCPConnectionStore(path, path)
    legacy.upsert("github=stdio:github", env_spec="github=TOKEN=kept")
    before = path.read_bytes()
    assert legacy.normalize() == []
    assert legacy.active_specs() == (["github=stdio:github"], ["github=TOKEN=kept"])
    assert path.read_bytes() == before


def test_boot_uses_only_active_catalog_entries_and_legacy_locals(store):
    store.global_config.set_mcp_configuration(["github=stdio:github", "jira=stdio:jira"], [])
    store.workspace_config.set_mcp_configuration(["ide=stdio:ide"], [])
    store.set_active("github", True)
    try:
        runtime = start_mcp_clients(cli_specs=None, cli_env_specs=None, store=store)
        assert runtime.specs == ("github=stdio:github", "ide=stdio:ide")
        assert set(runtime.bridge.states()) == {"github", "ide"}
        assert runtime.bridge.sessions == {}
    finally:
        set_bridge(None)


def test_http_settings_resolve_per_key_and_explicit_false_overrides_global(tmp_path):
    store = MCPHttpSettingsStore(tmp_path / "global.json", tmp_path / "local.json")
    assert store.load() == MCPHttpSettings()
    store.save("global", enabled=True, host="0.0.0.0", port=9096, allow_tools="agent")
    store.save("local", enabled=False, port=9097)
    assert store.load() == MCPHttpSettings(False, "0.0.0.0", 9097, "agent")
    assert store.sources() == dict(enabled="local", host="global", port="local", allow_tools="global")
    store.save("local", port=None)
    assert store.load().port == 9096
    assert json.loads(store.workspace_file.read_text()) == {"http_enabled": False}


@pytest.mark.parametrize("values", [{"port": 0}, {"port": 65536}, {"port": True}, {"enabled": "false"}, {"host": " "}])
def test_http_settings_reject_invalid_values_without_writing(tmp_path, values):
    store = MCPHttpSettingsStore(tmp_path / "global.json", tmp_path / "local.json")
    with pytest.raises(ValueError):
        store.save("local", **values)
    assert not store.workspace_file.exists()


def test_connection_store_from_workspace_accepts_legacy_fixture(tmp_path):
    legacy_file = tmp_path / "mcp-connections.json"
    ConfigManager(legacy_file).set_mcp_configuration(["jira=stdio:jira"], None)

    store = MCPConnectionStore.from_workspace(SimpleNamespace(mcp_connections_file=legacy_file))

    assert store.global_file == legacy_file
    assert store.workspace_file == legacy_file
    assert [(entry.name, entry.scope, entry.active) for entry in store.entries()] == [
        ("jira", SCOPE_LOCAL, True)
    ]


def test_local_environment_overrides_global_without_local_spec(store):
    store.global_config.set_mcp_configuration(
        ["jira=stdio:jira"], ["jira=TOKEN=global,MODE=prod"]
    )
    store.workspace_config.set_mcp_configuration(None, ["jira=TOKEN=local"])
    store.workspace_config.set_mcp_active(["jira"])

    entry = store.entry("jira")

    assert entry.scope == SCOPE_GLOBAL
    assert entry.spec == "jira=stdio:jira"
    assert entry.env_spec == "jira=TOKEN=local"
    assert store.active_specs() == (["jira=stdio:jira"], ["jira=TOKEN=local"])


def test_client_toggle_persists_workspace_activation_and_restarts_only_when_enabled(
    store, monkeypatch
):
    store.global_config.set_mcp_configuration(["jira=stdio:jira"], None)
    store.workspace_config.set_mcp_active(["jira"])

    bridge = MCPClientBridge()
    bridge.mark_failed("jira", "offline", transport="stdio")
    set_bridge(bridge)
    executor = MagicMock(registry=ToolRegistry())
    manager = MCPConnectionManager(
        executor=executor,
        workspace=SimpleNamespace(),
        store=store,
    )
    started = []
    monkeypatch.setattr(
        manager,
        "_start_connection_thread",
        lambda spec, **_kwargs: started.append(spec),
    )

    try:
        disabled = manager.set_active("jira", False)
        assert not disabled.active
        assert store.workspace_config.mcp_active is None
        assert bridge.state("jira") is None

        enabled = manager.set_active("jira", True)
        assert enabled.active
        assert store.workspace_config.mcp_active == ["jira"]
        assert started == ["jira=stdio:jira"]
        assert bridge.state("jira").phase == MCPConnectionPhase.PENDING
    finally:
        set_bridge(None)


def test_http_clients_active_is_a_workspace_overlay_keyed_by_name(tmp_path):
    """A liberação de clients OAuth é local; sem a chave, nenhum client entra."""
    store = MCPHttpSettingsStore(
        tmp_path / "global" / "mcp-server.json", tmp_path / "workspace" / "mcp-server.json"
    )
    assert store.active_clients() == []
    assert not store.is_client_active("ChatGPT")

    assert store.set_client_active("ChatGPT", True) == ["ChatGPT"]
    assert store.set_client_active(" Claude Code (quimera) ", True) == ["ChatGPT", "Claude Code (quimera)"]
    assert store.set_client_active("ChatGPT", True) == ["Claude Code (quimera)", "ChatGPT"]
    assert store.is_client_active("ChatGPT") and store.is_client_active("Claude Code (quimera)")
    assert not store.is_client_active("Grok")
    assert not store.global_file.exists()
    # A liberação convive com as demais chaves do overlay sem apagá-las.
    store.save("local", enabled=True, port=9096)
    assert store.active_clients() == ["Claude Code (quimera)", "ChatGPT"]
    assert store.load().port == 9096

    assert store.set_client_active("ChatGPT", False) == ["Claude Code (quimera)"]
    assert store.set_client_active("Claude Code (quimera)", False) == []
    assert "http_clients_active" not in json.loads(store.workspace_file.read_text(encoding="utf-8"))
    assert store.load().enabled
    with pytest.raises(ValueError, match="nome"):
        store.set_client_active("  ", True)

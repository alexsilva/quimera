"""Tests for quimera/plugins/base.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quimera.plugins.base import (
    AgentPlugin,
    CliConnection,
    OpenAIConnection,
    PluginRegistry,
    _connection_from_dict,
    _dynamic_plugin_metadata,
    _sanitize_dynamic_plugin_metadata,
    _humanize_agent_name,
    all_names,
    all_plugins,
    apply_connection_overrides,
    connection_to_dict,
    extract_model_from_cli_cmd,
    format_connection_label,
    get,
    is_valid_agent_name,
    load_connections,
    register,
    register_dynamic_plugin,
    reload_plugins,
    remove_connection,
    save_connections,
    set_connection_override,
)


class _EnvAwarePlugin(AgentPlugin):
    def env_for_cli(self) -> dict:
        return {"DYNAMIC_ENV_HOOK": "ok"}


# ---------------------------------------------------------------------------
# extract_model_from_cli_cmd
# ---------------------------------------------------------------------------

def test_extract_model_empty_cmd():
    assert extract_model_from_cli_cmd([]) is None
    assert extract_model_from_cli_cmd(None) is None


def test_extract_model_exception_in_cmd():
    # Objects that raise on str() won't happen easily, but simulate via bad iterable
    bad = MagicMock()
    bad.__iter__ = MagicMock(side_effect=Exception("boom"))
    # Can't iterate → returns None via except
    assert extract_model_from_cli_cmd(bad) is None


def test_extract_model_short_flag_equals():
    assert extract_model_from_cli_cmd(["-m=gpt-3"]) == "gpt-3"


def test_extract_model_short_flag_empty_value():
    assert extract_model_from_cli_cmd(["-m="]) is None


def test_extract_model_space_flag_skip_dash_value():
    # Next arg starts with '-', should not be taken as model
    assert extract_model_from_cli_cmd(["-m", "-bad"]) is None


def test_extract_model_not_found():
    assert extract_model_from_cli_cmd(["claude", "--foo", "bar"]) is None


# ---------------------------------------------------------------------------
# _humanize_agent_name
# ---------------------------------------------------------------------------

def test_humanize_agent_name_dashes():
    assert _humanize_agent_name("my-agent") == "My Agent"


def test_humanize_agent_name_underscores():
    assert _humanize_agent_name("deep_seek_pro") == "Deep Seek Pro"


# ---------------------------------------------------------------------------
# _dynamic_plugin_metadata
# ---------------------------------------------------------------------------

def test_dynamic_plugin_metadata_structure():
    meta = _dynamic_plugin_metadata("testbot")
    assert meta["dynamic"] is True
    assert meta["prefix"] == "/testbot"
    assert "general" in meta["capabilities"]
    assert meta["supports_task_execution"] is True


# ---------------------------------------------------------------------------
# register_dynamic_plugin
# ---------------------------------------------------------------------------

def test_register_dynamic_plugin_creates_plugin():
    plugin = register_dynamic_plugin("dyntest1")
    assert plugin.name == "dyntest1"
    assert plugin.dynamic is True
    assert plugin.prefix == "/dyntest1"


def test_register_dynamic_plugin_with_connection():
    conn = OpenAIConnection(model="gpt-4", base_url="http://x", api_key_env="K")
    plugin = register_dynamic_plugin("dyntest2", connection=conn)
    assert plugin.effective_connection() == conn


def test_register_dynamic_plugin_invalid_name():
    with pytest.raises(ValueError):
        register_dynamic_plugin("INVALID NAME!")


def test_register_dynamic_plugin_with_metadata():
    meta = {"icon": "🔥", "base_tier": 3}
    plugin = register_dynamic_plugin("dyntest3", metadata=meta)
    assert plugin.icon == "🔥"
    assert plugin.base_tier == 3


def test_sanitize_dynamic_plugin_metadata_drops_private_and_identity_fields():
    """Metadata persistido não pode injetar campos privados nem trocar identidade."""
    sanitized = _sanitize_dynamic_plugin_metadata({
        "name": "evil",
        "prefix": "/safe",
        "_mcp_token": "secret",
        "_connection_override": {"type": "cli"},
        "supports_tools": False,
    })

    assert sanitized == {"prefix": "/safe", "supports_tools": False}


def test_register_dynamic_plugin_ignores_unsafe_metadata_fields():
    """Campos desconhecidos/privados em metadata não quebram nem contaminam plugin."""
    plugin = register_dynamic_plugin(
        "safeagent",
        metadata={
            "name": "evil",
            "_mcp_token": "secret",
            "unknown_field": "boom",
            "icon": "🛡️",
        },
    )

    assert plugin.name == "safeagent"
    assert plugin.icon == "🛡️"
    assert plugin._mcp_token is None


def test_register_dynamic_plugin_inherits_base():
    # Register a base plugin first
    base = AgentPlugin(
        name="baseagent",
        prefix="/baseagent",
        style=("blue", "Base"),
        spy_stdout_formatter=lambda x: [],
        has_builtin_tools=True,
        runtime_rw_paths=["/tmp"],
    )
    register(base)
    meta = {"base": "baseagent"}
    plugin = register_dynamic_plugin("dyntest4", metadata=meta)
    assert plugin.spy_stdout_formatter is not None
    assert plugin.has_builtin_tools is True
    assert "/tmp" in plugin.runtime_rw_paths
    assert plugin._base_plugin_name == "baseagent"


def test_register_dynamic_plugin_inherits_base_class():
    base = _EnvAwarePlugin(
        name="envbase",
        prefix="/envbase",
        style=("blue", "Env Base"),
    )
    register(base)
    plugin = register_dynamic_plugin("dyntest5", metadata={"base": "envbase"})

    assert isinstance(plugin, _EnvAwarePlugin)
    assert plugin.env_for_cli() == {"DYNAMIC_ENV_HOOK": "ok"}


# ---------------------------------------------------------------------------
# _connection_from_dict
# ---------------------------------------------------------------------------

def test_connection_from_dict_cli_string_cmd():
    data = {"type": "cli", "cmd": "echo hello world"}
    conn = _connection_from_dict(data)
    assert isinstance(conn, CliConnection)
    assert conn.cmd == ["echo", "hello", "world"]


def test_connection_from_dict_cli_single_item_with_spaces():
    data = {"type": "cli", "cmd": ["echo hello world"]}
    conn = _connection_from_dict(data)
    assert isinstance(conn, CliConnection)
    assert conn.cmd == ["echo", "hello", "world"]


def test_connection_from_dict_cli_list_cmd():
    data = {"type": "cli", "cmd": ["echo", "hello"], "prompt_as_arg": True, "output_format": "json"}
    conn = _connection_from_dict(data)
    assert isinstance(conn, CliConnection)
    assert conn.prompt_as_arg is True
    assert conn.output_format == "json"


def test_connection_from_dict_openai():
    data = {"model": "gpt-4", "base_url": "http://x", "provider": "openai"}
    conn = _connection_from_dict(data)
    assert isinstance(conn, OpenAIConnection)
    assert conn.model == "gpt-4"


# ---------------------------------------------------------------------------
# connection_to_dict
# ---------------------------------------------------------------------------

def test_connection_to_dict_cli():
    conn = CliConnection(cmd=["echo"], prompt_as_arg=True)
    d = connection_to_dict(conn)
    assert d["type"] == "cli"
    assert d["cmd"] == ["echo"]


def test_connection_to_dict_openai():
    conn = OpenAIConnection(model="gpt-4")
    d = connection_to_dict(conn)
    assert d["type"] == "openai"
    assert d["model"] == "gpt-4"


# ---------------------------------------------------------------------------
# AgentPlugin properties and methods
# ---------------------------------------------------------------------------

def _make_plugin(**kwargs) -> AgentPlugin:
    defaults = dict(name="testplugin", prefix="/test", style=("blue", "Test"), cmd=["claude"])
    defaults.update(kwargs)
    return AgentPlugin(**defaults)


def test_render_style():
    p = _make_plugin(icon="🚀", style=("red", "Rocket"))
    assert p.render_style == ("red", "🚀  Rocket")


def test_configure_with_model_not_cli():
    p = _make_plugin(driver="openai_compat", model="gpt-4", base_url="http://x", api_key_env="K")
    # Set override to OpenAI so effective_connection is OpenAIConnection
    object.__setattr__(p, "_connection_override", OpenAIConnection())
    with pytest.raises(ValueError, match="não usa driver CLI"):
        p.configure_with_model("gpt-3")


def test_configure_with_model_empty_model_id():
    p = _make_plugin(cmd=["claude", "--model=claude-3"])
    with pytest.raises(ValueError, match="não pode ser vazio"):
        p.configure_with_model("")


def test_configure_with_model_no_placeholder():
    p = _make_plugin(cmd=["claude", "--verbose"])
    with pytest.raises(ValueError, match="não tem placeholder"):
        p.configure_with_model("gpt-4")


def test_configure_with_model_success():
    p = _make_plugin(cmd=["claude", "--model=old-model", "--verbose"])
    conn = p.configure_with_model("new-model")
    assert conn.cmd == ["claude", "--model=new-model", "--verbose"]


def test_effective_cmd_openai_connection():
    p = _make_plugin(cmd=["fallback"])
    object.__setattr__(p, "_connection_override", OpenAIConnection(model="gpt-4"))
    # OpenAI connection → returns list(self.cmd)
    assert p.effective_cmd() == ["fallback"]


def test_effective_prompt_as_arg_openai():
    p = _make_plugin(prompt_as_arg=True)
    object.__setattr__(p, "_connection_override", OpenAIConnection())
    assert p.effective_prompt_as_arg() is True


def test_effective_output_format_openai():
    p = _make_plugin(output_format="stream-json")
    object.__setattr__(p, "_connection_override", OpenAIConnection())
    assert p.effective_output_format() == "stream-json"


def test_effective_model_cli():
    p = _make_plugin(model="claude-3")
    # No override, driver is "cli" → CliConnection
    assert p.effective_model() == "claude-3"


def test_effective_base_url_cli():
    p = _make_plugin(base_url="http://cli-url")
    assert p.effective_base_url() == "http://cli-url"


def test_effective_api_key_env_cli():
    p = _make_plugin(api_key_env="MY_KEY")
    assert p.effective_api_key_env() == "MY_KEY"


# ---------------------------------------------------------------------------
# format_connection_label
# ---------------------------------------------------------------------------

def test_format_connection_label_cli_no_cmd():
    conn = CliConnection(cmd=[])
    assert "sem comando" in format_connection_label(conn)


def test_format_connection_label_cli_with_cmd():
    conn = CliConnection(cmd=["echo", "hello"])
    label = format_connection_label(conn)
    assert "echo hello" in label


def test_format_connection_label_openai_with_extra_body():
    conn = OpenAIConnection(model="gpt-4", extra_body={"thinking": {"type": "enabled"}})
    label = format_connection_label(conn)
    assert "extra_body" in label
    assert "thinking" in label


def test_format_connection_label_openai_no_extra_body():
    conn = OpenAIConnection(model="gpt-4")
    label = format_connection_label(conn)
    assert "extra_body" not in label


# ---------------------------------------------------------------------------
# load/save/remove connections (filesystem mocked)
# ---------------------------------------------------------------------------

def test_load_connections_file_not_exists(tmp_path):
    with patch("quimera.plugins.base._get_connections_file", return_value=tmp_path / "conn.json"):
        result = load_connections()
    assert result == {}


def test_load_connections_file_exists(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({"agent1": {"model": "gpt-4"}}), encoding="utf-8")
    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        result = load_connections()
    assert result == {"agent1": {"model": "gpt-4"}}


def test_save_connections(tmp_path):
    f = tmp_path / "conn.json"
    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        save_connections({"a": {"model": "x"}})
    assert json.loads(f.read_text()) == {"a": {"model": "x"}}


def test_remove_connection_existing(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({"myagent": {"model": "gpt-4"}}), encoding="utf-8")
    p = _make_plugin(name="myagent")
    register(p)
    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        result = remove_connection("myagent")
    assert result is True
    assert p._connection_override is None


def test_remove_connection_not_existing(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({}), encoding="utf-8")
    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        result = remove_connection("noexist")
    assert result is False


# ---------------------------------------------------------------------------
# set_connection_override
# ---------------------------------------------------------------------------

def test_set_connection_override_no_persist():
    p = _make_plugin(name="sco_test1")
    register(p)
    conn = OpenAIConnection(model="gpt-4")
    with patch("quimera.plugins.base.load_connections") as mock_load:
        set_connection_override("sco_test1", conn, persist=False)
        mock_load.assert_not_called()
    assert p._connection_override == conn


def test_set_connection_override_with_persist_dynamic(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({}), encoding="utf-8")
    plugin = register_dynamic_plugin("sco_dyn1")
    conn = OpenAIConnection(model="gpt-4", base_url="http://x", api_key_env="K")
    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        set_connection_override("sco_dyn1", conn, persist=True)
    saved = json.loads(f.read_text())
    assert "sco_dyn1" in saved
    assert saved["sco_dyn1"]["plugin"]["dynamic"] is True


# ---------------------------------------------------------------------------
# apply_connection_overrides / reload_plugins
# ---------------------------------------------------------------------------

def test_apply_connection_overrides_registers_dynamic(tmp_path):
    f = tmp_path / "conn.json"
    conn_data = {
        "type": "openai",
        "model": "gpt-4",
        "base_url": "http://x",
        "api_key_env": "K",
        "provider": "openai",
        "supports_native_tools": True,
        "extra_body": None,
        "plugin": {"dynamic": True},
    }
    f.write_text(json.dumps({"newdynagent": conn_data}), encoding="utf-8")
    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        apply_connection_overrides()
    p = get("newdynagent")
    assert p is not None
    assert p._connection_override is not None


def test_apply_connection_overrides_skips_invalid(tmp_path):
    f = tmp_path / "conn.json"
    conn_data = {
        "type": "openai",
        "model": "gpt-4",
        "base_url": "http://x",
        "api_key_env": "K",
        "provider": "openai",
        "supports_native_tools": True,
        "extra_body": None,
        "plugin": {"dynamic": True},
    }
    # Invalid agent name → register_dynamic_plugin raises ValueError → skip
    f.write_text(json.dumps({"INVALID AGENT": conn_data}), encoding="utf-8")
    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        apply_connection_overrides()  # should not raise
    assert get("invalid agent") is None


def test_reload_plugins_returns_names(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({}), encoding="utf-8")
    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        names = reload_plugins()
    assert isinstance(names, list)


def test_apply_connection_overrides_uses_explicit_registry(tmp_path):
    f = tmp_path / "conn.json"
    conn_data = {
        "type": "openai",
        "model": "gpt-4",
        "base_url": "http://x",
        "api_key_env": "K",
        "provider": "openai",
        "supports_native_tools": True,
        "extra_body": None,
        "plugin": {"dynamic": True},
    }
    f.write_text(json.dumps({"scoped_agent_zz99": conn_data}), encoding="utf-8")
    scoped_registry = PluginRegistry()

    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        apply_connection_overrides(registry=scoped_registry)

    plugin = scoped_registry.get("scoped_agent_zz99")
    assert plugin is not None
    assert plugin._connection_override is not None


def test_set_connection_override_uses_explicit_registry_without_persist():
    scoped_registry = PluginRegistry()
    plugin = _make_plugin(name="scoped-agent")
    scoped_registry.register(plugin)
    conn = OpenAIConnection(model="gpt-4")

    set_connection_override("scoped-agent", conn, persist=False, registry=scoped_registry)

    assert plugin._connection_override == conn


def test_reload_plugins_uses_explicit_registry(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({}), encoding="utf-8")
    scoped_registry = PluginRegistry()
    scoped_registry.register(_make_plugin(name="only-local"))

    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        names = reload_plugins(registry=scoped_registry)

    assert names == ["only-local"]


def test_remove_connection_uses_explicit_registry(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({"scoped-agent": {"model": "gpt-4"}}), encoding="utf-8")
    scoped_registry = PluginRegistry()
    plugin = _make_plugin(name="scoped-agent")
    object.__setattr__(plugin, "_connection_override", OpenAIConnection(model="gpt-4"))
    scoped_registry.register(plugin)

    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        removed = remove_connection("scoped-agent", registry=scoped_registry)

    assert removed is True
    assert plugin._connection_override is None


# ---------------------------------------------------------------------------
# is_valid_agent_name
# ---------------------------------------------------------------------------

def test_is_valid_agent_name_valid():
    assert is_valid_agent_name("my-agent") is True
    assert is_valid_agent_name("agent123") is True


def test_is_valid_agent_name_invalid():
    assert is_valid_agent_name("") is False
    assert is_valid_agent_name("My Agent") is False
    assert is_valid_agent_name("agent!") is False


# ---------------------------------------------------------------------------
# extract_model_from_cli_cmd — remaining branches
# ---------------------------------------------------------------------------

def test_extract_model_long_flag_equals():
    assert extract_model_from_cli_cmd(["claude", "--model=claude-3"]) == "claude-3"


def test_extract_model_space_flag_returns_model():
    assert extract_model_from_cli_cmd(["claude", "--model", "claude-3"]) == "claude-3"


# ---------------------------------------------------------------------------
# effective_connection — non-cli driver (line 273)
# ---------------------------------------------------------------------------

def test_effective_connection_non_cli_driver():
    p = _make_plugin(driver="openai_compat", model="gpt-4", base_url="http://x", api_key_env="MY_KEY")
    conn = p.effective_connection()
    assert isinstance(conn, OpenAIConnection)
    assert conn.model == "gpt-4"
    assert conn.provider == "openai_compat"


# ---------------------------------------------------------------------------
# effective_driver
# ---------------------------------------------------------------------------

def test_effective_driver_cli():
    p = _make_plugin(cmd=["echo"])
    assert p.effective_driver() == "cli"


def test_effective_driver_openai():
    p = _make_plugin(driver="openai_compat", model="gpt-4", base_url="http://x", api_key_env="K")
    assert p.effective_driver() == "openai_compat"


# ---------------------------------------------------------------------------
# effective_cmd — CliConnection branch (line 297)
# ---------------------------------------------------------------------------

def test_effective_cmd_cli_connection():
    p = _make_plugin(cmd=["echo", "hi"])
    assert p.effective_cmd() == ["echo", "hi"]


# ---------------------------------------------------------------------------
# resolve_runtime_model (lines 302-303)
# ---------------------------------------------------------------------------

def test_resolve_runtime_model():
    p = _make_plugin(cmd=["claude", "--model=my-model"])
    assert p.resolve_runtime_model() == "my-model"


def test_resolve_runtime_model_with_cwd():
    p = _make_plugin(cmd=["claude", "--model=my-model"])
    assert p.resolve_runtime_model(cwd="/tmp") == "my-model"


# ---------------------------------------------------------------------------
# effective_prompt_as_arg — CliConnection branch (line 309)
# ---------------------------------------------------------------------------

def test_effective_prompt_as_arg_cli():
    p = _make_plugin(cmd=["echo"], prompt_as_arg=True)
    assert p.effective_prompt_as_arg() is True


# ---------------------------------------------------------------------------
# effective_output_format — CliConnection branch (line 316)
# ---------------------------------------------------------------------------

def test_effective_output_format_cli():
    p = _make_plugin(cmd=["echo"], output_format="json")
    assert p.effective_output_format() == "json"


# ---------------------------------------------------------------------------
# effective_model — OpenAI connection branch (line 323)
# ---------------------------------------------------------------------------

def test_effective_model_openai():
    p = _make_plugin(driver="openai_compat", model="gpt-4", base_url="http://x", api_key_env="K")
    assert p.effective_model() == "gpt-4"


# ---------------------------------------------------------------------------
# effective_base_url — OpenAI connection branch (line 330)
# ---------------------------------------------------------------------------

def test_effective_base_url_openai():
    p = _make_plugin(driver="openai_compat", model="gpt-4", base_url="http://custom", api_key_env="K")
    assert p.effective_base_url() == "http://custom"


# ---------------------------------------------------------------------------
# effective_api_key_env — OpenAI connection branch (line 337)
# ---------------------------------------------------------------------------

def test_effective_api_key_env_openai():
    p = _make_plugin(driver="openai_compat", model="gpt-4", base_url="http://x", api_key_env="MY_VAR")
    assert p.effective_api_key_env() == "MY_VAR"


# ---------------------------------------------------------------------------
# all_plugins (line 437)
# ---------------------------------------------------------------------------

def test_all_plugins_returns_list():
    from quimera.plugins.base import all_plugins
    result = all_plugins()
    assert isinstance(result, list)
    assert all(isinstance(p, AgentPlugin) for p in result)


# ---------------------------------------------------------------------------
# apply_connection_overrides — plugin is None after register attempt (line 141)
# Uses a mock where register_dynamic_plugin succeeds but _registry still has None
# ---------------------------------------------------------------------------

def test_apply_connection_overrides_plugin_none_after_register(tmp_path):
    f = tmp_path / "conn.json"
    conn_data = {
        "type": "cli",
        "cmd": ["echo"],
        "plugin": {"dynamic": True},
    }
    # Use a valid name not previously registered
    f.write_text(json.dumps({"tempagent99": conn_data}), encoding="utf-8")

    original_get = __import__("quimera.plugins.base", fromlist=["_registry"])
    call_count = [0]

    def patched_register(name, metadata=None, registry=None):
        # Register succeeds but we simulate the next _registry.get returning None
        pass  # Don't actually register — so plugin stays None

    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        with patch("quimera.plugins.base.register_dynamic_plugin", side_effect=patched_register):
            apply_connection_overrides()  # plugin will be None → hits line 141 continue


# ---------------------------------------------------------------------------
# set_connection_override — dynamic plugin with base_ref (line 183)
# ---------------------------------------------------------------------------

def test_set_connection_override_dynamic_with_base_ref(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({}), encoding="utf-8")
    # Register a base
    base = AgentPlugin(name="mybase2", prefix="/mybase2", style=("green", "Base2"))
    register(base)
    # Register dynamic with base reference
    plugin = register_dynamic_plugin("dynwithbase1", metadata={"base": "mybase2"})
    assert plugin._base_plugin_name == "mybase2"

    conn = OpenAIConnection(model="gpt-4", base_url="http://x", api_key_env="K")
    with patch("quimera.plugins.base._get_connections_file", return_value=f):
        set_connection_override("dynwithbase1", conn, persist=True)

    saved = json.loads(f.read_text())
    assert saved["dynwithbase1"]["plugin"]["base"] == "mybase2"


# ---------------------------------------------------------------------------
# PluginRegistry
# ---------------------------------------------------------------------------

def _make_plugin_for_registry(name: str = "regtest") -> AgentPlugin:
    return AgentPlugin(name=name, prefix=f"/{name}", style=("blue", name))


def test_plugin_registry_register_and_get():
    registry = PluginRegistry()
    p = _make_plugin_for_registry("p1")
    registry.register(p)
    assert registry.get("p1") is p


def test_plugin_registry_get_nonexistent():
    registry = PluginRegistry()
    assert registry.get("nope") is None


def test_plugin_registry_all_names():
    registry = PluginRegistry()
    registry.register(_make_plugin_for_registry("b"))
    registry.register(_make_plugin_for_registry("a"))
    assert registry.all_names() == ["b", "a"]


def test_plugin_registry_all_plugins():
    registry = PluginRegistry()
    p = _make_plugin_for_registry("p1")
    registry.register(p)
    result = registry.all_plugins()
    assert result == [p]


def test_plugin_registry_isolation():
    r1 = PluginRegistry()
    r2 = PluginRegistry()
    r1.register(_make_plugin_for_registry("shared"))
    assert r2.get("shared") is None


def test_default_registry_is_plugin_registry_instance():
    from quimera.plugins.base import _registry
    assert isinstance(_registry, PluginRegistry)


def test_register_get_all_names_all_plugins_delegate_to_default():
    p = _make_plugin_for_registry("delegated")
    register(p)
    assert get("delegated") is p
    assert "delegated" in all_names()
    assert p in all_plugins()

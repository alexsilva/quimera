"""Tests for quimera/profiles/base.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quimera.profiles.base import (
    ExecutionProfile,
    CliConnection,
    OpenAIConnection,
    ProfileRegistry,
    _connection_from_dict,
    _connection_profile_metadata,
    _sanitize_connection_profile_metadata,
    _humanize_agent_name,
    all_names,
    all_profiles,
    apply_connections,
    connection_to_dict,
    extract_model_from_cli_cmd,
    format_connection_label,
    get,
    is_valid_agent_name,
    load_connections,
    register,
    register_connection_profile,
    reload_profiles,
    remove_connection,
    save_connections,
    set_connection,
)


class _EnvAwareProfile(ExecutionProfile):
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
# _connection_profile_metadata
# ---------------------------------------------------------------------------

def test_connection_profile_metadata_structure():
    meta = _connection_profile_metadata("testbot")
    assert meta["dynamic"] is True
    assert meta["prefix"] == "/testbot"
    assert "general" in meta["capabilities"]
    assert meta["supports_task_execution"] is True


# ---------------------------------------------------------------------------
# register_connection_profile
# ---------------------------------------------------------------------------

def test_register_connection_profile_creates_profile():
    profile = register_connection_profile("dyntest1")
    assert profile.name == "dyntest1"
    assert profile.dynamic is True
    assert profile.prefix == "/dyntest1"


def test_register_connection_profile_with_connection():
    conn = OpenAIConnection(model="gpt-4", base_url="http://x", api_key_env="K")
    profile = register_connection_profile("dyntest2", connection=conn)
    assert profile.effective_connection() == conn


def test_register_connection_profile_invalid_name():
    with pytest.raises(ValueError):
        register_connection_profile("INVALID NAME!")


def test_register_connection_profile_with_metadata():
    meta = {"icon": "🔥", "base_tier": 3}
    profile = register_connection_profile("dyntest3", metadata=meta)
    assert profile.icon == "🔥"
    assert profile.base_tier == 3


def test_sanitize_connection_profile_metadata_drops_private_and_identity_fields():
    """Metadata persistido não pode injetar campos privados nem trocar identidade."""
    sanitized = _sanitize_connection_profile_metadata({
        "name": "evil",
        "prefix": "/safe",
        "_mcp_token": "secret",
        "_connection_override": {"type": "cli"},
        "supports_tools": False,
    })

    assert sanitized == {"prefix": "/safe", "supports_tools": False}


def test_register_connection_profile_ignores_unsafe_metadata_fields():
    """Campos desconhecidos/privados em metadata não quebram nem contaminam profile."""
    profile = register_connection_profile(
        "safeagent",
        metadata={
            "name": "evil",
            "_mcp_token": "secret",
            "unknown_field": "boom",
            "icon": "🛡️",
        },
    )

    assert profile.name == "safeagent"
    assert profile.icon == "🛡️"
    assert profile._mcp_token is None


def test_register_connection_profile_inherits_base():
    # Register a profile first
    base = ExecutionProfile(
        name="baseagent",
        prefix="/baseagent",
        style=("blue", "Base"),
        spy_stdout_formatter=lambda x: [],
        has_builtin_tools=True,
        runtime_rw_paths=["/tmp"],
    )
    register(base)
    meta = {"profile": "baseagent"}
    profile = register_connection_profile("dyntest4", metadata=meta)
    assert profile.spy_stdout_formatter is not None
    assert profile.has_builtin_tools is True
    assert "/tmp" in profile.runtime_rw_paths
    assert profile._profile_name == "baseagent"


def test_register_connection_profile_inherits_base_class():
    base = _EnvAwareProfile(
        name="envbase",
        prefix="/envbase",
        style=("blue", "Env Base"),
    )
    register(base)
    profile = register_connection_profile("dyntest5", metadata={"profile": "envbase"})

    assert isinstance(profile, _EnvAwareProfile)
    assert profile.env_for_cli() == {"DYNAMIC_ENV_HOOK": "ok"}


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
# ExecutionProfile properties and methods
# ---------------------------------------------------------------------------

def _make_profile(**kwargs) -> ExecutionProfile:
    defaults = dict(name="testprofile", prefix="/test", style=("blue", "Test"), cmd=["claude"])
    defaults.update(kwargs)
    return ExecutionProfile(**defaults)


def test_render_style():
    p = _make_profile(icon="🚀", style=("red", "Rocket"))
    assert p.render_style == ("red", "🚀  Rocket")


def test_configure_with_model_not_cli():
    p = _make_profile(driver="openai_compat", model="gpt-4", base_url="http://x", api_key_env="K")
    # Set override to OpenAI so effective_connection is OpenAIConnection
    object.__setattr__(p, "_connection_override", OpenAIConnection())
    with pytest.raises(ValueError, match="não usa driver CLI"):
        p.configure_with_model("gpt-3")


def test_configure_with_model_empty_model_id():
    p = _make_profile(cmd=["claude", "--model=claude-3"])
    with pytest.raises(ValueError, match="não pode ser vazio"):
        p.configure_with_model("")


def test_configure_with_model_no_placeholder():
    p = _make_profile(cmd=["claude", "--verbose"])
    with pytest.raises(ValueError, match="não tem placeholder"):
        p.configure_with_model("gpt-4")


def test_configure_with_model_success():
    p = _make_profile(cmd=["claude", "--model=old-model", "--verbose"])
    conn = p.configure_with_model("new-model")
    assert conn.cmd == ["claude", "--model=new-model", "--verbose"]


def test_effective_cmd_openai_connection():
    p = _make_profile(cmd=["fallback"])
    object.__setattr__(p, "_connection_override", OpenAIConnection(model="gpt-4"))
    # OpenAI connection → returns list(self.cmd)
    assert p.effective_cmd() == ["fallback"]


def test_effective_prompt_as_arg_openai():
    p = _make_profile(prompt_as_arg=True)
    object.__setattr__(p, "_connection_override", OpenAIConnection())
    assert p.effective_prompt_as_arg() is True


def test_effective_output_format_openai():
    p = _make_profile(output_format="stream-json")
    object.__setattr__(p, "_connection_override", OpenAIConnection())
    assert p.effective_output_format() == "stream-json"


def test_effective_model_cli():
    p = _make_profile(model="claude-3")
    # No override, driver is "cli" → CliConnection
    assert p.effective_model() == "claude-3"


def test_effective_base_url_cli():
    p = _make_profile(base_url="http://cli-url")
    assert p.effective_base_url() == "http://cli-url"


def test_effective_api_key_env_cli():
    p = _make_profile(api_key_env="MY_KEY")
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
    with patch("quimera.profiles.base._get_connections_file", return_value=tmp_path / "conn.json"):
        result = load_connections()
    assert result == {}


def test_load_connections_file_exists(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({"agent1": {"model": "gpt-4"}}), encoding="utf-8")
    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        result = load_connections()
    assert result == {"agent1": {"model": "gpt-4"}}


def test_save_connections(tmp_path):
    f = tmp_path / "conn.json"
    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        save_connections({"a": {"model": "x"}})
    assert json.loads(f.read_text()) == {"a": {"model": "x"}}


def test_remove_connection_existing(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({"myagent": {"model": "gpt-4"}}), encoding="utf-8")
    p = _make_profile(name="myagent")
    register(p)
    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        result = remove_connection("myagent")
    assert result is True
    assert p._connection_override is None


def test_remove_connection_not_existing(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({}), encoding="utf-8")
    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        result = remove_connection("noexist")
    assert result is False


# ---------------------------------------------------------------------------
# set_connection
# ---------------------------------------------------------------------------

def test_set_connection_no_persist():
    p = _make_profile(name="sco_test1")
    register(p)
    conn = OpenAIConnection(model="gpt-4")
    with patch("quimera.profiles.base.load_connections") as mock_load:
        set_connection("sco_test1", conn, persist=False)
        mock_load.assert_not_called()
    assert p._connection_override == conn


def test_set_connection_with_persist_dynamic(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({}), encoding="utf-8")
    profile = register_connection_profile("sco_dyn1")
    conn = OpenAIConnection(model="gpt-4", base_url="http://x", api_key_env="K")
    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        set_connection("sco_dyn1", conn, persist=True)
    saved = json.loads(f.read_text())
    assert "sco_dyn1" in saved
    assert saved["sco_dyn1"]["profile"]["dynamic"] is True


# ---------------------------------------------------------------------------
# apply_connections / reload_profiles
# ---------------------------------------------------------------------------

def test_apply_connections_registers_dynamic(tmp_path):
    f = tmp_path / "conn.json"
    conn_data = {
        "type": "openai",
        "model": "gpt-4",
        "base_url": "http://x",
        "api_key_env": "K",
        "provider": "openai",
        "supports_native_tools": True,
        "extra_body": None,
        "profile": {"dynamic": True},
    }
    f.write_text(json.dumps({"newdynagent": conn_data}), encoding="utf-8")
    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        apply_connections()
    p = get("newdynagent")
    assert p is not None
    assert p._connection_override is not None


def test_apply_connections_skips_invalid(tmp_path):
    f = tmp_path / "conn.json"
    conn_data = {
        "type": "openai",
        "model": "gpt-4",
        "base_url": "http://x",
        "api_key_env": "K",
        "provider": "openai",
        "supports_native_tools": True,
        "extra_body": None,
        "profile": {"dynamic": True},
    }
    # Invalid agent name → register_connection_profile raises ValueError → skip
    f.write_text(json.dumps({"INVALID AGENT": conn_data}), encoding="utf-8")
    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        apply_connections()  # should not raise
    assert get("invalid agent") is None


def test_reload_profiles_returns_names(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({}), encoding="utf-8")
    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        names = reload_profiles()
    assert isinstance(names, list)


def test_apply_connections_uses_explicit_registry(tmp_path):
    f = tmp_path / "conn.json"
    conn_data = {
        "type": "openai",
        "model": "gpt-4",
        "base_url": "http://x",
        "api_key_env": "K",
        "provider": "openai",
        "supports_native_tools": True,
        "extra_body": None,
        "profile": {"dynamic": True},
    }
    f.write_text(json.dumps({"scoped_agent_zz99": conn_data}), encoding="utf-8")
    scoped_registry = ProfileRegistry()

    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        apply_connections(registry=scoped_registry)

    profile = scoped_registry.get("scoped_agent_zz99")
    assert profile is not None
    assert profile._connection_override is not None


def test_set_connection_uses_explicit_registry_without_persist():
    scoped_registry = ProfileRegistry()
    profile = _make_profile(name="scoped-agent")
    scoped_registry.register(profile)
    conn = OpenAIConnection(model="gpt-4")

    set_connection("scoped-agent", conn, persist=False, registry=scoped_registry)

    assert profile._connection_override == conn


def test_reload_profiles_uses_explicit_registry(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({}), encoding="utf-8")
    scoped_registry = ProfileRegistry()
    scoped_registry.register(_make_profile(name="only-local"))

    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        names = reload_profiles(registry=scoped_registry)

    assert names == ["only-local"]


def test_remove_connection_uses_explicit_registry(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({"scoped-agent": {"model": "gpt-4"}}), encoding="utf-8")
    scoped_registry = ProfileRegistry()
    profile = _make_profile(name="scoped-agent")
    object.__setattr__(profile, "_connection_override", OpenAIConnection(model="gpt-4"))
    scoped_registry.register(profile)

    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        removed = remove_connection("scoped-agent", registry=scoped_registry)

    assert removed is True
    assert profile._connection_override is None


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
    p = _make_profile(driver="openai_compat", model="gpt-4", base_url="http://x", api_key_env="MY_KEY")
    conn = p.effective_connection()
    assert isinstance(conn, OpenAIConnection)
    assert conn.model == "gpt-4"
    assert conn.provider == "openai_compat"


# ---------------------------------------------------------------------------
# effective_driver
# ---------------------------------------------------------------------------

def test_effective_driver_cli():
    p = _make_profile(cmd=["echo"])
    assert p.effective_driver() == "cli"


def test_effective_driver_openai():
    p = _make_profile(driver="openai_compat", model="gpt-4", base_url="http://x", api_key_env="K")
    assert p.effective_driver() == "openai_compat"


# ---------------------------------------------------------------------------
# effective_cmd — CliConnection branch (line 297)
# ---------------------------------------------------------------------------

def test_effective_cmd_cli_connection():
    p = _make_profile(cmd=["echo", "hi"])
    assert p.effective_cmd() == ["echo", "hi"]


# ---------------------------------------------------------------------------
# resolve_runtime_model (lines 302-303)
# ---------------------------------------------------------------------------

def test_resolve_runtime_model():
    p = _make_profile(cmd=["claude", "--model=my-model"])
    assert p.resolve_runtime_model() == "my-model"


def test_resolve_runtime_model_with_cwd():
    p = _make_profile(cmd=["claude", "--model=my-model"])
    assert p.resolve_runtime_model(cwd="/tmp") == "my-model"


# ---------------------------------------------------------------------------
# effective_prompt_as_arg — CliConnection branch (line 309)
# ---------------------------------------------------------------------------

def test_effective_prompt_as_arg_cli():
    p = _make_profile(cmd=["echo"], prompt_as_arg=True)
    assert p.effective_prompt_as_arg() is True


# ---------------------------------------------------------------------------
# effective_output_format — CliConnection branch (line 316)
# ---------------------------------------------------------------------------

def test_effective_output_format_cli():
    p = _make_profile(cmd=["echo"], output_format="json")
    assert p.effective_output_format() == "json"


# ---------------------------------------------------------------------------
# effective_model — OpenAI connection branch (line 323)
# ---------------------------------------------------------------------------

def test_effective_model_openai():
    p = _make_profile(driver="openai_compat", model="gpt-4", base_url="http://x", api_key_env="K")
    assert p.effective_model() == "gpt-4"


# ---------------------------------------------------------------------------
# effective_base_url — OpenAI connection branch (line 330)
# ---------------------------------------------------------------------------

def test_effective_base_url_openai():
    p = _make_profile(driver="openai_compat", model="gpt-4", base_url="http://custom", api_key_env="K")
    assert p.effective_base_url() == "http://custom"


# ---------------------------------------------------------------------------
# effective_api_key_env — OpenAI connection branch (line 337)
# ---------------------------------------------------------------------------

def test_effective_api_key_env_openai():
    p = _make_profile(driver="openai_compat", model="gpt-4", base_url="http://x", api_key_env="MY_VAR")
    assert p.effective_api_key_env() == "MY_VAR"


# ---------------------------------------------------------------------------
# all_profiles (line 437)
# ---------------------------------------------------------------------------

def test_all_profiles_returns_list():
    from quimera.profiles.base import all_profiles
    result = all_profiles()
    assert isinstance(result, list)
    assert all(isinstance(p, ExecutionProfile) for p in result)


# ---------------------------------------------------------------------------
# apply_connections — profile is None after register attempt (line 141)
# Uses a mock where register_connection_profile succeeds but _registry still has None
# ---------------------------------------------------------------------------

def test_apply_connections_profile_none_after_register(tmp_path):
    f = tmp_path / "conn.json"
    conn_data = {
        "type": "cli",
        "cmd": ["echo"],
        "profile": {"dynamic": True},
    }
    # Use a valid name not previously registered
    f.write_text(json.dumps({"tempagent99": conn_data}), encoding="utf-8")

    original_get = __import__("quimera.profiles.base", fromlist=["_registry"])
    call_count = [0]

    def patched_register(name, metadata=None, registry=None):
        # Register succeeds but we simulate the next _registry.get returning None
        pass  # Don't actually register — so profile stays None

    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        with patch("quimera.profiles.base.register_connection_profile", side_effect=patched_register):
            apply_connections()  # profile will be None → hits line 141 continue


# ---------------------------------------------------------------------------
# set_connection — dynamic profile with base_ref (line 183)
# ---------------------------------------------------------------------------

def test_set_connection_dynamic_with_base_ref(tmp_path):
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({}), encoding="utf-8")
    # Register a base
    base = ExecutionProfile(name="mybase2", prefix="/mybase2", style=("green", "Base2"))
    register(base)
    # Register dynamic with base reference
    profile = register_connection_profile("dynwithbase1", metadata={"profile": "mybase2"})
    assert profile._profile_name == "mybase2"

    conn = OpenAIConnection(model="gpt-4", base_url="http://x", api_key_env="K")
    with patch("quimera.profiles.base._get_connections_file", return_value=f):
        set_connection("dynwithbase1", conn, persist=True)

    saved = json.loads(f.read_text())
    assert saved["dynwithbase1"]["profile"]["profile"] == "mybase2"


# ---------------------------------------------------------------------------
# ProfileRegistry
# ---------------------------------------------------------------------------

def _make_profile_for_registry(name: str = "regtest") -> ExecutionProfile:
    return ExecutionProfile(name=name, prefix=f"/{name}", style=("blue", name))


def test_profile_registry_register_and_get():
    registry = ProfileRegistry()
    p = _make_profile_for_registry("p1")
    registry.register(p)
    assert registry.get("p1") is p


def test_profile_registry_get_nonexistent():
    registry = ProfileRegistry()
    assert registry.get("nope") is None


def test_profile_registry_all_names():
    registry = ProfileRegistry()
    registry.register(_make_profile_for_registry("b"))
    registry.register(_make_profile_for_registry("a"))
    assert registry.all_names() == ["b", "a"]


def test_profile_registry_all_profiles():
    registry = ProfileRegistry()
    p = _make_profile_for_registry("p1")
    registry.register(p)
    result = registry.all_profiles()
    assert result == [p]


def test_profile_registry_isolation():
    r1 = ProfileRegistry()
    r2 = ProfileRegistry()
    r1.register(_make_profile_for_registry("shared"))
    assert r2.get("shared") is None


def test_default_registry_is_profile_registry_instance():
    from quimera.profiles.base import _registry
    assert isinstance(_registry, ProfileRegistry)


def test_register_get_all_names_all_profiles_delegate_to_default():
    p = _make_profile_for_registry("delegated")
    register(p)
    assert get("delegated") is p
    assert "delegated" in all_names()
    assert p in all_profiles()

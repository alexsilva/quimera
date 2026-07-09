import io
import builtins
import importlib.util
import os
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import quimera.cli as cli
from quimera.profiles.base import CliConnection, OpenAIConnection
from quimera.runtime.mcp.http_server import DEFAULT_HTTP_READ_ONLY_TOOLS


_ORIGINAL_DEPENDENCY_CHECK = cli._ensure_required_runtime_dependencies


class _FakeWorkspace:
    def __init__(self, cwd):
        self.cwd = cwd
        self.config_file = Path("/tmp/quimera-test-config.json")
        self.mcp_config_file = Path("/tmp/quimera-test-workspace-mcp-config.json")
        self.tmp = SimpleNamespace(root=Path("/tmp/quimera-test-tmp"))


class _FakeConfig:
    last_instance = None

    def __init__(self, config_file):
        self.config_file = config_file
        self.user_name = "Tester"
        self.theme_set = None
        self.history_window_set = None
        _FakeConfig.last_instance = self

    def set_user_name(self, name):
        self.user_name = name

    def set_theme(self, theme):
        self.theme_set = theme

    def set_history_window(self, value):
        self.history_window_set = value

    @property
    def mcp_clients(self):
        return self._mcp_clients if hasattr(self, "_mcp_clients") else None

    @property
    def mcp_client_env(self):
        return self._mcp_client_env if hasattr(self, "_mcp_client_env") else None

    def set_mcp_clients(self, value):
        self._mcp_clients = value

    def set_mcp_client_env(self, value):
        self._mcp_client_env = value


class _FakeApp:
    last_instance = None

    def __init__(self, cwd, **kwargs):
        self.cwd = cwd
        self.kwargs = kwargs
        self.ran = False
        self.tool_executor = object()
        self.workspace = kwargs.get("workspace")
        self.mcp_socket_calls: list[str | None] = []
        self.mcp_socket_tokens: list[str | None] = []
        self.mcp_http_calls: list[str | None] = []
        self.mcp_http_tokens: list[str | None] = []
        _FakeApp.last_instance = self

    def run(self):
        self.ran = True

    def configure_mcp_socket(self, socket_path: str | None, token: str | None = None) -> None:
        self.mcp_socket_calls.append(socket_path)
        self.mcp_socket_tokens.append(token)

    def configure_mcp_http(self, url: str | None, token: str | None = None) -> None:
        self.mcp_http_calls.append(url)
        self.mcp_http_tokens.append(token)


@pytest.fixture(autouse=True)
def _skip_required_dependency_check(monkeypatch):
    monkeypatch.setattr(cli, "_ensure_required_runtime_dependencies", lambda: None)


def _patch_main_basics(monkeypatch, *, agent_names=None, theme_names=None):
    monkeypatch.setattr(cli, "Workspace", _FakeWorkspace)
    monkeypatch.setattr(cli, "ConfigManager", _FakeConfig)
    monkeypatch.setattr(cli, "QuimeraApp", _FakeApp)
    monkeypatch.setattr(cli._profiles, "all_names", lambda: agent_names or ["claude"])
    monkeypatch.setattr(cli, "load_connections", lambda: {name: {"type": "cli", "cmd": [name]} for name in (agent_names or ["claude"])})
    monkeypatch.setattr(cli._themes, "names", lambda: theme_names or ["default"])
    monkeypatch.setattr(cli, "_ensure_required_runtime_dependencies", lambda: None)



def test_required_dependency_check_fails_fast_for_missing_openai(monkeypatch):
    """Verifica que required dependency check fails fast for missing openai."""
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None if name == "openai" else object())

    with pytest.raises(SystemExit) as exc:
        _ORIGINAL_DEPENDENCY_CHECK()

    assert exc.value.code == "Instalação incompleta: dependência obrigatória 'openai' não encontrada. Reinstale o projeto com: pip install -e ."


def test_required_dependency_check_reports_all_missing_packages(monkeypatch):
    """Verifica que required dependency check reports all missing packages."""
    missing_modules = {"openai", "rich", "textual"}
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None if name in missing_modules else object())

    with pytest.raises(SystemExit) as exc:
        _ORIGINAL_DEPENDENCY_CHECK()

    assert exc.value.code == (
        "Instalação incompleta: dependências obrigatórias 'openai', 'rich', 'textual' "
        "não encontradas. Reinstale o projeto com: pip install -e ."
    )


def test_main_help_does_not_check_required_runtime_dependencies(monkeypatch, capsys):
    """Verifica que main help does not check required runtime dependencies."""
    monkeypatch.setattr(sys, "argv", ["quimera", "--help"])
    monkeypatch.setattr(cli, "_ensure_required_runtime_dependencies", lambda: (_ for _ in ()).throw(AssertionError("should not run")))

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert "usage: quimera" in capsys.readouterr().out

def test_prompt_text_uses_default_when_empty(monkeypatch):
    """Verifica que prompt text uses default when empty."""
    monkeypatch.setattr(cli, "_read_input", lambda _text: "")

    assert cli._prompt_text("Label", "padrao") == "padrao"


def test_prompt_text_returns_non_empty_input(monkeypatch):
    """Verifica que prompt text returns non empty input."""
    monkeypatch.setattr(cli, "_read_input", lambda _text: "valor")

    assert cli._prompt_text("Label", "padrao") == "valor"


def test_prompt_bool_reprompts_on_invalid_then_accepts_yes(monkeypatch):
    """Verifica que prompt bool reprompts on invalid then accepts yes."""
    answers = iter(["talvez", "sim"])
    monkeypatch.setattr(cli, "_read_input", lambda _text: next(answers))

    with patch("builtins.print") as mock_print:
        assert cli._prompt_bool("Confirma", default=False) is True

    mock_print.assert_called_once_with("Valor inválido. Use 's' ou 'n'.")


def test_prompt_bool_uses_default_when_empty(monkeypatch):
    """Verifica que prompt bool uses default when empty."""
    monkeypatch.setattr(cli, "_read_input", lambda _text: "")
    assert cli._prompt_bool("Confirma", default=True) is True


def test_prompt_bool_accepts_negative_forms(monkeypatch):
    """Verifica que prompt bool accepts negative forms."""
    monkeypatch.setattr(cli, "_read_input", lambda _text: "não")
    assert cli._prompt_bool("Confirma", default=True) is False


def test_cli_import_fallback_without_ui_dependencies():
    """Verifica que cli import fallback without ui dependencies."""
    real_import = builtins.__import__

    def _import(name, *args, **kwargs):
        if name in {"ui", "agents", "quimera.ui", "quimera.agents"}:
            raise ImportError("interactive deps missing")
        return real_import(name, *args, **kwargs)

    spec = importlib.util.spec_from_file_location("quimera.cli_no_ui", cli.__file__)
    module = importlib.util.module_from_spec(spec)
    with patch("builtins.__import__", side_effect=_import):
        spec.loader.exec_module(module)

    assert module.TerminalRenderer is None
    assert module.AgentClient is None


def test_configure_connection_interactively_cli_branch(monkeypatch):
    """Verifica que configure connection interactively cli branch."""
    profile = SimpleNamespace(
        cmd=["codex", "exec"],
        model=None,
        base_url=None,
        api_key_env=None,
        driver="cli",
        supports_tools=False,
        effective_connection=lambda: CliConnection(cmd=["codex"], prompt_as_arg=False, output_format="text"),
    )
    # output_format (empty=keep), cmd
    answers = iter(["", "codex --ask"])
    monkeypatch.setattr(cli, "_prompt_text", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(cli, "_prompt_bool", lambda *_args, **_kwargs: True)

    conn = cli._configure_connection_interactively(profile, driver_hint="cli")

    assert isinstance(conn, CliConnection)
    assert conn.cmd == ["codex", "--ask"]
    assert conn.prompt_as_arg is True


def test_configure_connection_interactively_cli_empty_cmd_raises(monkeypatch):
    """Verifica que configure connection interactively cli empty cmd raises."""
    profile = SimpleNamespace(
        cmd=[],
        model=None,
        base_url=None,
        api_key_env=None,
        driver="cli",
        supports_tools=False,
        effective_connection=lambda: CliConnection(cmd=[]),
    )
    monkeypatch.setattr(cli, "_prompt_text", lambda *_args, **_kwargs: "")
    with pytest.raises(SystemExit, match="comando CLI vazio"):
        cli._configure_connection_interactively(profile, driver_hint="cli")


def test_configure_connection_interactively_openai_invalid_driver_then_json_error(monkeypatch):
    """Verifica que configure connection interactively openai invalid driver then json error."""
    profile = SimpleNamespace(
        cmd=["codex"],
        model="gpt-x",
        base_url="https://example.test/v1",
        api_key_env="MY_KEY",
        driver="openai",
        supports_tools=True,
        effective_connection=lambda: CliConnection(cmd=["codex"]),
    )
    answers = iter(
        [
            "invalid",        # driver inicial inválido
            "openai",         # retry do driver
            "",               # provider (aceita default "openai_compat")
            "",               # model (usa default)
            "",               # base_url (usa default)
            "",               # api_key_env (usa default)
            "{invalid json}", # extra_body_raw inválido
            "",               # max_connections (usa default)
        ]
    )
    monkeypatch.setattr(cli, "_prompt_text", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(cli, "_prompt_bool", lambda *_args, **_kwargs: True)

    with patch("builtins.print") as mock_print:
        conn = cli._configure_connection_interactively(profile)

    assert isinstance(conn, OpenAIConnection)
    assert conn.model == "gpt-x"
    assert conn.base_url == "https://example.test/v1"
    assert conn.api_key_env == "MY_KEY"
    assert conn.provider == "openai_compat"
    assert conn.extra_body is None
    assert any("Driver inválido. Use 'cli' ou 'openai'." in c.args[0] for c in mock_print.call_args_list)
    assert any("JSON inválido:" in c.args[0] for c in mock_print.call_args_list)


def test_configure_connection_interactively_openai_empty_json_clears_extra_body(monkeypatch):
    """Verifica que configure connection interactively openai empty json clears extra body."""
    current = OpenAIConnection(
        model="gpt-cur",
        base_url="https://cur",
        api_key_env="CUR_KEY",
        provider="openai",
        supports_native_tools=True,
        extra_body={"thinking": {"type": "enabled"}},
    )
    profile = SimpleNamespace(
        cmd=["codex"],
        model="fallback-model",
        base_url="https://fallback",
        api_key_env="FALLBACK_KEY",
        driver="openai",
        supports_tools=True,
        effective_connection=lambda: current,
    )
    answers = iter(
        [
            "",     # provider (aceita default "openai")
            "",     # model: mantém default
            "",     # base_url
            "",     # api_key_env
            "{}",   # extra_body_raw: limpa (empty object → None)
            "",     # max_connections
        ]
    )
    monkeypatch.setattr(cli, "_prompt_text", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(cli, "_prompt_bool", lambda *_args, **_kwargs: True)

    conn = cli._configure_connection_interactively(profile, driver_hint="openai")

    assert isinstance(conn, OpenAIConnection)
    assert conn.extra_body is None
    assert conn.provider == "openai"


def test_configure_connection_interactively_openai_empty_input_preserves_extra_body(monkeypatch):
    """Verifica que configure connection interactively openai empty input preserves extra body."""
    current = OpenAIConnection(
        model="gpt-cur",
        base_url="https://cur",
        api_key_env="CUR_KEY",
        provider="openai_compat",
        supports_native_tools=True,
        extra_body={"thinking": {"type": "enabled"}},
    )
    profile = SimpleNamespace(
        cmd=["codex"],
        model="fallback-model",
        base_url="https://fallback",
        api_key_env="FALLBACK_KEY",
        driver="openai",
        supports_tools=True,
        effective_connection=lambda: current,
    )
    answers = iter(
        [
            "",   # provider (aceita default "openai_compat")
            "",   # model
            "",   # base_url
            "",   # api_key_env
            "",   # extra_body_raw vazio: preserva atual
            "",   # max_connections
        ]
    )
    monkeypatch.setattr(cli, "_prompt_text", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(cli, "_prompt_bool", lambda *_args, **_kwargs: True)

    conn = cli._configure_connection_interactively(profile, driver_hint="openai")

    assert isinstance(conn, OpenAIConnection)
    assert conn.extra_body == {"thinking": {"type": "enabled"}}


def test_build_connection_base_profile_not_found_raises(monkeypatch):
    """Verifica que build connection profile not found raises."""
    profile = SimpleNamespace()
    args = Namespace(profile="inexistente", model="gpt-4o", driver=None, cmd=None, extra_body=None, base_url=None, api_key_env=None)
    monkeypatch.setattr(cli._profiles, "get", lambda _name: None)

    with pytest.raises(SystemExit, match="Perfil de execução 'inexistente' não encontrado"):
        cli._build_connection_from_args(profile, args)


def test_build_connection_base_profile_value_error_becomes_system_exit(monkeypatch):
    """Verifica que build connection profile value error becomes system exit."""
    class _BaseProfile:
        def configure_with_model(self, _model):
            raise ValueError("modelo inválido")

    profile = SimpleNamespace()
    args = Namespace(profile="base-x", model="gpt-4o", driver=None, cmd=None, extra_body=None, base_url=None, api_key_env=None)
    monkeypatch.setattr(cli._profiles, "get", lambda _name: _BaseProfile())

    with pytest.raises(SystemExit, match="modelo inválido"):
        cli._build_connection_from_args(profile, args)


def test_build_connection_without_driver_uses_interactive(monkeypatch):
    """Verifica que build connection without driver uses interactive."""
    profile = SimpleNamespace()
    args = Namespace(profile=None, model=None, driver=None, cmd=None, extra_body=None, base_url=None, api_key_env=None)

    with patch("quimera.cli._configure_connection_interactively", return_value="ok") as mocked:
        assert cli._build_connection_from_args(profile, args) == "ok"

    mocked.assert_called_once_with(profile)


def test_build_connection_cli_with_cmd_returns_cli_connection():
    """Verifica que build connection cli with cmd returns cli connection."""
    profile = SimpleNamespace()
    args = Namespace(profile=None, model=None, driver="cli", cmd=["codex", "exec"], extra_body=None, base_url=None, api_key_env=None)

    conn = cli._build_connection_from_args(profile, args)

    assert isinstance(conn, CliConnection)
    assert conn.cmd == ["codex", "exec"]
    assert conn.prompt_as_arg is False
    assert conn.output_format is None


def test_build_connection_cli_with_cmd_inherits_profile_output_format():
    """Conexão CLI explícita preserva parser do perfil de execução."""
    profile = SimpleNamespace(effective_output_format=lambda: "opencode-json")
    args = Namespace(profile=None, model=None, driver="cli", cmd=["opencode", "run"], extra_body=None, base_url=None, api_key_env=None)

    conn = cli._build_connection_from_args(profile, args)

    assert isinstance(conn, CliConnection)
    assert conn.output_format == "opencode-json"


def test_build_connection_cli_without_cmd_falls_back_to_interactive(monkeypatch):
    """Verifica que build connection cli without cmd falls back to interactive."""
    profile = SimpleNamespace()
    args = Namespace(profile=None, model=None, driver="cli", cmd=None, extra_body=None, base_url=None, api_key_env=None)

    with patch("quimera.cli._configure_connection_interactively", return_value="interactive") as mocked:
        assert cli._build_connection_from_args(profile, args) == "interactive"

    mocked.assert_called_once_with(profile, driver_hint="cli")


def test_main_rejects_non_positive_history_window(monkeypatch):
    """Verifica que main rejects non positive history window."""
    monkeypatch.setattr(sys, "argv", ["quimera", "--history-window", "0"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2


def test_main_rejects_non_positive_set_history_window(monkeypatch):
    """`--set-history-window` deve rejeitar valores menores ou iguais a zero."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--set-history-window", "0"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2


def test_main_set_history_window_persists_and_exits(monkeypatch):
    """`--set-history-window` persiste a config e encerra sem iniciar a app interativa."""
    _patch_main_basics(monkeypatch)
    _FakeApp.last_instance = None
    _FakeConfig.last_instance = None
    monkeypatch.setattr(sys, "argv", ["quimera", "--set-history-window", "96"])

    with patch("builtins.print") as mock_print:
        cli.main()

    assert _FakeConfig.last_instance is not None
    assert _FakeConfig.last_instance.history_window_set == 96
    assert _FakeApp.last_instance is None
    mock_print.assert_called_once_with("History window definida: 96")


def test_main_list_connections_empty_prints_message(monkeypatch):
    """Verifica que main list connections empty prints message."""
    monkeypatch.setattr(sys, "argv", ["quimera", "--list-connections"])
    monkeypatch.setattr(cli, "load_connections", lambda: {})

    with patch("builtins.print") as mock_print:
        cli.main()

    mock_print.assert_called_once_with("Nenhuma conexão persistida.")


def test_main_list_connections_prints_each_entry(monkeypatch):
    """Verifica que main list connections prints each entry."""
    monkeypatch.setattr(sys, "argv", ["quimera", "--list-connections"])
    monkeypatch.setattr(cli, "load_connections", lambda: {"codex": {"type": "cli", "cmd": ["codex"]}})
    monkeypatch.setattr(cli, "_connection_from_dict", lambda _data: CliConnection(cmd=["codex"]))
    monkeypatch.setattr(cli, "format_connection_label", lambda _conn: "cli: codex")

    with patch("builtins.print") as mock_print:
        cli.main()

    mock_print.assert_called_once_with("codex: cli: codex")


def test_available_agent_names_uses_persisted_connections_not_profiles(monkeypatch):
    """Perfis de execução não ativam agentes por si só."""
    monkeypatch.setattr(cli._profiles, "all_names", lambda: ["claude", "codex", "opencode"])
    monkeypatch.setattr(cli, "load_connections", lambda: {})

    assert cli._available_agent_names() == []


def test_available_agent_names_returns_connection_names(monkeypatch):
    """Agentes disponíveis são conexões nomeadas persistidas."""
    monkeypatch.setattr(cli._profiles, "all_names", lambda: ["claude", "codex", "opencode"])
    monkeypatch.setattr(
        cli,
        "load_connections",
        lambda: {
            "alice": {"type": "cli", "cmd": ["codex"]},
            "bob": {"type": "openai", "model": "gpt-4o"},
        },
    )

    assert cli._available_agent_names() == ["alice", "bob"]


def test_main_connect_registers_dynamic_profile_and_saves_override(monkeypatch):
    """Verifica que main connect registers dynamic profile and saves override."""
    dynamic_profile = SimpleNamespace(effective_connection=lambda: CliConnection(cmd=["builtin"]))

    monkeypatch.setattr(sys, "argv", ["quimera", "--connect", "novo-agente", "--driver", "cli", "--cmd", "novo-cli"])
    monkeypatch.setattr(cli._profiles, "get", lambda _name: None)
    monkeypatch.setattr(cli, "is_valid_agent_name", lambda _name: True)

    with patch("quimera.cli.register_connection_profile", return_value=dynamic_profile) as register_dynamic, \
            patch("quimera.cli.set_connection") as set_override, \
            patch("quimera.cli.format_connection_label", return_value="cli: ok"), \
            patch("builtins.print") as mock_print:
        cli.main()

    register_dynamic.assert_called_once_with("novo-agente", metadata=None)
    set_override.assert_called_once()
    printed_messages = [call.args[0] for call in mock_print.call_args_list]
    assert any("Conexão registrada: novo-agente" in msg for msg in printed_messages)
    assert any("Conexão salva em base_dir para novo-agente" in msg for msg in printed_messages)


def test_main_connect_invalid_agent_name_in_connect_errors(monkeypatch):
    """Verifica que main connect invalid agent name in connect errors."""
    monkeypatch.setattr(sys, "argv", ["quimera", "--connect", "Inválido!", "--driver", "cli", "--cmd", "x"])
    monkeypatch.setattr(cli._profiles, "get", lambda _name: None)
    monkeypatch.setattr(cli, "is_valid_agent_name", lambda _name: False)

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_main_connect_new_agent_with_missing_base_profile_errors(monkeypatch):
    """Verifica que main connect new agent with missing profile errors."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["quimera", "--connect", "novo-agente", "--profile", "base-missing", "--driver", "cli", "--cmd", "x"],
    )

    def fake_get(_name):
        return None

    monkeypatch.setattr(cli._profiles, "get", fake_get)
    monkeypatch.setattr(cli, "is_valid_agent_name", lambda _name: True)

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_main_connect_new_agent_with_base_profile_sets_metadata(monkeypatch):
    """Verifica que main connect new agent with profile sets metadata."""
    dynamic_profile = SimpleNamespace(effective_connection=lambda: CliConnection(cmd=["builtin"]))
    base_profile = SimpleNamespace(name="base-ok")
    calls = []

    def fake_get(name):
        calls.append(name)
        if name == "novo-agente":
            return None
        if name == "base-ok":
            return base_profile
        return None

    monkeypatch.setattr(
        sys,
        "argv",
        ["quimera", "--connect", "novo-agente", "--profile", "base-ok", "--driver", "cli", "--cmd", "novo-cli"],
    )
    monkeypatch.setattr(cli._profiles, "get", fake_get)
    monkeypatch.setattr(cli, "is_valid_agent_name", lambda _name: True)

    with patch("quimera.cli.register_connection_profile", return_value=dynamic_profile) as register_dynamic, \
            patch("quimera.cli.set_connection"), \
            patch("quimera.cli.format_connection_label", return_value="cli: ok"), \
            patch("builtins.print"):
        cli.main()

    register_dynamic.assert_called_once_with("novo-agente", metadata={"profile": "base-ok"})


def test_main_connect_profile_model_uses_model_flag(monkeypatch):
    """--model funciona para perfis CLI com suporte a modelo."""
    base_profile = SimpleNamespace(
        name="codex",
        configure_with_model=lambda model: CliConnection(
            cmd=["codex", "exec", "--model", model, "--json"],
            output_format="codex-json",
        ),
        effective_connection=lambda: CliConnection(cmd=["codex", "exec", "--json"]),
    )
    dynamic_profile = SimpleNamespace(effective_connection=lambda: CliConnection(cmd=["codex", "exec", "--json"]))

    def fake_get(name):
        if name == "codex-gpt-5-5":
            return None
        if name == "codex":
            return base_profile
        return None

    monkeypatch.setattr(
        sys,
        "argv",
        ["quimera", "--connect", "codex-gpt-5-5", "--profile", "codex", "--model", "gpt-5.5"],
    )
    monkeypatch.setattr(cli._profiles, "get", fake_get)
    monkeypatch.setattr(cli, "is_valid_agent_name", lambda _name: True)

    with patch("quimera.cli.register_connection_profile", return_value=dynamic_profile) as register_dynamic, \
            patch("quimera.cli.set_connection") as set_override, \
            patch("quimera.cli.format_connection_label", return_value="cli: ok"), \
            patch("builtins.print"):
        cli.main()

    register_dynamic.assert_called_once_with("codex-gpt-5-5", metadata={"profile": "codex"})
    saved_connection = set_override.call_args.args[1]
    assert isinstance(saved_connection, CliConnection)
    assert saved_connection.cmd == ["codex", "exec", "--model", "gpt-5.5", "--json"]
    assert saved_connection.output_format == "codex-json"


def test_main_connect_existing_profile_inherits_base_settings(monkeypatch):
    """Verifica que main connect existing profile inherits base settings."""
    base_formatter = lambda text: text
    existing_profile = SimpleNamespace(
        dynamic=True,
        effective_connection=lambda: CliConnection(cmd=["existing"]),
        spy_stdout_formatter=None,
        runtime_rw_paths=[],
    )
    inherited_profile = SimpleNamespace(
        effective_connection=lambda: CliConnection(cmd=["existing"]),
        effective_output_format=lambda: None,
    )
    base_profile = SimpleNamespace(name="base-ref", spy_stdout_formatter=base_formatter, runtime_rw_paths=["/tmp/rw"])

    def fake_get(name):
        if name == "codex":
            return existing_profile
        if name == "base-ref":
            return base_profile
        return None

    monkeypatch.setattr(sys, "argv", ["quimera", "--connect", "codex", "--profile", "base-ref", "--driver", "cli", "--cmd", "codex-cli"])
    monkeypatch.setattr(cli._profiles, "get", fake_get)

    with patch("quimera.cli.set_connection") as set_override, \
            patch("quimera.cli.register_connection_profile", return_value=inherited_profile) as register_dynamic, \
            patch("quimera.cli.format_connection_label", return_value="cli: ok"), \
            patch("builtins.print"):
        cli.main()

    register_dynamic.assert_called_once_with("codex", metadata={"profile": "base-ref"})
    set_override.assert_called_once()


def test_main_connect_existing_profile_with_missing_base_errors(monkeypatch):
    """Verifica que main connect existing profile with missing base errors."""
    existing_profile = SimpleNamespace(
        dynamic=True,
        effective_connection=lambda: CliConnection(cmd=["existing"]),
        spy_stdout_formatter=None,
        runtime_rw_paths=[],
    )

    def fake_get(name):
        if name == "codex":
            return existing_profile
        return None

    monkeypatch.setattr(sys, "argv", ["quimera", "--connect", "codex", "--profile", "base-inexistente", "--driver", "cli", "--cmd", "codex-cli"])
    monkeypatch.setattr(cli._profiles, "get", fake_get)

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_main_driver_repl_runtime_error_returns_exit_2(monkeypatch):
    """Verifica que main driver repl runtime error returns exit 2."""
    class _FailingRepl:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, one_shot_prompt=None):
            raise RuntimeError("falhou")

    monkeypatch.setattr(sys, "argv", ["quimera", "--driver-repl", "ollama-qwen"])
    monkeypatch.setattr(cli, "DriverRepl", _FailingRepl)

    fake_stderr = io.StringIO()
    with patch("sys.stderr", fake_stderr):
        with pytest.raises(SystemExit) as exc:
            cli.main()

    assert exc.value.code == 2
    assert "falhou" in fake_stderr.getvalue()

def test_main_excludes_fake_agents_without_test_mode(monkeypatch):
    """Verifica que main excludes fake agents without test mode."""
    _patch_main_basics(monkeypatch, agent_names=["claude", "fake-cli", "fake-cli-delegate", "fake-openai", "fake-openai-mcp-cli"])
    monkeypatch.setattr(sys, "argv", ["quimera"])

    with patch("quimera.runtime.mcp.session.MCPServer"):
        cli.main()

    assert _FakeApp.last_instance.kwargs["agents"] == ["claude"]


def test_main_test_mode_uses_only_fake_agents(monkeypatch):
    """Verifica que main test mode uses only fake agents."""
    class FakeBackend:
        stopped = False

        def shutdown(self):
            self.stopped = True

        def server_close(self):
            self.stopped = True

    backend = FakeBackend()
    _patch_main_basics(monkeypatch, agent_names=["claude", "fake-cli", "fake-cli-delegate", "fake-openai", "fake-openai-mcp-cli"])
    monkeypatch.setattr(cli._profiles, "enable_test_profiles", lambda: cli._profiles.TEST_PROFILE_NAMES)
    monkeypatch.setattr(cli, "_start_test_fake_openai_backend", lambda: backend)
    monkeypatch.setattr(sys, "argv", ["quimera", "--test"])

    with patch("quimera.runtime.mcp.session.MCPServer"):
        cli.main()

    assert _FakeApp.last_instance.kwargs["agents"] == ["fake-cli", "fake-cli-delegate", "fake-openai", "fake-openai-mcp-cli"]
    assert backend.stopped is True


def test_main_test_mode_with_cli_only_fake_agent_does_not_start_openai_backend(monkeypatch):
    """Verifica que main test mode with cli only fake agent does not start openai backend."""
    _patch_main_basics(monkeypatch, agent_names=["fake-cli", "fake-openai"])
    monkeypatch.setattr(cli._profiles, "enable_test_profiles", lambda: cli._profiles.TEST_PROFILE_NAMES)
    monkeypatch.setattr(cli, "_start_test_fake_openai_backend", lambda: (_ for _ in ()).throw(AssertionError("should not start")))
    monkeypatch.setattr(sys, "argv", ["quimera", "--test", "--agents", "fake-cli"])

    with patch("quimera.runtime.mcp.session.MCPServer"):
        cli.main()

    assert _FakeApp.last_instance.kwargs["agents"] == ["fake-cli"]


def test_start_test_fake_openai_backend_uses_free_port_and_non_persistent_override(monkeypatch):
    """Verifica que start test fake openai backend uses free port and non persistent override."""
    captured = {}

    def fake_set_connection(agent_name, connection, persist=True):
        captured["agent_name"] = agent_name
        captured["connection"] = connection
        captured["persist"] = persist

    monkeypatch.setattr(cli, "set_connection", fake_set_connection)
    backend = cli._start_test_fake_openai_backend()
    try:
        connection = captured["connection"]
        assert captured["agent_name"] == "fake-openai"
        assert captured["persist"] is False
        assert connection.model == "quimera-fake-tools"
        assert connection.base_url.startswith("http://127.0.0.1:")
        assert connection.base_url.endswith("/v1")
        assert connection.base_url != "http://127.0.0.1:8765/v1"
        assert connection.api_key_env == "QUIMERA_FAKE_API_KEY"
        assert connection.provider == "openai_compat"
    finally:
        cli._stop_test_fake_openai_backend(backend)


def test_main_connect_fake_profile_is_blocked_even_in_test_mode(monkeypatch):
    """Verifica que main connect fake profile is blocked even in test mode."""
    _patch_main_basics(monkeypatch, agent_names=["fake-openai"])
    monkeypatch.setattr(sys, "argv", ["quimera", "--test", "--connect", "fake-openai", "--driver", "openai", "--model", "m"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2


def test_main_fake_driver_repl_requires_test_mode(monkeypatch):
    """Verifica que main fake driver repl requires test mode."""
    _patch_main_basics(monkeypatch, agent_names=["claude"])
    monkeypatch.setattr(sys, "argv", ["quimera", "--driver-repl", "fake-openai"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2

def test_main_rejects_unknown_agents(monkeypatch):
    """Verifica que main rejects unknown agents."""
    _patch_main_basics(monkeypatch, agent_names=["claude"])
    monkeypatch.setattr(sys, "argv", ["quimera", "--agents", "desconhecido"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2


def test_main_name_sets_user_name_and_returns(monkeypatch):
    """Verifica que main name sets user name and returns."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--name", "Ana", "Silva"])

    with patch("builtins.print") as mock_print:
        cli.main()

    mock_print.assert_called_once_with("Nome configurado: Ana Silva")


def test_main_whoami_prints_current_user_name(monkeypatch):
    """Verifica que main whoami prints current user name."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--whoami"])

    with patch("builtins.print") as mock_print:
        cli.main()

    mock_print.assert_called_once_with("Tester")


def test_main_set_theme_persists_and_prints(monkeypatch):
    """Verifica que main set theme persists and prints."""
    _patch_main_basics(monkeypatch, theme_names=["sunny"])
    monkeypatch.setattr(sys, "argv", ["quimera", "--set-theme", "sunny"])
    monkeypatch.setattr(cli._themes, "get", lambda _name: SimpleNamespace(name="sunny", description="Tema claro"))

    with patch("builtins.print") as mock_print:
        cli.main()

    mock_print.assert_called_once_with("Tema padrão definido: sunny — Tema claro")


def test_main_interactive_test_requires_terminal_renderer_and_agent_client(monkeypatch):
    """Verifica que main interactive test requires terminal renderer and agent client."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--interactive-test"])
    monkeypatch.setattr(cli, "TerminalRenderer", None)
    monkeypatch.setattr(cli, "AgentClient", None)

    with pytest.raises(RuntimeError, match="Modo interativo não disponível"):
        cli.main()


def test_main_ignores_stdin_reconfigure_errors_and_still_runs(monkeypatch):
    """Verifica que main ignores stdin reconfigure errors and still runs."""
    class _BrokenStdin:
        encoding = None

        def fileno(self):
            return 0

        def reconfigure(self, **_kwargs):
            raise ValueError("boom")

    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _BrokenStdin())
    monkeypatch.setattr(cli.os, "device_encoding", lambda _fd: None)
    monkeypatch.setattr(sys, "argv", ["quimera"])

    with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls:
        mock_mcp = mock_mcp_cls.return_value
        cli.main()

    assert _FakeApp.last_instance is not None
    assert _FakeApp.last_instance.ran is True
    called_path = mock_mcp.start_background.call_args[0][0]
    assert called_path.startswith("/tmp/quimera-test-tmp/mcp-")
    assert called_path.endswith(".sock")


def test_main_mcp_uses_workspace_tmp_and_configures_profiles(monkeypatch):
    """Verifica que main mcp uses workspace tmp and configures profiles."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-socket"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls:
        mock_mcp = mock_mcp_cls.return_value
        cli.main()

    called_path = mock_mcp.start_background.call_args[0][0]
    assert called_path.startswith("/tmp/quimera-test-tmp/mcp-")
    assert called_path.endswith(".sock")
    assert _FakeApp.last_instance.mcp_socket_calls == [called_path]


def test_main_mcp_client_uses_workspace_config(monkeypatch):
    """Conexões MCP externas não usam a configuração global do usuário."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--no-mcp"])
    captured = {}
    monkeypatch.setattr(cli, "start_mcp_clients", lambda **kwargs: captured.update(kwargs))

    cli.main()

    assert captured["config"].config_file == Path(
        "/tmp/quimera-test-workspace-mcp-config.json"
    )
    assert captured["config"].config_file != Path("/tmp/quimera-test-config.json")


def test_main_no_mcp_disables_mcp(monkeypatch):
    """Verifica que main no mcp disables mcp."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--no-mcp"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls:
        cli.main()

    mock_mcp_cls.assert_not_called()
    assert _FakeApp.last_instance.mcp_socket_calls == [None]


def test_main_mcp_uses_explicit_socket_path(monkeypatch):
    """Verifica que main mcp uses explicit socket path."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-socket", "/tmp/custom-mcp.sock"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls:
        mock_mcp = mock_mcp_cls.return_value
        cli.main()

    mock_mcp.start_background.assert_called_once_with("/tmp/custom-mcp.sock")


def test_main_mcp_updates_prompt_builder_session_state(monkeypatch):
    """Verifica que main mcp updates prompt builder session state."""
    class _FakeAppWithPromptBuilder(_FakeApp):
        def __init__(self, cwd, **kwargs):
            super().__init__(cwd, **kwargs)
            self.prompt_builder = SimpleNamespace(session_state={"session_id": "sessao-teste"})

    monkeypatch.setattr(cli, "Workspace", _FakeWorkspace)
    monkeypatch.setattr(cli, "ConfigManager", _FakeConfig)
    monkeypatch.setattr(cli, "QuimeraApp", _FakeAppWithPromptBuilder)
    monkeypatch.setattr(cli._profiles, "all_names", lambda: ["claude"])
    monkeypatch.setattr(cli._themes, "names", lambda: ["default"])
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-socket", "/tmp/custom-mcp.sock"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer"):
        cli.main()

    app = _FakeAppWithPromptBuilder.last_instance
    assert app is not None
    assert app.prompt_builder.session_state["mcp_enabled"] is True
    assert app.prompt_builder.session_state["mcp_socket_path"] == "/tmp/custom-mcp.sock"


def test_main_mcp_http_adds_external_http_without_replacing_internal_socket(monkeypatch):
    """Verifica que main mcp http adds external http without replacing internal socket."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-http", "--mcp-host", "127.0.0.1", "--mcp-port", "9090"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls, patch("quimera.runtime.mcp.session.MCP_HTTPServer") as mock_http_cls:
        internal_mcp = mock_mcp_cls.return_value
        external_mcp = mock_mcp_cls.return_value
        mock_http = mock_http_cls.return_value
        cli.main()

    assert mock_mcp_cls.call_count == 2
    called_path = internal_mcp.start_background.call_args[0][0]
    assert called_path.startswith("/tmp/quimera-test-tmp/mcp-")
    mock_http_cls.assert_called_once_with(
        external_mcp,
        host="127.0.0.1",
        port=9090,
        allowed_tools=DEFAULT_HTTP_READ_ONLY_TOOLS,
    )
    mock_http.start_background.assert_called_once_with()
    assert _FakeApp.last_instance.mcp_socket_calls == [called_path]
    assert _FakeApp.last_instance.mcp_http_calls == []
    assert _FakeApp.last_instance.mcp_socket_path == called_path
    assert _FakeApp.last_instance.mcp_http_url == "http://127.0.0.1:9090/mcp"


def test_main_mcp_http_can_combine_with_custom_internal_socket(monkeypatch):
    """Verifica que main mcp http can combine with custom internal socket."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-http", "--mcp-socket", "/tmp/custom-mcp.sock"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls, patch("quimera.runtime.mcp.session.MCP_HTTPServer"):
        cli.main()

    assert mock_mcp_cls.return_value.start_background.call_args_list[0][0][0] == "/tmp/custom-mcp.sock"
    assert _FakeApp.last_instance.mcp_socket_calls == ["/tmp/custom-mcp.sock"]


def test_main_mcp_http_uses_external_token_from_env_but_internal_socket_gets_session_token(monkeypatch):
    """Verifica que main mcp http uses external token from env but internal socket gets session token."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setenv("QUIMERA_MCP_TOKEN", "remote-token")
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-http", "--mcp-port", "9090"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.secrets.token_urlsafe", return_value="internal-token"), patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls, patch("quimera.runtime.mcp.session.MCP_HTTPServer"):
        cli.main()

    auth_tokens = [call.kwargs["auth_token"] for call in mock_mcp_cls.call_args_list]
    assert auth_tokens == ["internal-token", "remote-token"]
    assert _FakeApp.last_instance.mcp_socket_tokens == ["internal-token"]
    assert _FakeApp.last_instance.mcp_http_tokens == []


def test_main_mcp_socket_uses_internal_session_token_not_external_token_env(monkeypatch):
    """Verifica que main mcp socket uses internal session token not external token env."""
    _patch_main_basics(monkeypatch)
    monkeypatch.setenv("MY_MCP_TOKEN", "external-token-not-used")
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-socket", "--mcp-token-env", "MY_MCP_TOKEN"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.secrets.token_urlsafe", return_value="internal-token"), patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls:
        cli.main()

    assert mock_mcp_cls.call_args.kwargs["auth_token"] == "internal-token"
    assert _FakeApp.last_instance.mcp_socket_tokens == ["internal-token"]


def test_main_mcp_http_uses_token_loaded_from_app_env_file(monkeypatch, tmp_path):
    """Verifica que main mcp http uses token loaded from app env file."""
    class _FakeWorkspaceWithEnv(_FakeWorkspace):
        def __init__(self, cwd):
            super().__init__(cwd)
            self.env_file = tmp_path / ".env"
            self.env_file.write_text("QUIMERA_MCP_TOKEN=env-file-token\n", encoding="utf-8")
            self.tmp = SimpleNamespace(root=tmp_path / "tmp")

    class _FakeAppLoadsEnv(_FakeApp):
        def __init__(self, cwd, **kwargs):
            workspace = kwargs.get("workspace")
            if workspace is not None and hasattr(workspace, "env_file"):
                from quimera.env_config import EnvConfig
                EnvConfig(workspace.env_file).apply_to_environ()
            super().__init__(cwd, **kwargs)

    monkeypatch.delenv("QUIMERA_MCP_TOKEN", raising=False)
    monkeypatch.setattr(cli, "Workspace", _FakeWorkspaceWithEnv)
    monkeypatch.setattr(cli, "ConfigManager", _FakeConfig)
    monkeypatch.setattr(cli, "QuimeraApp", _FakeAppLoadsEnv)
    monkeypatch.setattr(cli._profiles, "all_names", lambda: ["claude"])
    monkeypatch.setattr(cli._themes, "names", lambda: ["default"])
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-http", "--mcp-port", "9090"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    try:
        with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls, patch("quimera.runtime.mcp.session.MCP_HTTPServer"):
            cli.main()

        auth_tokens = [call.kwargs["auth_token"] for call in mock_mcp_cls.call_args_list]
        assert auth_tokens[-1] == "env-file-token"
        assert _FakeAppLoadsEnv.last_instance.mcp_http_tokens == []
    finally:
        os.environ.pop("QUIMERA_MCP_TOKEN", None)

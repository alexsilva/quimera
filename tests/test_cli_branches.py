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
from quimera.plugins.base import CliConnection, OpenAIConnection
from quimera.runtime.mcp.http_server import DEFAULT_HTTP_READ_ONLY_TOOLS


class _FakeWorkspace:
    def __init__(self, cwd):
        self.cwd = cwd
        self.config_file = Path("/tmp/quimera-test-config.json")
        self.tmp = SimpleNamespace(root=Path("/tmp/quimera-test-tmp"))


class _FakeConfig:
    last_instance = None

    def __init__(self, _config_file):
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


def _patch_main_basics(monkeypatch, *, agent_names=None, theme_names=None):
    monkeypatch.setattr(cli, "Workspace", _FakeWorkspace)
    monkeypatch.setattr(cli, "ConfigManager", _FakeConfig)
    monkeypatch.setattr(cli, "QuimeraApp", _FakeApp)
    monkeypatch.setattr(cli._plugins, "all_names", lambda: agent_names or ["claude"])
    monkeypatch.setattr(cli._themes, "names", lambda: theme_names or ["default"])


def test_read_input_uses_prompt_toolkit_when_tty(monkeypatch):
    monkeypatch.setattr(cli, "_pt_prompt", lambda _text: "  valor  ")
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(isatty=lambda: True))

    assert cli._read_input("Prompt") == "valor"


def test_prompt_text_uses_default_when_empty(monkeypatch):
    monkeypatch.setattr(cli, "_read_input", lambda _text: "")

    assert cli._prompt_text("Label", "padrao") == "padrao"


def test_prompt_text_returns_non_empty_input(monkeypatch):
    monkeypatch.setattr(cli, "_read_input", lambda _text: "valor")

    assert cli._prompt_text("Label", "padrao") == "valor"


def test_prompt_bool_reprompts_on_invalid_then_accepts_yes(monkeypatch):
    answers = iter(["talvez", "sim"])
    monkeypatch.setattr(cli, "_read_input", lambda _text: next(answers))

    with patch("builtins.print") as mock_print:
        assert cli._prompt_bool("Confirma", default=False) is True

    mock_print.assert_called_once_with("Valor inválido. Use 's' ou 'n'.")


def test_prompt_bool_uses_default_when_empty(monkeypatch):
    monkeypatch.setattr(cli, "_read_input", lambda _text: "")
    assert cli._prompt_bool("Confirma", default=True) is True


def test_prompt_bool_accepts_negative_forms(monkeypatch):
    monkeypatch.setattr(cli, "_read_input", lambda _text: "não")
    assert cli._prompt_bool("Confirma", default=True) is False


def test_cli_import_fallback_without_ui_dependencies():
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
    plugin = SimpleNamespace(
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

    conn = cli._configure_connection_interactively(plugin, driver_hint="cli")

    assert isinstance(conn, CliConnection)
    assert conn.cmd == ["codex", "--ask"]
    assert conn.prompt_as_arg is True


def test_configure_connection_interactively_cli_empty_cmd_raises(monkeypatch):
    plugin = SimpleNamespace(
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
        cli._configure_connection_interactively(plugin, driver_hint="cli")


def test_configure_connection_interactively_openai_invalid_driver_then_json_error(monkeypatch):
    plugin = SimpleNamespace(
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
        conn = cli._configure_connection_interactively(plugin)

    assert isinstance(conn, OpenAIConnection)
    assert conn.model == "gpt-x"
    assert conn.base_url == "https://example.test/v1"
    assert conn.api_key_env == "MY_KEY"
    assert conn.provider == "openai_compat"
    assert conn.extra_body is None
    assert any("Driver inválido. Use 'cli' ou 'openai'." in c.args[0] for c in mock_print.call_args_list)
    assert any("JSON inválido:" in c.args[0] for c in mock_print.call_args_list)


def test_configure_connection_interactively_openai_empty_json_clears_extra_body(monkeypatch):
    current = OpenAIConnection(
        model="gpt-cur",
        base_url="https://cur",
        api_key_env="CUR_KEY",
        provider="openai",
        supports_native_tools=True,
        extra_body={"thinking": {"type": "enabled"}},
    )
    plugin = SimpleNamespace(
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

    conn = cli._configure_connection_interactively(plugin, driver_hint="openai")

    assert isinstance(conn, OpenAIConnection)
    assert conn.extra_body is None
    assert conn.provider == "openai"


def test_configure_connection_interactively_openai_empty_input_preserves_extra_body(monkeypatch):
    current = OpenAIConnection(
        model="gpt-cur",
        base_url="https://cur",
        api_key_env="CUR_KEY",
        provider="openai_compat",
        supports_native_tools=True,
        extra_body={"thinking": {"type": "enabled"}},
    )
    plugin = SimpleNamespace(
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

    conn = cli._configure_connection_interactively(plugin, driver_hint="openai")

    assert isinstance(conn, OpenAIConnection)
    assert conn.extra_body == {"thinking": {"type": "enabled"}}


def test_build_connection_base_plugin_not_found_raises(monkeypatch):
    plugin = SimpleNamespace()
    args = Namespace(base="inexistente", model="gpt-4o", driver=None, cmd=None, extra_body=None, base_url=None, api_key_env=None)
    monkeypatch.setattr(cli._plugins, "get", lambda _name: None)

    with pytest.raises(SystemExit, match="Plugin base 'inexistente' não encontrado"):
        cli._build_connection_from_args(plugin, args)


def test_build_connection_base_plugin_value_error_becomes_system_exit(monkeypatch):
    class _BasePlugin:
        def configure_with_model(self, _model):
            raise ValueError("modelo inválido")

    plugin = SimpleNamespace()
    args = Namespace(base="base-x", model="gpt-4o", driver=None, cmd=None, extra_body=None, base_url=None, api_key_env=None)
    monkeypatch.setattr(cli._plugins, "get", lambda _name: _BasePlugin())

    with pytest.raises(SystemExit, match="modelo inválido"):
        cli._build_connection_from_args(plugin, args)


def test_build_connection_without_driver_uses_interactive(monkeypatch):
    plugin = SimpleNamespace()
    args = Namespace(base=None, model=None, driver=None, cmd=None, extra_body=None, base_url=None, api_key_env=None)

    with patch("quimera.cli._configure_connection_interactively", return_value="ok") as mocked:
        assert cli._build_connection_from_args(plugin, args) == "ok"

    mocked.assert_called_once_with(plugin)


def test_build_connection_cli_with_cmd_returns_cli_connection():
    plugin = SimpleNamespace()
    args = Namespace(base=None, model=None, driver="cli", cmd=["codex", "exec"], extra_body=None, base_url=None, api_key_env=None)

    conn = cli._build_connection_from_args(plugin, args)

    assert isinstance(conn, CliConnection)
    assert conn.cmd == ["codex", "exec"]
    assert conn.prompt_as_arg is False
    assert conn.output_format is None


def test_build_connection_cli_without_cmd_falls_back_to_interactive(monkeypatch):
    plugin = SimpleNamespace()
    args = Namespace(base=None, model=None, driver="cli", cmd=None, extra_body=None, base_url=None, api_key_env=None)

    with patch("quimera.cli._configure_connection_interactively", return_value="interactive") as mocked:
        assert cli._build_connection_from_args(plugin, args) == "interactive"

    mocked.assert_called_once_with(plugin, driver_hint="cli")


def test_main_rejects_non_positive_history_window(monkeypatch):
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
    monkeypatch.setattr(sys, "argv", ["quimera", "--list-connections"])
    monkeypatch.setattr(cli, "load_connections", lambda: {})

    with patch("builtins.print") as mock_print:
        cli.main()

    mock_print.assert_called_once_with("Nenhuma conexão persistida.")


def test_main_list_connections_prints_each_entry(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["quimera", "--list-connections"])
    monkeypatch.setattr(cli, "load_connections", lambda: {"codex": {"type": "cli", "cmd": ["codex"]}})
    monkeypatch.setattr(cli, "_connection_from_dict", lambda _data: CliConnection(cmd=["codex"]))
    monkeypatch.setattr(cli, "format_connection_label", lambda _conn: "cli: codex")

    with patch("builtins.print") as mock_print:
        cli.main()

    mock_print.assert_called_once_with("codex: cli: codex")


def test_main_connect_registers_dynamic_plugin_and_saves_override(monkeypatch):
    dynamic_plugin = SimpleNamespace(effective_connection=lambda: CliConnection(cmd=["builtin"]))

    monkeypatch.setattr(sys, "argv", ["quimera", "--connect", "novo-agente", "--driver", "cli", "--cmd", "novo-cli"])
    monkeypatch.setattr(cli._plugins, "get", lambda _name: None)
    monkeypatch.setattr(cli, "is_valid_agent_name", lambda _name: True)

    with patch("quimera.cli.register_dynamic_plugin", return_value=dynamic_plugin) as register_dynamic, \
            patch("quimera.cli.set_connection_override") as set_override, \
            patch("quimera.cli.format_connection_label", return_value="cli: ok"), \
            patch("builtins.print") as mock_print:
        cli.main()

    register_dynamic.assert_called_once_with("novo-agente", metadata=None)
    set_override.assert_called_once()
    printed_messages = [call.args[0] for call in mock_print.call_args_list]
    assert any("Agente registrado dinamicamente: novo-agente" in msg for msg in printed_messages)
    assert any("Conexão salva em base_dir para novo-agente" in msg for msg in printed_messages)


def test_main_connect_invalid_agent_name_in_connect_errors(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["quimera", "--connect", "Inválido!", "--driver", "cli", "--cmd", "x"])
    monkeypatch.setattr(cli._plugins, "get", lambda _name: None)
    monkeypatch.setattr(cli, "is_valid_agent_name", lambda _name: False)

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_main_connect_new_agent_with_missing_base_plugin_errors(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["quimera", "--connect", "novo-agente", "--base", "base-missing", "--driver", "cli", "--cmd", "x"],
    )

    def fake_get(_name):
        return None

    monkeypatch.setattr(cli._plugins, "get", fake_get)
    monkeypatch.setattr(cli, "is_valid_agent_name", lambda _name: True)

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_main_connect_new_agent_with_base_plugin_sets_metadata(monkeypatch):
    dynamic_plugin = SimpleNamespace(effective_connection=lambda: CliConnection(cmd=["builtin"]))
    base_plugin = SimpleNamespace(name="base-ok")
    calls = []

    def fake_get(name):
        calls.append(name)
        if name == "novo-agente":
            return None
        if name == "base-ok":
            return base_plugin
        return None

    monkeypatch.setattr(
        sys,
        "argv",
        ["quimera", "--connect", "novo-agente", "--base", "base-ok", "--driver", "cli", "--cmd", "novo-cli"],
    )
    monkeypatch.setattr(cli._plugins, "get", fake_get)
    monkeypatch.setattr(cli, "is_valid_agent_name", lambda _name: True)

    with patch("quimera.cli.register_dynamic_plugin", return_value=dynamic_plugin) as register_dynamic, \
            patch("quimera.cli.set_connection_override"), \
            patch("quimera.cli.format_connection_label", return_value="cli: ok"), \
            patch("builtins.print"):
        cli.main()

    register_dynamic.assert_called_once_with("novo-agente", metadata={"base": "base-ok"})


def test_main_connect_existing_plugin_inherits_base_settings(monkeypatch):
    base_formatter = lambda text: text
    existing_plugin = SimpleNamespace(
        effective_connection=lambda: CliConnection(cmd=["existing"]),
        spy_stdout_formatter=None,
        runtime_rw_paths=[],
    )
    base_plugin = SimpleNamespace(name="base-ref", spy_stdout_formatter=base_formatter, runtime_rw_paths=["/tmp/rw"])

    def fake_get(name):
        if name == "codex":
            return existing_plugin
        if name == "base-ref":
            return base_plugin
        return None

    monkeypatch.setattr(sys, "argv", ["quimera", "--connect", "codex", "--base", "base-ref", "--driver", "cli", "--cmd", "codex-cli"])
    monkeypatch.setattr(cli._plugins, "get", fake_get)

    with patch("quimera.cli.set_connection_override") as set_override, \
            patch("quimera.cli.format_connection_label", return_value="cli: ok"), \
            patch("builtins.print"):
        cli.main()

    assert existing_plugin._base_plugin_name == "base-ref"
    assert existing_plugin.spy_stdout_formatter is base_formatter
    assert existing_plugin.runtime_rw_paths == ["/tmp/rw"]
    set_override.assert_called_once()


def test_main_connect_existing_plugin_with_missing_base_errors(monkeypatch):
    existing_plugin = SimpleNamespace(
        effective_connection=lambda: CliConnection(cmd=["existing"]),
        spy_stdout_formatter=None,
        runtime_rw_paths=[],
    )

    def fake_get(name):
        if name == "codex":
            return existing_plugin
        return None

    monkeypatch.setattr(sys, "argv", ["quimera", "--connect", "codex", "--base", "base-inexistente", "--driver", "cli", "--cmd", "codex-cli"])
    monkeypatch.setattr(cli._plugins, "get", fake_get)

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_main_driver_repl_runtime_error_returns_exit_2(monkeypatch):
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


def test_main_rejects_unknown_agents(monkeypatch):
    _patch_main_basics(monkeypatch, agent_names=["claude"])
    monkeypatch.setattr(sys, "argv", ["quimera", "--agents", "desconhecido"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2


def test_main_name_sets_user_name_and_returns(monkeypatch):
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--name", "Ana", "Silva"])

    with patch("builtins.print") as mock_print:
        cli.main()

    mock_print.assert_called_once_with("Nome configurado: Ana Silva")


def test_main_whoami_prints_current_user_name(monkeypatch):
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--whoami"])

    with patch("builtins.print") as mock_print:
        cli.main()

    mock_print.assert_called_once_with("Tester")


def test_main_set_theme_persists_and_prints(monkeypatch):
    _patch_main_basics(monkeypatch, theme_names=["sunny"])
    monkeypatch.setattr(sys, "argv", ["quimera", "--set-theme", "sunny"])
    monkeypatch.setattr(cli._themes, "get", lambda _name: SimpleNamespace(name="sunny", description="Tema claro"))

    with patch("builtins.print") as mock_print:
        cli.main()

    mock_print.assert_called_once_with("Tema padrão definido: sunny — Tema claro")


def test_main_interactive_test_requires_terminal_renderer_and_agent_client(monkeypatch):
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--interactive-test"])
    monkeypatch.setattr(cli, "TerminalRenderer", None)
    monkeypatch.setattr(cli, "AgentClient", None)

    with pytest.raises(RuntimeError, match="Modo interativo não disponível"):
        cli.main()


def test_main_ignores_stdin_reconfigure_errors_and_still_runs(monkeypatch):
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


def test_main_mcp_uses_workspace_tmp_and_configures_plugins(monkeypatch):
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


def test_main_no_mcp_disables_mcp(monkeypatch):
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--no-mcp"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls:
        cli.main()

    mock_mcp_cls.assert_not_called()
    assert _FakeApp.last_instance.mcp_socket_calls == [None]


def test_main_mcp_uses_explicit_socket_path(monkeypatch):
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-socket", "/tmp/custom-mcp.sock"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls:
        mock_mcp = mock_mcp_cls.return_value
        cli.main()

    mock_mcp.start_background.assert_called_once_with("/tmp/custom-mcp.sock")


def test_main_mcp_updates_prompt_builder_session_state(monkeypatch):
    class _FakeAppWithPromptBuilder(_FakeApp):
        def __init__(self, cwd, **kwargs):
            super().__init__(cwd, **kwargs)
            self.prompt_builder = SimpleNamespace(session_state={"session_id": "sessao-teste"})

    monkeypatch.setattr(cli, "Workspace", _FakeWorkspace)
    monkeypatch.setattr(cli, "ConfigManager", _FakeConfig)
    monkeypatch.setattr(cli, "QuimeraApp", _FakeAppWithPromptBuilder)
    monkeypatch.setattr(cli._plugins, "all_names", lambda: ["claude"])
    monkeypatch.setattr(cli._themes, "names", lambda: ["default"])
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-socket", "/tmp/custom-mcp.sock"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer"):
        cli.main()

    app = _FakeAppWithPromptBuilder.last_instance
    assert app is not None
    assert app.prompt_builder.session_state["mcp_enabled"] is True
    assert app.prompt_builder.session_state["mcp_socket_path"] == "/tmp/custom-mcp.sock"


def test_main_mcp_http_configures_http_transport(monkeypatch):
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-http", "--mcp-host", "127.0.0.1", "--mcp-port", "9090"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls, patch("quimera.runtime.mcp.session.MCP_HTTPServer") as mock_http_cls:
        mock_mcp = mock_mcp_cls.return_value
        mock_http = mock_http_cls.return_value
        cli.main()

    mock_http_cls.assert_called_once_with(
        mock_mcp,
        host="127.0.0.1",
        port=9090,
        allowed_tools=DEFAULT_HTTP_READ_ONLY_TOOLS,
    )
    mock_http.start_background.assert_called_once_with()
    assert _FakeApp.last_instance.mcp_http_calls == ["http://127.0.0.1:9090/mcp"]
    assert _FakeApp.last_instance.mcp_socket_path is None


def test_main_rejects_multiple_mcp_transports(monkeypatch):
    _patch_main_basics(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-http", "--mcp-socket"])
    stderr = io.StringIO()
    monkeypatch.setattr(cli.sys, "stderr", stderr)

    with pytest.raises(SystemExit):
        cli.main()

    assert "use apenas um transporte MCP" in stderr.getvalue()


def test_main_mcp_http_uses_token_from_env(monkeypatch):
    _patch_main_basics(monkeypatch)
    monkeypatch.setenv("QUIMERA_MCP_TOKEN", "remote-token")
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-http", "--mcp-port", "9090"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls, patch("quimera.runtime.mcp.session.MCP_HTTPServer"):
        cli.main()

    assert mock_mcp_cls.call_args.kwargs["auth_token"] == "remote-token"
    assert _FakeApp.last_instance.mcp_http_tokens == ["remote-token"]


def test_main_mcp_socket_uses_custom_token_env(monkeypatch):
    _patch_main_basics(monkeypatch)
    monkeypatch.setenv("MY_MCP_TOKEN", "socket-token")
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-socket", "--mcp-token-env", "MY_MCP_TOKEN"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls:
        cli.main()

    assert mock_mcp_cls.call_args.kwargs["auth_token"] == "socket-token"
    assert _FakeApp.last_instance.mcp_socket_tokens == ["socket-token"]


def test_main_mcp_http_uses_token_loaded_from_app_env_file(monkeypatch, tmp_path):
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
    monkeypatch.setattr(cli._plugins, "all_names", lambda: ["claude"])
    monkeypatch.setattr(cli._themes, "names", lambda: ["default"])
    monkeypatch.setattr(sys, "argv", ["quimera", "--mcp-http", "--mcp-port", "9090"])
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

    try:
        with patch("quimera.runtime.mcp.session.MCPServer") as mock_mcp_cls, patch("quimera.runtime.mcp.session.MCP_HTTPServer"):
            cli.main()

        assert mock_mcp_cls.call_args.kwargs["auth_token"] == "env-file-token"
        assert _FakeAppLoadsEnv.last_instance.mcp_http_tokens == ["env-file-token"]
    finally:
        os.environ.pop("QUIMERA_MCP_TOKEN", None)

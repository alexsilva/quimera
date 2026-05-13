import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from quimera.app.system_layer import AppSystemLayer
from quimera.constants import (
    CMD_APPROVE,
    CMD_APPROVE_ALL,
    CMD_CONNECT,
    CMD_CONTEXT,
    CMD_CONTEXT_BRANCH,
    CMD_CONTEXT_EDIT,
    CMD_RELOAD,
    CMD_RESET_STATE,
)
from quimera.plugins import AgentPlugin
from quimera.plugins.base import CliConnection, OpenAIConnection


class DummyRenderer:
    def __init__(self):
        self.system_messages = []
        self.warning_messages = []
        self.error_messages = []
        self.neutral_messages = []
        self.flush_calls = 0

    def show_system(self, message):
        self.system_messages.append(message)

    def show_warning(self, message):
        self.warning_messages.append(message)

    def show_error(self, message):
        self.error_messages.append(message)

    def show_system_neutral(self, message):
        self.neutral_messages.append(message)

    def flush(self):
        self.flush_calls += 1


def make_plugin(name="codex", prefix="/codex", aliases=None):
    return AgentPlugin(
        name=name,
        prefix=prefix,
        style=("cyan", name.upper()),
        aliases=list(aliases or []),
        cmd=[name],
    )


def make_app(renderer=None):
    renderer = renderer or DummyRenderer()
    app = SimpleNamespace(
        renderer=renderer,
        _output_lock=threading.Lock(),
        _nonblocking_input_status=None,
        _prompt_owning_thread_id=None,
        _deferred_system_messages=[],
        _MAX_DEFERRED_SYSTEM_MESSAGES=2,
        active_agents=[],
        selected_agents=[],
        history=[],
        shared_state={},
        prompt_builder=None,
        execution_mode=None,
        _approval_handler=None,
    )

    app._clear_user_prompt_line_if_needed = Mock()
    app._redisplay_user_prompt_if_needed = Mock()
    app.get_agent_plugin = Mock(return_value=None)
    app.get_available_plugins = Mock(return_value=[])
    app.read_user_input = Mock(return_value="")
    app.clear_terminal_screen = Mock()
    app.reset_shared_state = Mock()
    app.task_services = SimpleNamespace(handle_task_command=Mock())
    app.context_manager = SimpleNamespace(
        show=Mock(),
        edit=Mock(),
        handle_context_branch=Mock(return_value=True),
    )
    return app


def test_flush_deferred_messages_clears_when_renderer_missing():
    app = make_app()
    app.renderer = None
    app._deferred_system_messages = ["a", "b"]

    AppSystemLayer(app).flush_deferred_messages()

    assert app._deferred_system_messages == []


def test_flush_deferred_messages_shows_and_flushes():
    app = make_app()
    app._deferred_system_messages = [("system", "a"), ("neutral", "b"), ("warning", "c"), ("error", "d")]

    AppSystemLayer(app).flush_deferred_messages()

    assert app.renderer.system_messages == ["a"]
    assert app.renderer.neutral_messages == ["b"]
    assert app.renderer.warning_messages == ["c"]
    assert app.renderer.error_messages == ["d"]
    assert app.renderer.flush_calls == 1
    assert app._deferred_system_messages == []


def test_show_system_message_returns_when_renderer_missing():
    app = make_app()
    app.renderer = None

    AppSystemLayer(app).show_system_message("ignored")


def test_show_system_message_defer_queue_none_is_noop():
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._deferred_system_messages = None

    AppSystemLayer(app).show_system_message("[task 1] codex:\nresultado")


def test_show_system_message_defer_overflow_drops_oldest():
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._MAX_DEFERRED_SYSTEM_MESSAGES = 2
    app._deferred_system_messages = [("system", "old-1"), ("system", "old-2")]

    AppSystemLayer(app).show_system_message("[task 1] codex: novo resultado sem newline")

    assert app._deferred_system_messages == [
        ("system", "old-2"),
        ("system", "[task 1] codex: novo resultado sem newline"),
    ]


def test_show_system_message_standard_path_flushes_and_redraws():
    app = make_app()

    AppSystemLayer(app).show_system_message("mensagem")

    assert app.renderer.system_messages == ["mensagem"]
    assert app.renderer.flush_calls == 1
    app._clear_user_prompt_line_if_needed.assert_called_once()
    app._redisplay_user_prompt_if_needed.assert_called_once_with(clear_first=False)


def test_show_muted_message_returns_when_renderer_missing():
    app = make_app()
    app.renderer = None

    AppSystemLayer(app).show_muted_message("ignored")


def test_show_muted_message_prefers_neutral_and_flushes():
    app = make_app()

    AppSystemLayer(app).show_muted_message("neutro")

    assert app.renderer.neutral_messages == ["neutro"]
    assert app.renderer.system_messages == []
    assert app.renderer.flush_calls == 1


def test_show_muted_message_defers_from_background_thread_while_prompt_active():
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()

    AppSystemLayer(app).show_muted_message("neutro")

    assert app._deferred_system_messages == [("neutral", "neutro")]
    assert app.renderer.neutral_messages == []
    assert app.renderer.flush_calls == 0


def test_show_muted_message_defers_task_completion_from_background_thread():
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()

    AppSystemLayer(app).show_muted_message("[task 252] concluída: Commit criado")

    assert app._deferred_system_messages == [("neutral", "[task 252] concluída: Commit criado")]
    assert app.renderer.neutral_messages == []
    assert app.renderer.flush_calls == 0


def test_show_warning_message_defers_from_background_thread_while_prompt_active():
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()

    AppSystemLayer(app).show_warning_message("atenção")

    assert app._deferred_system_messages == [("warning", "atenção")]
    assert app.renderer.warning_messages == []
    assert app.renderer.flush_calls == 0


def test_show_error_message_defers_from_background_thread_while_prompt_active():
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()

    AppSystemLayer(app).show_error_message("erro")

    assert app._deferred_system_messages == [("error", "erro")]
    assert app.renderer.error_messages == []
    assert app.renderer.flush_calls == 0


def test_show_task_response_uses_strip_and_emits_only_non_empty():
    app = make_app()
    layer = AppSystemLayer(app)

    layer.show_task_response(7, "codex", "   resultado final   ")
    layer.show_task_response(8, "codex", "   ")

    assert app.renderer.neutral_messages == ["[task 7] codex:\nresultado final"]


def test_resolve_prompt_target_covers_default_exact_and_alias_paths():
    app = make_app()
    plugin = make_plugin(name="Alpha", prefix="/alpha", aliases=["/a"])  # noqa: N806
    app.active_agents = ["ghost", "Alpha"]

    def get_plugin(name):
        return plugin if name == "Alpha" else None

    app.get_agent_plugin = Mock(side_effect=get_plugin)
    layer = AppSystemLayer(app)

    app.active_agents = ["codex"]
    assert layer._resolve_prompt_target("/prompt") == "codex"

    app.active_agents = ["Alpha"]
    assert layer._resolve_prompt_target("/prompt /alpha") == "Alpha"

    app.active_agents = ["ghost", "Alpha"]
    assert layer._resolve_prompt_target("/prompt /a") == "Alpha"


def test_resolve_connect_target_variants_and_validation_fallback():
    app = make_app()
    plugin = make_plugin(name="chatgpt", prefix="/chatgpt", aliases=["/gpt"])
    app.get_available_plugins = Mock(return_value=[plugin])
    layer = AppSystemLayer(app)

    assert layer._resolve_connect_target(CMD_CONNECT) is None
    assert layer._resolve_connect_target("/connect /gpt") == "chatgpt"

    with patch("quimera.app.system_layer.is_valid_agent_name", return_value=True):
        assert layer._resolve_connect_target("/connect agente-x") == "agente-x"

    with patch("quimera.app.system_layer.is_valid_agent_name", return_value=False):
        assert layer._resolve_connect_target("/connect agente invalido") is None


def test_read_command_input_uses_app_reader_when_available():
    app = make_app()
    app.read_user_input = Mock(return_value="ok")

    value = AppSystemLayer(app)._read_command_input("prompt: ")

    assert value == "ok"
    app.read_user_input.assert_called_once_with("prompt: ", timeout=-1)


def test_read_command_input_falls_back_to_builtin_input():
    app = make_app()
    app.read_user_input = None

    with patch("builtins.input", return_value="fallback") as patched_input:
        value = AppSystemLayer(app)._read_command_input("prompt: ")

    assert value == "fallback"
    patched_input.assert_called_once_with("prompt: ")


def test_prompt_bool_reprompts_on_invalid_value():
    app = make_app()
    app.read_user_input = Mock(side_effect=["talvez", "s"])
    layer = AppSystemLayer(app)

    assert layer._prompt_bool("Confirma", default=False) is True
    assert app.renderer.warning_messages == ["Valor inválido. Use 's' ou 'n'."]


def test_prompt_bool_supports_default_and_negative_answer():
    app = make_app()
    app.read_user_input = Mock(side_effect=["", "n"])
    layer = AppSystemLayer(app)

    assert layer._prompt_bool("Confirma", default=True) is True
    assert layer._prompt_bool("Confirma", default=True) is False


def test_configure_connection_interactively_raises_for_unknown_base_plugin():
    app = make_app()
    app.read_user_input = Mock(side_effect=["desconhecido"])
    layer = AppSystemLayer(app)
    plugin = make_plugin()

    with patch("quimera.app.system_layer._plugins.get", return_value=None):
        with pytest.raises(ValueError, match="não encontrado"):
            layer._configure_connection_interactively(plugin)


def test_configure_connection_interactively_raises_for_empty_model_in_base_plugin_mode():
    app = make_app()
    app.read_user_input = Mock(side_effect=["base", ""])
    layer = AppSystemLayer(app)
    plugin = make_plugin()
    base_plugin = make_plugin(name="base")

    with patch("quimera.app.system_layer._plugins.get", return_value=base_plugin):
        with pytest.raises(ValueError, match="modelo vazio"):
            layer._configure_connection_interactively(plugin)


def test_configure_connection_interactively_returns_base_plugin_connection_when_valid():
    app = make_app()
    app.read_user_input = Mock(side_effect=["base", "gpt-5"])
    layer = AppSystemLayer(app)
    plugin = make_plugin()
    base_plugin = make_plugin(name="base")
    base_plugin.cmd = ["base", "--model=default"]

    with patch("quimera.app.system_layer._plugins.get", return_value=base_plugin):
        connection, base_name = layer._configure_connection_interactively(plugin)

    assert isinstance(connection, CliConnection)
    assert connection.cmd == ["base", "--model=gpt-5"]
    assert base_name == "base"


def test_configure_connection_interactively_cli_reprompts_invalid_driver_and_rejects_empty_cmd():
    app = make_app()
    app.read_user_input = Mock(side_effect=["", "invalido", "cli", ""])
    layer = AppSystemLayer(app)
    plugin = make_plugin()
    plugin.cmd = []

    with pytest.raises(ValueError, match="comando CLI vazio"):
        layer._configure_connection_interactively(plugin)

    assert app.renderer.warning_messages == ["Driver inválido. Use 'cli' ou 'openai'."]


def test_configure_connection_interactively_cli_returns_connection_when_valid():
    app = make_app()
    app.read_user_input = Mock(side_effect=["", "cli", "codex run", "s"])
    layer = AppSystemLayer(app)
    plugin = make_plugin()
    plugin.cmd = []

    connection, base_name = layer._configure_connection_interactively(plugin)

    assert isinstance(connection, CliConnection)
    assert connection.cmd == ["codex", "run"]
    assert connection.prompt_as_arg is True
    assert base_name is None


def test_configure_connection_interactively_openai_empty_object_clears_extra_body():
    app = make_app()
    app.read_user_input = Mock(side_effect=["", "openai", "{}", "", "", ""])
    layer = AppSystemLayer(app)
    plugin = make_plugin()
    object.__setattr__(
        plugin,
        "_connection_override",
        OpenAIConnection(model="m1", base_url="https://api.local", api_key_env="KEY", provider="openai", extra_body={"keep": 1}),
    )

    connection, base_name = layer._configure_connection_interactively(plugin)

    assert isinstance(connection, OpenAIConnection)
    assert connection.extra_body is None
    assert base_name is None


def test_configure_connection_interactively_openai_invalid_json_keeps_previous_extra_body():
    app = make_app()
    app.read_user_input = Mock(side_effect=["", "openai", "{", "", "", ""])
    layer = AppSystemLayer(app)
    plugin = make_plugin()
    object.__setattr__(
        plugin,
        "_connection_override",
        OpenAIConnection(model="m1", base_url="https://api.local", api_key_env="KEY", provider="openai", extra_body={"keep": 2}),
    )

    connection, _ = layer._configure_connection_interactively(plugin)

    assert connection.extra_body == {"keep": 2}
    assert len(app.renderer.warning_messages) == 1
    assert "JSON inválido" in app.renderer.warning_messages[0]


def test_configure_connection_interactively_openai_blank_input_preserves_extra_body():
    app = make_app()
    app.read_user_input = Mock(side_effect=["", "openai", "", "", "", ""])
    layer = AppSystemLayer(app)
    plugin = make_plugin()
    object.__setattr__(
        plugin,
        "_connection_override",
        OpenAIConnection(model="m1", base_url="https://api.local", api_key_env="KEY", provider="openai", extra_body={"preserve": 1}),
    )

    connection, _ = layer._configure_connection_interactively(plugin)

    assert connection.extra_body == {"preserve": 1}


def test_build_prompt_preview_message_raises_without_prompt_builder():
    app = make_app()
    layer = AppSystemLayer(app)

    with pytest.raises(RuntimeError, match="prompt_builder indisponível"):
        layer._build_prompt_preview_message("codex")


def test_handle_command_connect_dynamic_plugin_and_configure_error():
    app = make_app()
    app.get_agent_plugin = Mock(return_value=None)
    layer = AppSystemLayer(app)
    dynamic_plugin = make_plugin(name="dinamico")

    with patch("quimera.app.system_layer.register_dynamic_plugin", return_value=dynamic_plugin), patch.object(
        layer,
        "_configure_connection_interactively",
        side_effect=ValueError("cancelado"),
    ):
        handled = layer.handle_command("/connect dinamico")

    assert handled is True
    assert app.renderer.warning_messages == ["cancelado"]
    assert any("Agente registrado dinamicamente" in msg for msg in app.renderer.system_messages)


def test_handle_command_connect_applies_base_plugin_and_updates_active_lists():
    app = make_app()
    layer = AppSystemLayer(app)
    plugin = make_plugin(name="target")
    base_plugin = make_plugin(name="base")
    base_plugin.spy_stdout_formatter = lambda raw: []
    base_plugin.runtime_rw_paths = ["/tmp/work"]
    app.get_agent_plugin = Mock(return_value=plugin)

    with patch.object(
        layer,
        "_configure_connection_interactively",
        return_value=(CliConnection(cmd=["target"]), "base"),
    ), patch("quimera.app.system_layer._plugins.get", return_value=base_plugin), patch(
        "quimera.app.system_layer.set_connection_override"
    ) as set_override:
        handled = layer.handle_command("/connect target")

    assert handled is True
    assert getattr(plugin, "_base_plugin_name") == "base"
    assert plugin.spy_stdout_formatter is base_plugin.spy_stdout_formatter
    assert plugin.runtime_rw_paths == ["/tmp/work"]
    assert app.active_agents == ["target"]
    assert app.selected_agents == ["target"]
    set_override.assert_called_once()


def test_handle_command_connect_passes_injected_registry_to_set_override():
    app = make_app()
    app._plugin_registry = object()
    layer = AppSystemLayer(app)
    plugin = make_plugin(name="target")
    app.get_agent_plugin = Mock(return_value=plugin)

    with patch.object(
        layer,
        "_configure_connection_interactively",
        return_value=(CliConnection(cmd=["target"]), None),
    ), patch("quimera.app.system_layer.set_connection_override") as set_override:
        handled = layer.handle_command("/connect target")

    assert handled is True
    set_override.assert_called_once()
    assert set_override.call_args.kwargs["registry"] is app._plugin_registry


def test_handle_command_reload_and_reset_state_paths():
    app = make_app()
    layer = AppSystemLayer(app)

    with patch("quimera.app.system_layer.reload_plugins", return_value=["a", "b"]):
        assert layer.handle_command(CMD_RELOAD) is True

    assert app.active_agents == ["a", "b"]
    assert app.selected_agents == ["a", "b"]

    assert layer.handle_command(CMD_RESET_STATE) is True
    app.reset_shared_state.assert_called_once()


def test_handle_command_reload_passes_injected_registry():
    app = make_app()
    app._plugin_registry = object()
    layer = AppSystemLayer(app)

    with patch("quimera.app.system_layer.reload_plugins", return_value=["a"]) as reload_mock:
        assert layer.handle_command(CMD_RELOAD) is True

    reload_mock.assert_called_once_with(registry=app._plugin_registry)


def test_handle_command_disconnect_passes_injected_registry():
    app = make_app()
    app._plugin_registry = object()
    layer = AppSystemLayer(app)

    with patch("quimera.app.system_layer.remove_connection", return_value=True) as remove_mock:
        assert layer.handle_command("/disconnect target") is True

    remove_mock.assert_called_once_with("target", registry=app._plugin_registry)


def test_handle_command_approve_all_and_approve_available_and_unavailable():
    app = make_app()
    layer = AppSystemLayer(app)

    approve_all = Mock()
    pre_approve = Mock()
    app._approval_handler = SimpleNamespace(set_approve_all=approve_all, pre_approve=pre_approve)

    assert layer.handle_command(CMD_APPROVE_ALL) is True
    assert layer.handle_command(CMD_APPROVE) is True
    approve_all.assert_called_once_with(True)
    pre_approve.assert_called_once()

    app._approval_handler = None
    assert layer.handle_command(CMD_APPROVE_ALL) is True
    assert layer.handle_command(CMD_APPROVE) is True
    assert app.renderer.warning_messages[-2:] == [
        "[aprovação] mecanismo de aprovação não disponível.",
        "[aprovação] mecanismo de aprovação não disponível.",
    ]


def test_handle_command_context_variants():
    app = make_app()
    layer = AppSystemLayer(app)

    assert layer.handle_command(CMD_CONTEXT) is True
    assert layer.handle_command(CMD_CONTEXT_EDIT) is True
    assert layer.handle_command(f"{CMD_CONTEXT_BRANCH} feat-x") is True

    app.context_manager.show.assert_called_once()
    app.context_manager.edit.assert_called_once()
    app.context_manager.handle_context_branch.assert_called_once_with(f"{CMD_CONTEXT_BRANCH} feat-x")


def test_handle_command_returns_false_for_unknown_command():
    app = make_app()
    layer = AppSystemLayer(app)

    assert layer.handle_command("/nao-existe") is False

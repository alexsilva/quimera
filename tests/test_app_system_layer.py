import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from quimera.app.agent_pool import AgentPool
from quimera.app.system_layer import AppSystemLayer
from tests.legacy_app_adapters import system_layer_from_app
from quimera.prompt_templates import PromptText
from quimera.constants import (
    CMD_APPROVE,
    CMD_APPROVE_ALL,
    CMD_BUGS,
    CMD_CONNECT,
    CMD_CONTEXT,
    CMD_CONTEXT_BRANCH,
    CMD_CONTEXT_EDIT,
    CMD_POLICY,
    CMD_PROMPT,
    CMD_RELOAD,
    CMD_RESET,
)
from quimera.profiles import ExecutionProfile
from quimera.profiles.base import CliConnection, OpenAIConnection
from quimera.ui.base import RendererBase


class DummyRenderer(RendererBase):
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

    def reset_visual_state(self, *a, **kw): pass


def make_profile(name="codex", prefix="/codex", aliases=None):
    return ExecutionProfile(
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

    app._redisplay_user_prompt_if_needed = Mock()
    app.get_agent_profile = Mock(return_value=None)
    app.get_available_profiles = Mock(return_value=[])
    app.read_user_input = Mock(return_value="")
    app.clear_terminal_screen = Mock()
    app.session_state_mgr = SimpleNamespace(reset=Mock())
    app.task_services = SimpleNamespace(handle_task_command=Mock())
    app.context_manager = SimpleNamespace(
        show=Mock(),
        edit=Mock(),
        handle_context_branch=Mock(return_value=True),
    )
    return app


def test_flush_deferred_messages_clears_when_renderer_missing():
    """Verifica que Test flush deferred messages clears when renderer missing."""
    app = make_app()
    app.renderer = None
    app._deferred_system_messages = ["a", "b"]

    system_layer_from_app(app).flush_deferred_messages()

    assert app._deferred_system_messages == []


def test_flush_deferred_messages_shows_and_flushes():
    """Verifica que Test flush deferred messages shows and flushes."""
    app = make_app()
    app._deferred_system_messages = [("system", "a"), ("neutral", "b"), ("warning", "c"), ("error", "d")]

    system_layer_from_app(app).flush_deferred_messages()

    assert app.renderer.system_messages == ["a"]
    assert app.renderer.neutral_messages == ["b"]
    assert app.renderer.warning_messages == ["c"]
    assert app.renderer.error_messages == ["d"]
    assert app.renderer.flush_calls == 1
    assert app._deferred_system_messages == []


def test_show_system_message_returns_when_renderer_missing():
    """Verifica que Test show system message returns when renderer missing."""
    app = make_app()
    app.renderer = None

    system_layer_from_app(app).show_system_message("ignored")


def test_show_system_message_defer_queue_none_is_noop():
    """Verifica que Test show system message defer queue none is noop."""
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._deferred_system_messages = None

    system_layer_from_app(app).show_system_message("[task 1] codex:\nresultado")


def test_show_system_message_defer_overflow_drops_oldest():
    """Verifica que Test show system message defer overflow drops oldest."""
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._MAX_DEFERRED_SYSTEM_MESSAGES = 2
    app._deferred_system_messages = [("system", "old-1"), ("system", "old-2")]

    system_layer_from_app(app).show_system_message("[task 1] codex: novo resultado sem newline")

    assert app._deferred_system_messages == [
        ("system", "old-2"),
        ("system", "[task 1] codex: novo resultado sem newline"),
    ]


def test_show_system_message_standard_path_flushes_and_redraws():
    """Verifica que Test show system message standard path flushes and redraws."""
    app = make_app()

    system_layer_from_app(app).show_system_message("mensagem")

    assert app.renderer.system_messages == ["mensagem"]
    assert app.renderer.flush_calls == 1
    app._redisplay_user_prompt_if_needed.assert_called_once_with(clear_first=False)


def test_show_system_message_prefers_quick_flush_on_prompt_owner_thread():
    """Verifica que Test show system message prefers quick flush on prompt owner thread."""
    class QuickFlushRenderer(DummyRenderer):
        def __init__(self):
            super().__init__()
            self.flush_quick_calls = 0

        def flush_quick(self):
            self.flush_quick_calls += 1
            return True

        def flush(self):
            raise AssertionError("flush() não deveria ser usado quando flush_quick existe")

    renderer = QuickFlushRenderer()
    app = make_app(renderer=renderer)

    system_layer_from_app(app).show_system_message("mensagem")

    assert renderer.system_messages == ["mensagem"]
    assert renderer.flush_quick_calls == 1
    app._redisplay_user_prompt_if_needed.assert_called_once_with(clear_first=False)


def test_show_muted_message_returns_when_renderer_missing():
    """Verifica que Test show muted message returns when renderer missing."""
    app = make_app()
    app.renderer = None

    system_layer_from_app(app).show_muted_message("ignored")


def test_show_muted_message_prefers_neutral_and_flushes():
    """Verifica que Test show muted message prefers neutral and flushes."""
    app = make_app()

    system_layer_from_app(app).show_muted_message("neutro")

    assert app.renderer.neutral_messages == ["neutro"]
    assert app.renderer.system_messages == []
    assert app.renderer.flush_calls == 1


def test_show_muted_message_defers_from_background_thread_while_prompt_active():
    """Verifica que Test show muted message defers from background thread while prompt active."""
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()

    system_layer_from_app(app).show_muted_message("neutro")

    assert app._deferred_system_messages == [("neutral", "neutro")]
    assert app.renderer.neutral_messages == []
    assert app.renderer.flush_calls == 0


def test_show_muted_message_defers_task_completion_from_background_thread():
    """Verifica que Test show muted message defers task completion from background thread."""
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()

    system_layer_from_app(app).show_muted_message("[task 252] concluída: Commit criado")

    assert app._deferred_system_messages == [("neutral", "[task 252] concluída: Commit criado")]
    assert app.renderer.neutral_messages == []
    assert app.renderer.flush_calls == 0


def test_show_system_message_shows_above_prompt_when_input_gate_supports_it():
    """Verifica que Test show system message shows above prompt when input gate supports it."""
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()
    callbacks = []

    def run_in_terminal_message(callback):
        callbacks.append(callback)
        callback()
        return True

    app.input_gate = SimpleNamespace(run_in_terminal_message=run_in_terminal_message)

    system_layer_from_app(app).show_system_message("sys msg")

    assert app._deferred_system_messages == []
    assert app.renderer.system_messages == ["sys msg"]
    assert app.renderer.flush_calls == 1
    assert len(callbacks) == 1


def test_show_muted_message_shows_task_completion_above_prompt_when_input_gate_supports_it():
    """Verifica que Test show muted message shows task completion above prompt when input gate supports it."""
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()
    callbacks = []

    def run_in_terminal_message(callback):
        callbacks.append(callback)
        callback()
        return True

    app.input_gate = SimpleNamespace(run_in_terminal_message=run_in_terminal_message)

    system_layer_from_app(app).show_muted_message("[task 252] concluída: Commit criado")

    assert app._deferred_system_messages == []
    assert app.renderer.neutral_messages == ["[task 252] concluída: Commit criado"]
    assert app.renderer.flush_calls == 1
    assert len(callbacks) == 1


def test_show_warning_message_defers_from_background_thread_while_prompt_active():
    """Verifica que Test show warning message defers from background thread while prompt active."""
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()

    system_layer_from_app(app).show_warning_message("atenção")

    assert app._deferred_system_messages == [("warning", "atenção")]
    assert app.renderer.warning_messages == []
    assert app.renderer.flush_calls == 0


def test_show_warning_message_shows_above_prompt_when_input_gate_supports_it():
    """Verifica que Test show warning message shows above prompt when input gate supports it."""
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()
    callbacks = []

    def run_in_terminal_message(callback):
        callbacks.append(callback)
        callback()
        return True

    app.input_gate = SimpleNamespace(run_in_terminal_message=run_in_terminal_message)

    system_layer_from_app(app).show_warning_message("atenção")

    assert app._deferred_system_messages == []
    assert app.renderer.warning_messages == ["atenção"]
    assert app.renderer.flush_calls == 1
    assert len(callbacks) == 1


def test_show_error_message_defers_from_background_thread_while_prompt_active():
    """Verifica que Test show error message defers from background thread while prompt active."""
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()

    system_layer_from_app(app).show_error_message("erro")

    assert app._deferred_system_messages == [("error", "erro")]
    assert app.renderer.error_messages == []
    assert app.renderer.flush_calls == 0


def test_show_error_message_shows_above_prompt_when_input_gate_supports_it():
    """Verifica que Test show error message shows above prompt when input gate supports it."""
    app = make_app()
    app._nonblocking_input_status = "reading"
    app._prompt_owning_thread_id = object()
    callbacks = []

    def run_in_terminal_message(callback):
        callbacks.append(callback)
        callback()
        return True

    app.input_gate = SimpleNamespace(run_in_terminal_message=run_in_terminal_message)

    system_layer_from_app(app).show_error_message("erro")

    assert app._deferred_system_messages == []
    assert app.renderer.error_messages == ["erro"]
    assert app.renderer.flush_calls == 1
    assert len(callbacks) == 1


def test_show_task_response_uses_strip_and_emits_only_non_empty():
    """Verifica que Test show task response uses strip and emits only non empty."""
    app = make_app()
    layer = system_layer_from_app(app)

    layer.show_task_response(7, "codex", "   resultado final   ")
    layer.show_task_response(8, "codex", "   ")

    assert app.renderer.neutral_messages == ["[task 7] codex:\nresultado final"]


def test_resolve_prompt_target_covers_default_exact_and_alias_paths():
    """Verifica que Test resolve prompt target covers default exact and alias paths."""
    app = make_app()
    profile = make_profile(name="Alpha", prefix="/alpha", aliases=["/a"])  # noqa: N806
    app.active_agents = ["ghost", "Alpha"]

    def get_profile(name):
        return profile if name == "Alpha" else None

    app.get_agent_profile = Mock(side_effect=get_profile)
    layer = system_layer_from_app(app)

    app.active_agents = ["codex"]
    assert layer._resolve_prompt_target("/prompt") == "codex"

    # robustez: /prompt show codex também resolve
    assert layer._resolve_prompt_target("/prompt show codex") == "codex"
    assert layer._resolve_prompt_target("/prompt codex") == "codex"
    assert layer._resolve_prompt_target("/prompt /codex") == "codex"

    app.active_agents = ["Alpha"]
    assert layer._resolve_prompt_target("/prompt /alpha") == "Alpha"

    app.active_agents = ["ghost", "Alpha"]
    assert layer._resolve_prompt_target("/prompt /a") == "Alpha"


def test_resolve_prompt_target_prompts_when_ambiguous():
    """Verifica que Test resolve prompt target prompts when ambiguous."""
    app = make_app()
    app.active_agents = ["codex", "claude"]
    layer = system_layer_from_app(app)

    with patch.object(layer, "_prompt_text", return_value="claude") as mock_prompt:
        assert layer._resolve_prompt_target("/prompt") == "claude"
        mock_prompt.assert_called_once()
        assert "codex, claude" in mock_prompt.call_args[0][0]


def test_resolve_connect_target_variants_and_validation_fallback():
    """Verifica que Test resolve connect target variants and validation fallback."""
    app = make_app()
    profile = make_profile(name="chatgpt", prefix="/chatgpt", aliases=["/gpt"])
    app.get_available_profiles = Mock(return_value=[profile])
    layer = system_layer_from_app(app)

    assert layer._resolve_connect_target(CMD_CONNECT) is None
    assert layer._resolve_connect_target("/connect /gpt") == "chatgpt"

    with patch("quimera.app.system_layer.is_valid_agent_name", return_value=True):
        assert layer._resolve_connect_target("/connect agente-x") == "agente-x"

    with patch("quimera.app.system_layer.is_valid_agent_name", return_value=False):
        assert layer._resolve_connect_target("/connect agente invalido") is None


def test_read_command_input_uses_app_reader_when_available():
    """Verifica que Test read command input uses app reader when available."""
    app = make_app()
    app.read_user_input = Mock(return_value="ok")

    value = system_layer_from_app(app)._read_command_input("prompt: ")

    assert value == "ok"
    app.read_user_input.assert_called_once_with("prompt: ", timeout=-1)


def test_read_command_input_falls_back_to_builtin_input():
    """Verifica que Test read command input falls back to builtin input."""
    app = make_app()
    app.read_user_input = None

    with patch("builtins.input", return_value="fallback") as patched_input:
        value = system_layer_from_app(app)._read_command_input("prompt: ")

    assert value == "fallback"
    patched_input.assert_called_once_with("prompt: ")


def test_prompt_bool_reprompts_on_invalid_value():
    """Verifica que Test prompt bool reprompts on invalid value."""
    app = make_app()
    app.read_user_input = Mock(side_effect=["talvez", "s"])
    layer = system_layer_from_app(app)

    assert layer._prompt_bool("Confirma", default=False) is True
    assert app.renderer.warning_messages == ["Valor inválido. Use 's' ou 'n'."]


def test_prompt_bool_supports_default_and_negative_answer():
    """Verifica que Test prompt bool supports default and negative answer."""
    app = make_app()
    app.read_user_input = Mock(side_effect=["", "n"])
    layer = system_layer_from_app(app)

    assert layer._prompt_bool("Confirma", default=True) is True
    assert layer._prompt_bool("Confirma", default=True) is False


def test_configure_connection_interactively_raises_for_unknown_profile():
    """Verifica que Test configure connection interactively raises for unknown profile."""
    app = make_app()
    app.read_user_input = Mock(side_effect=["desconhecido"])
    layer = system_layer_from_app(app)
    profile = make_profile()

    with patch("quimera.app.system_layer._profiles.get", return_value=None):
        with pytest.raises(ValueError, match="não encontrado"):
            layer._configure_connection_interactively(profile)


def test_configure_connection_interactively_raises_for_empty_model_in_profile_mode():
    """Verifica que Test configure connection interactively raises for empty model in profile mode."""
    app = make_app()
    app.read_user_input = Mock(side_effect=["base", ""])
    layer = system_layer_from_app(app)
    profile = make_profile()
    profile = make_profile(name="base")

    with patch("quimera.app.system_layer._profiles.get", return_value=profile):
        with pytest.raises(ValueError, match="modelo vazio"):
            layer._configure_connection_interactively(profile)


def test_configure_connection_interactively_returns_profile_connection_when_valid():
    """Verifica que Test configure connection interactively returns profile connection when valid."""
    app = make_app()
    app.read_user_input = Mock(side_effect=["base", "gpt-5"])
    layer = system_layer_from_app(app)
    profile = make_profile()
    profile = make_profile(name="base")
    profile.cmd = ["base", "--model=default"]

    with patch("quimera.app.system_layer._profiles.get", return_value=profile):
        connection, profile_name = layer._configure_connection_interactively(profile)

    assert isinstance(connection, CliConnection)
    assert connection.cmd == ["base", "--model=gpt-5"]
    assert profile_name == "base"


def test_configure_connection_interactively_cli_reprompts_invalid_driver_and_rejects_empty_cmd():
    """Verifica que Test configure connection interactively cli reprompts invalid driver and rejects empty cmd."""
    app = make_app()
    # profile_base, driver(invalid), driver(valid), output_format, cmd(empty→ValueError)
    app.read_user_input = Mock(side_effect=["", "invalido", "cli", "", ""])
    layer = system_layer_from_app(app)
    profile = make_profile()
    profile.cmd = []

    with pytest.raises(ValueError, match="comando CLI vazio"):
        layer._configure_connection_interactively(profile)

    assert app.renderer.warning_messages == ["Driver inválido. Use 'cli' ou 'openai'."]


def test_configure_connection_interactively_cli_returns_connection_when_valid():
    """Verifica que Test configure connection interactively cli returns connection when valid."""
    app = make_app()
    # profile_base, driver, output_format, cmd, prompt_as_arg
    app.read_user_input = Mock(side_effect=["", "cli", "", "codex run", "s"])
    layer = system_layer_from_app(app)
    profile = make_profile()
    profile.cmd = []

    connection, profile_name = layer._configure_connection_interactively(profile)

    assert isinstance(connection, CliConnection)
    assert connection.cmd == ["codex", "run"]
    assert connection.prompt_as_arg is True
    assert profile_name is None


def test_configure_connection_interactively_openai_empty_object_clears_extra_body():
    """Verifica que Test configure connection interactively openai empty object clears extra body."""
    app = make_app()
    # profile_base, driver, provider, model, base_url, api_key_env, extra_body("{}"=clear), supports_tools, max_connections
    app.read_user_input = Mock(side_effect=["", "openai", "", "", "", "", "{}", "", ""])
    layer = system_layer_from_app(app)
    profile = make_profile()
    object.__setattr__(
        profile,
        "_connection_override",
        OpenAIConnection(model="m1", base_url="https://api.local", api_key_env="KEY", provider="openai", extra_body={"keep": 1}),
    )

    connection, profile_name = layer._configure_connection_interactively(profile)

    assert isinstance(connection, OpenAIConnection)
    assert connection.extra_body is None
    assert profile_name is None


def test_configure_connection_interactively_openai_invalid_json_keeps_previous_extra_body():
    """Verifica que Test configure connection interactively openai invalid json keeps previous extra body."""
    app = make_app()
    # profile_base, driver, provider, model, base_url, api_key_env, extra_body("{"=invalid), supports_tools, max_connections
    app.read_user_input = Mock(side_effect=["", "openai", "", "", "", "", "{", "", ""])
    layer = system_layer_from_app(app)
    profile = make_profile()
    object.__setattr__(
        profile,
        "_connection_override",
        OpenAIConnection(model="m1", base_url="https://api.local", api_key_env="KEY", provider="openai", extra_body={"keep": 2}),
    )

    connection, _ = layer._configure_connection_interactively(profile)

    assert connection.extra_body == {"keep": 2}
    assert len(app.renderer.warning_messages) == 1
    assert "JSON inválido" in app.renderer.warning_messages[0]


def test_configure_connection_interactively_openai_blank_input_preserves_extra_body():
    """Verifica que Test configure connection interactively openai blank input preserves extra body."""
    app = make_app()
    # profile_base, driver, provider, model, base_url, api_key_env, extra_body(empty=preserve), supports_tools, max_connections
    app.read_user_input = Mock(side_effect=["", "openai", "", "", "", "", "", "", ""])
    layer = system_layer_from_app(app)
    profile = make_profile()
    object.__setattr__(
        profile,
        "_connection_override",
        OpenAIConnection(model="m1", base_url="https://api.local", api_key_env="KEY", provider="openai", extra_body={"preserve": 1}),
    )

    connection, _ = layer._configure_connection_interactively(profile)

    assert connection.extra_body == {"preserve": 1}


def test_build_prompt_preview_message_raises_without_prompt_builder():
    """Verifica que Test build prompt preview message raises without prompt builder."""
    app = make_app()
    layer = system_layer_from_app(app)

    with pytest.raises(RuntimeError, match="prompt_builder indisponível"):
        layer._build_prompt_preview_message("codex")


def _make_dummy_metrics():
    return {
        "rules_chars": 0,
        "session_state_chars": 0,
        "persistent_chars": 0,
        "request_chars": 0,
        "execution_state_chars": 0,
        "shared_state_chars": 0,
        "history_chars": 0,
        "delegation_chars": 0,
        "history_messages": 0,
        "total_chars": 5,
    }


def test_build_prompt_preview_message_omits_raw_history():
    """Verifica que Test build prompt preview message omits raw history."""
    app = make_app()
    app.history = [
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": "olá"},
    ]
    mock_builder = Mock()
    mock_builder.build.return_value = (PromptText("PROMPT", strict=False), _make_dummy_metrics())
    mock_builder.history_window = 10
    app.prompt_builder = mock_builder
    layer = system_layer_from_app(app)

    result = layer._build_prompt_preview_message("codex")

    assert "RAW HISTÓRICO" not in result
    assert "[0] USER: oi" not in result
    assert "[1] ASSISTANT: olá" not in result
    assert "ANÁLISE DOS BLOCOS:" in result
    assert "PROMPT FINAL:" in result


def test_build_prompt_preview_message_empty_history_omits_placeholder():
    """Verifica que Test build prompt preview message empty history omits placeholder."""
    app = make_app()
    app.history = []
    mock_builder = Mock()
    mock_builder.build.return_value = (PromptText("PROMPT", strict=False), _make_dummy_metrics())
    mock_builder.history_window = 10
    app.prompt_builder = mock_builder
    layer = system_layer_from_app(app)

    result = layer._build_prompt_preview_message("codex")

    assert "RAW HISTÓRICO" not in result
    assert "[sem mensagens no histórico]" not in result
    assert "ANÁLISE DOS BLOCOS:" in result


def test_build_prompt_preview_message_follower_mode_passes_is_first_speaker_false():
    """Verifica que Test build prompt preview message follower mode passes is first speaker false."""
    app = make_app()
    app.history = []
    mock_builder = Mock()
    mock_builder.build.return_value = (PromptText("PROMPT", strict=False), _make_dummy_metrics())
    mock_builder.history_window = 10
    app.prompt_builder = mock_builder
    layer = system_layer_from_app(app)

    layer._build_prompt_preview_message("codex", is_first_speaker=False)

    _, kwargs = mock_builder.build.call_args
    assert kwargs.get("is_first_speaker") is False


def test_build_prompt_preview_message_first_speaker_mode_label():
    """Verifica que Test build prompt preview message first speaker mode label."""
    app = make_app()
    app.history = []
    mock_builder = Mock()
    mock_builder.build.return_value = (PromptText("PROMPT", strict=False), _make_dummy_metrics())
    mock_builder.history_window = 10
    app.prompt_builder = mock_builder
    layer = system_layer_from_app(app)

    result = layer._build_prompt_preview_message("codex", is_first_speaker=True)
    assert "primeiro-falante" in result

    result_follower = layer._build_prompt_preview_message("codex", is_first_speaker=False)
    assert "follower/reviewer" in result_follower


def test_handle_command_connect_dynamic_profile_and_configure_error():
    """Verifica que Test handle command connect dynamic profile and configure error."""
    app = make_app()
    app.get_agent_profile = Mock(return_value=None)
    layer = system_layer_from_app(app)
    dynamic_profile = make_profile(name="dinamico")

    with patch("quimera.app.system_layer.register_connection_profile", return_value=dynamic_profile), patch.object(
        layer,
        "_configure_connection_interactively",
        side_effect=ValueError("cancelado"),
    ):
        handled = layer.handle_command("/connect dinamico")

    assert handled is True
    assert app.renderer.warning_messages == ["cancelado"]
    assert any("Conexão registrada" in msg for msg in app.renderer.system_messages)


def test_handle_command_connect_opens_textual_modal_without_prompting():
    app = make_app()
    layer = system_layer_from_app(app)
    profile = make_profile(name="target")
    app.get_agent_profile = Mock(return_value=profile)
    app.renderer.open_connection_config = Mock(return_value=True)

    with patch.object(layer, "_configure_connection_interactively") as configure:
        handled = layer.handle_command("/connect target")

    assert handled is True
    app.renderer.open_connection_config.assert_called_once_with("target", advanced=False)
    configure.assert_not_called()


def test_handle_command_connect_forwards_advanced_flag_to_modal():
    app = make_app()
    layer = system_layer_from_app(app)
    profile = make_profile(name="target")
    app.get_agent_profile = Mock(return_value=profile)
    app.renderer.open_connection_config = Mock(return_value=True)

    assert layer.handle_command("/connect target --advanced") is True

    app.renderer.open_connection_config.assert_called_once_with("target", advanced=True)


def test_handle_command_connect_applies_profile_and_updates_active_lists():
    """Verifica que Test handle command connect applies profile and updates active lists."""
    app = make_app()
    layer = system_layer_from_app(app)
    target_profile = make_profile(name="target")
    inherited_profile = make_profile(name="target")
    inherited_profile.dynamic = True
    base_profile = make_profile(name="base")
    base_profile.spy_stdout_formatter = lambda raw: []
    base_profile.runtime_rw_paths = ["/tmp/work"]
    app.get_agent_profile = Mock(return_value=target_profile)

    with patch.object(
        layer,
        "_configure_connection_interactively",
        return_value=(CliConnection(cmd=["target"]), "base"),
    ), patch("quimera.app.system_layer.register_connection_profile", return_value=inherited_profile) as register_dynamic, patch(
        "quimera.app.system_layer.set_connection"
    ) as set_override:
        handled = layer.handle_command("/connect target")

    assert handled is True
    register_dynamic.assert_called_once_with("target", metadata={"profile": "base"}, registry=None)
    assert app.active_agents == ["target"]
    assert app.selected_agents == ["target"]
    set_override.assert_called_once()


def test_handle_command_connect_passes_injected_registry_to_set_override():
    """Verifica que Test handle command connect passes injected registry to set override."""
    app = make_app()
    app._profile_registry = object()
    layer = system_layer_from_app(app)
    profile = make_profile(name="target")
    app.get_agent_profile = Mock(return_value=profile)

    with patch.object(
        layer,
        "_configure_connection_interactively",
        return_value=(CliConnection(cmd=["target"]), None),
    ), patch("quimera.app.system_layer.set_connection") as set_override:
        handled = layer.handle_command("/connect target")

    assert handled is True
    set_override.assert_called_once()
    assert set_override.call_args.kwargs["registry"] is app._profile_registry


def test_handle_command_reload_preserves_session_agents():
    """Verifica que Test handle command reload preserves session agents."""
    app = make_app()
    app.active_agents = ["existing_agent"]
    app.selected_agents = ["existing_agent", "ghost"]
    layer = system_layer_from_app(app)

    with patch("quimera.app.system_layer.reload_profiles", return_value=["existing_agent", "new_agent"]):
        assert layer.handle_command(CMD_RELOAD) is True

    assert app.active_agents == ["existing_agent"]
    assert app.selected_agents == ["existing_agent"]
    assert app.renderer.system_messages[-1] == "Profiles recarregados: 2 profile(s)"


def test_handle_command_reload_and_reset_state_paths():
    """Verifica que Test handle command reload and reset state paths."""
    app = make_app()
    app.active_agents = ["a", "stale"]
    app.selected_agents = ["a", "ghost"]
    layer = system_layer_from_app(app)

    with patch("quimera.app.system_layer.reload_profiles", return_value=["a", "b"]):
        assert layer.handle_command(CMD_RELOAD) is True

    assert app.active_agents == ["a"]
    assert app.selected_agents == ["a"]
    assert app.renderer.system_messages[-1] == "Profiles recarregados: 2 profile(s)"

    assert layer.handle_command(CMD_RESET) is True
    app.session_state_mgr.reset.assert_called_once_with("state")

    assert layer.handle_command(f"{CMD_RESET} all") is True
    assert app.session_state_mgr.reset.call_args_list[-1] == (("all",),)


def test_handle_command_reload_passes_injected_registry():
    """Verifica que Test handle command reload passes injected registry."""
    app = make_app()
    app._profile_registry = object()
    layer = system_layer_from_app(app)

    with patch("quimera.app.system_layer.reload_profiles", return_value=["a"]) as reload_mock:
        assert layer.handle_command(CMD_RELOAD) is True

    reload_mock.assert_called_once_with(registry=app._profile_registry)


def test_handle_command_disconnect_passes_injected_registry():
    """Verifica que Test handle command disconnect passes injected registry."""
    app = make_app()
    app._profile_registry = object()
    layer = system_layer_from_app(app)

    with patch("quimera.app.system_layer.remove_connection", return_value=True) as remove_mock:
        assert layer.handle_command("/disconnect target") is True

    remove_mock.assert_called_once_with("target", registry=app._profile_registry)


def test_handle_command_approve_all_and_approve_available_and_unavailable():
    """Verifica que Test handle command approve all and approve available and unavailable."""
    app = make_app()
    layer = system_layer_from_app(app)

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


def test_handle_command_policy_status_and_setter():
    """Verifica que /policy mostra e altera o preset do workspace."""
    app = make_app()
    policy_name = {"value": "strict"}
    app.get_workspace_policy_name = Mock(side_effect=lambda: policy_name["value"])
    app.set_workspace_policy_name = Mock(side_effect=lambda value: policy_name.update(value=value))
    layer = system_layer_from_app(app)

    assert layer.handle_command(CMD_POLICY) is True
    assert "atual: strict" in app.renderer.system_messages[-1]

    assert layer.handle_command("/policy autonomous") is True
    app.set_workspace_policy_name.assert_called_once_with("autonomous")
    assert "workspace_policy=autonomous" in app.renderer.system_messages[-1]

    app.set_workspace_policy_name.reset_mock()
    assert layer.handle_command("/policy developer") is True
    app.set_workspace_policy_name.assert_called_once_with("developer")
    assert "workspace_policy=developer" in app.renderer.system_messages[-1]


def test_handle_command_policy_rejects_unknown_preset():
    """Verifica que /policy rejeita presets desconhecidos."""
    app = make_app()
    app.get_workspace_policy_name = Mock(return_value="strict")
    app.set_workspace_policy_name = Mock()
    layer = system_layer_from_app(app)

    assert layer.handle_command("/policy unsafe") is True
    app.set_workspace_policy_name.assert_not_called()
    assert app.renderer.warning_messages[-1] == "Uso: /policy [status|strict|developer|autonomous]"


def test_handle_command_context_variants():
    """Verifica que Test handle command context variants."""
    app = make_app()
    layer = system_layer_from_app(app)

    assert layer.handle_command(CMD_CONTEXT) is True
    assert layer.handle_command(f"{CMD_CONTEXT} edit") is True
    assert layer.handle_command(f"{CMD_CONTEXT} branch feat-x") is True

    app.context_manager.show.assert_called_once()
    app.context_manager.edit.assert_called_once()
    app.context_manager.handle_context_branch.assert_called_once_with(f"{CMD_CONTEXT} branch feat-x")


def test_handle_command_context_backward_compat():
    """Hífenes ainda funcionam: /context-edit e /context-branch."""
    app = make_app()
    layer = system_layer_from_app(app)

    assert layer.handle_command(CMD_CONTEXT_EDIT) is True
    assert layer.handle_command(f"{CMD_CONTEXT_BRANCH} main") is True

    app.context_manager.edit.assert_called_once()
    app.context_manager.handle_context_branch.assert_called_once_with(f"{CMD_CONTEXT_BRANCH} main")


def test_handle_command_prompt_preview():
    """/prompt [agente] exibe preview do prompt."""
    app = make_app()
    app.renderer.show_prompt_preview = Mock()
    layer = system_layer_from_app(app)

    with patch.object(layer, "_resolve_prompt_target", return_value="codex"):
        with patch.object(layer, "_build_prompt_preview_message", return_value="preview"):
            assert layer.handle_command(f"{CMD_PROMPT} codex") is True
            app.renderer.show_prompt_preview.assert_called_once_with("codex", "preview")


def test_handle_command_returns_false_for_unknown_command():
    """Verifica que Test handle command returns false for unknown command."""
    app = make_app()
    layer = system_layer_from_app(app)

    assert layer.handle_command("/nao-existe") is False


def test_handle_command_bugs_dispatches_to_handler():
    """Verifica que Test handle command bugs dispatches to handler."""
    app = make_app()
    handler = Mock(return_value=True)
    layer = system_layer_from_app(app, bugs_command_handler=handler)

    assert layer.handle_command(CMD_BUGS) is True

    handler.assert_called_once_with(CMD_BUGS)


def test_handle_command_bugs_without_handler_warns():
    """Verifica que Test handle command bugs without handler warns."""
    app = make_app()
    layer = system_layer_from_app(app)

    assert layer.handle_command(CMD_BUGS) is True
    assert app.renderer.warning_messages[-1] == "Comando /bugs indisponível nesta sessão."


# ---------------------------------------------------------------------------
# Eventos de auditoria do ciclo deferred
# ---------------------------------------------------------------------------


def test_enqueue_logs_audit_event():
    """_enqueue_deferred_message loga deferred_enqueue via renderer."""
    renderer = DummyRenderer()
    renderer.log_debug_event = Mock()
    app = make_app(renderer)
    layer = system_layer_from_app(app)

    layer._enqueue_deferred_message("[task 1] codex: testando", level="system")

    renderer.log_debug_event.assert_called_once_with(
        "deferred_enqueue",
        message="[task 1] codex: testando",
        level="system",
        task_id=1,
    )


def test_enqueue_logs_audit_event_without_task_id():
    """deferred_enqueue funciona com mensagem sem task_id."""
    renderer = DummyRenderer()
    renderer.log_debug_event = Mock()
    app = make_app(renderer)
    layer = system_layer_from_app(app)

    layer._enqueue_deferred_message("mensagem livre", level="warning")

    renderer.log_debug_event.assert_called_once_with(
        "deferred_enqueue",
        message="mensagem livre",
        level="warning",
    )


def test_flush_logs_audit_event():
    """flush_deferred_messages loga deferred_flush via renderer."""
    renderer = DummyRenderer()
    renderer.log_debug_event = Mock()
    app = make_app(renderer)
    app._deferred_system_messages = [
        ("system", "[task 1] codex: concluída"),
        ("neutral", "outra msg"),
    ]
    layer = system_layer_from_app(app)

    layer.flush_deferred_messages()

    renderer.log_debug_event.assert_called_once_with(
        "deferred_flush",
        count=2,
        previews=["[task 1] codex: concluída", "outra msg"],
    )


def test_deferred_audit_no_logger_does_not_crash():
    """Enqueue e flush não quebram quando não há _audit_logger."""
    app = make_app()  # DummyRenderer não tem _audit_logger
    layer = system_layer_from_app(app)

    layer._enqueue_deferred_message("teste", level="system")

    app._deferred_system_messages = [("system", "teste")]
    layer.flush_deferred_messages()


# ---------------------------------------------------------------------------
# Compactação do flush_deferred_messages (T1)
# ---------------------------------------------------------------------------


def test_compact_deferred_only_transient():
    """Lote com apenas mensagens transitórias: dedup mantém só a última por task."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: aguardando review"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    # Now dedup keeps only the last message per task (T2b)
    assert result == [("system", "[task 1] codex: aguardando review")]


def test_compact_deferred_terminal_suppresses_transient_same_task():
    """Lote com terminal + transitórios da mesma task: remove os transitórios."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: processando"),
        ("system", "[task 1] codex: concluída"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [("system", "⚙ [task 1] codex: concluída")]


def test_compact_deferred_terminal_middle():
    """Terminal no meio do lote ainda remove transitórios anteriores."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: concluída"),
        ("system", "[task 1] codex: pós-processamento"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [("system", "⚙ [task 1] codex: concluída")]


def test_compact_deferred_different_tasks_not_affected():
    """Tasks diferentes não se afetam."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 2] claude: processando"),
        ("system", "[task 1] codex: concluída"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [
        ("system", "[task 2] claude: processando"),
        ("system", "⚙ [task 1] codex: concluída"),
    ]


def test_compact_deferred_no_task_messages():
    """Mensagens sem task ID não são afetadas."""
    deferred = [
        ("system", "mensagem normal"),
        ("neutral", "outra mensagem"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == deferred


def test_compact_deferred_mixed_tasks_one_terminal():
    """Task 1 terminal, task 2 sem terminal: task 2 preservada."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 2] claude: iniciando"),
        ("system", "[task 1] codex: concluída"),
        ("system", "[task 2] claude: processando"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [
        ("system", "[task 2] claude: iniciando"),
        ("system", "⚙ [task 1] codex: concluída"),
        ("system", "[task 2] claude: processando"),
    ]


def test_compact_deferred_plain_string_items():
    """Mensagens como string simples (formato legado) também são compactadas."""
    deferred = [
        "[task 1] codex: iniciando",
        "[task 1] codex: concluída",
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == ["⚙ [task 1] codex: concluída"]


def test_compact_deferred_falhou_keyword():
    """Terminal 'falhou' suprime transitórios da mesma task."""
    deferred = [
        ("system", "[task 5] codex: executando"),
        ("system", "[task 5] codex: falhou: erro crítico"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [("system", "⚙ [task 5] codex: falhou: erro crítico")]


def test_compact_deferred_cancelado_keyword():
    """Terminal 'cancelado' suprime transitórios da mesma task."""
    deferred = [
        ("system", "[task 3] codex: processando"),
        ("system", "[task 3] codex: cancelado pelo usuário"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [("system", "⚙ [task 3] codex: cancelado pelo usuário")]


def test_flush_deferred_compactacao_integration():
    """flush_deferred_messages aplica compactação antes de renderizar."""
    app = make_app()
    app._deferred_system_messages = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: concluída"),
    ]

    system_layer_from_app(app).flush_deferred_messages()

    assert app.renderer.system_messages == ["⚙ [task 1] codex: concluída"]
    assert app._deferred_system_messages == []


# ---------------------------------------------------------------------------
# T2: Anotação de retries/falhas na conclusão + linha compacta
# ---------------------------------------------------------------------------


def test_compact_deferred_t2_annotates_single_retry():
    """Task com 1 retry (bloqueada): terminal é anotado."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: bloqueada"),
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: concluída"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [("system", "⚙ [task 1] codex: concluída (após 1 tentativas)")]


def test_compact_deferred_t2_annotates_multiple_retries():
    """Múltiplos retries: contagem correta."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: bloqueada"),
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: bloqueada"),
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: concluída"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [("system", "⚙ [task 1] codex: concluída (após 2 tentativas)")]


def test_compact_deferred_t2_annotates_requeue_as_retry():
    """requeue (tentativa N) conta como retry."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("warning", "[task 1] requeue (tentativa 2)"),
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: concluída"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [("system", "⚙ [task 1] codex: concluída (após 1 tentativas)")]


def test_compact_deferred_t2_annotates_sem_resposta_as_retry():
    """sem resposta conta como retry."""
    """sem resposta conta como retry."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: sem resposta"),
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: concluída"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [("system", "⚙ [task 1] codex: concluída (após 1 tentativas)")]


def test_compact_deferred_t2_annotates_erro_as_retry():
    """erro: conta como retry."""
    deferred = [
        ("neutral", "[task 1] codex: iniciando"),
        ("neutral", "[task 1] codex: erro: timeout"),
        ("neutral", "[task 1] codex: iniciando"),
        ("neutral", "[task 1] codex: concluída"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [("neutral", "⚙ [task 1] codex: concluída (após 1 tentativas)")]


def test_compact_deferred_t2_dedup_multiple_terminals():
    """Múltiplas mensagens terminais da mesma task: apenas a última sobrevive."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: concluída"),
        ("system", "[task 1] concluída | aprovada por gemini: resultado ok"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [
        ("system", "⚙ [task 1] concluída | aprovada por gemini: resultado ok"),
    ]


def test_compact_deferred_t2_no_annotation_without_retries():
    """Sem retries: terminal não é anotado."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: processando"),
        ("system", "[task 1] codex: concluída"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [("system", "⚙ [task 1] codex: concluída")]


def test_compact_deferred_t2_annotates_falhou_with_retries():
    """Falha com retries anteriores: terminal falhou é anotado."""
    deferred = [
        ("system", "[task 5] codex: iniciando"),
        ("system", "[task 5] codex: bloqueada"),
        ("system", "[task 5] codex: iniciando"),
        ("system", "[task 5] codex: falhou: erro crítico"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [("system", "⚙ [task 5] codex: falhou: erro crítico (após 1 tentativas)")]


def test_compact_deferred_t2_plain_strings_with_retries():
    """Mensagens como string simples com retries preservam nível."""
    deferred = [
        "[task 1] codex: iniciando",
        "[task 1] codex: bloqueada",
        "[task 1] codex: iniciando",
        "[task 1] codex: concluída",
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == ["⚙ [task 1] codex: concluída (após 1 tentativas)"]


def test_compact_deferred_t2_mixed_tasks_retry():
    """Tasks diferentes: retry de uma não contamina a outra."""
    deferred = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: bloqueada"),
        ("system", "[task 2] claude: processando"),
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: concluída"),
    ]
    result = AppSystemLayer._compact_deferred(deferred)
    assert result == [
        ("system", "[task 2] claude: processando"),
        ("system", "⚙ [task 1] codex: concluída (após 1 tentativas)"),
    ]


def test_flush_deferred_t2_retry_annotation_integration():
    """flush_deferred_messages renderiza terminal com anotação de retry."""
    app = make_app()
    app._deferred_system_messages = [
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: bloqueada"),
        ("system", "[task 1] codex: iniciando"),
        ("system", "[task 1] codex: concluída"),
    ]

    system_layer_from_app(app).flush_deferred_messages()

    assert app.renderer.system_messages == [
        "⚙ [task 1] codex: concluída (após 1 tentativas)",
    ]
    assert app._deferred_system_messages == []


# ---------------------------------------------------------------------------
# s/<agente> (freeze) e r/<agente> (unfreeze)
# ---------------------------------------------------------------------------

def _make_layer_with_pool(agents=None):
    """Cria AppSystemLayer com AgentPool real."""
    agents = agents or ["claude", "codex"]
    pool = AgentPool(agents)
    app = make_app()
    app.agent_pool = pool
    layer = system_layer_from_app(app)
    return layer, pool, app


def test_freeze_congela_pool():
    """Verifica que Test freeze congela pool."""
    layer, pool, _ = _make_layer_with_pool()
    layer.handle_command("s/claude")
    assert pool.frozen_agent == "claude"


def test_freeze_exibe_mensagem_sistema():
    """Verifica que Test freeze exibe mensagem sistema."""
    layer, _, app = _make_layer_with_pool()
    layer.handle_command("s/claude")
    assert any("claude" in msg for msg in app.renderer.system_messages)


def test_freeze_com_trailing_retorna_mensagem():
    """Verifica que Test freeze com trailing retorna mensagem."""
    layer, pool, _ = _make_layer_with_pool()
    result = layer.handle_command("s/claude explique o que fez")
    assert pool.frozen_agent == "claude"
    assert result == "explique o que fez"


def test_freeze_trailing_somente_agente_sem_texto():
    """Verifica que Test freeze trailing somente agente sem texto."""
    layer, _, _ = _make_layer_with_pool()
    result = layer.handle_command("s/claude")
    assert result is True


def test_freeze_agente_desconhecido_avisa_e_nao_congela():
    """Verifica que Test freeze agente desconhecido avisa e nao congela."""
    layer, pool, app = _make_layer_with_pool()
    result = layer.handle_command("s/naoexiste")
    assert pool.frozen_agent is None
    assert any("naoexiste" in msg for msg in app.renderer.warning_messages)
    assert result is True


def test_unfreeze_descongela_pool():
    """Verifica que Test unfreeze descongela pool."""
    layer, pool, _ = _make_layer_with_pool()
    pool.freeze("claude")
    layer.handle_command("r/claude")
    assert pool.frozen_agent is None


def test_unfreeze_exibe_mensagem_sistema():
    """Verifica que Test unfreeze exibe mensagem sistema."""
    layer, pool, app = _make_layer_with_pool()
    pool.freeze("claude")
    layer.handle_command("r/claude")
    assert any("descongelada" in msg or "rotacionar" in msg for msg in app.renderer.system_messages)


def test_freeze_take_primary_retorna_agente_congelado():
    """Verifica que Test freeze take primary retorna agente congelado."""
    layer, pool, _ = _make_layer_with_pool(["claude", "codex"])
    layer.handle_command("s/codex")
    assert pool.take_primary() == "codex"
    assert pool.take_primary() == "codex"


def test_freeze_primary_retorna_agente_congelado():
    """Verifica que primary também respeita o freeze, não retorna _agents[0]."""
    layer, pool, _ = _make_layer_with_pool(["claude", "codex"])
    layer.handle_command("s/codex")
    assert pool.primary == "codex"


def test_unfreeze_retoma_rotacao():
    """Verifica que Test unfreeze retoma rotacao."""
    layer, pool, _ = _make_layer_with_pool(["claude", "codex"])
    pool.freeze("claude")
    pool.unfreeze()
    primaries = {pool.take_primary() for _ in range(4)}
    assert primaries == {"claude", "codex"}


def test_remove_agente_congelado_limpa_estado():
    """remove() do agente congelado deve limpar frozen_agent e orchestrator_agent."""
    pool = AgentPool(["claude", "codex"])
    pool.freeze("claude")
    pool.remove("claude")
    assert pool.frozen_agent is None
    assert pool.orchestrator_agent is None


def test_remove_agente_orquestrador_limpa_estado():
    """remove() do orquestrador deve limpar ambos os campos."""
    pool = AgentPool(["claude", "codex"])
    pool.set_orchestrator("claude")
    pool.remove("claude")
    assert pool.frozen_agent is None
    assert pool.orchestrator_agent is None


def test_set_sem_agente_congelado_limpa_estado():
    """set() com lista sem o agente congelado deve limpar frozen_agent e orchestrator_agent."""
    pool = AgentPool(["claude", "codex"])
    pool.set_orchestrator("claude")
    pool.set(["codex"])
    assert pool.frozen_agent is None
    assert pool.orchestrator_agent is None


def test_readd_apos_remove_nao_reactiva_orquestrador():
    """Re-adicionar agente ao pool após remove() não deve reativar modo orquestrador."""
    pool = AgentPool(["claude", "codex"])
    pool.set_orchestrator("claude")
    pool.remove("claude")
    pool.add("claude")
    assert pool.frozen_agent is None
    assert pool.orchestrator_agent is None
    # primary deve ser codex (primeiro da lista) ou rotacionar, nunca fixo em claude
    primaries = {pool.take_primary() for _ in range(4)}
    assert "codex" in primaries


def test_readd_apos_set_nao_reactiva_orquestrador():
    """Re-adicionar agente via set() após ele ser removido não deve reativar modo orquestrador."""
    pool = AgentPool(["claude", "codex"])
    pool.set_orchestrator("claude")
    pool.set(["codex"])          # claude sai → estado limpo
    pool.set(["codex", "claude"]) # claude volta → não reativa
    assert pool.frozen_agent is None
    assert pool.orchestrator_agent is None


class StructuredRenderer(DummyRenderer):
    """Renderer com canal estruturado de atividade de agente."""

    supports_structured_agent_activity = True

    def __init__(self):
        super().__init__()
        self.retries = []
        self.failovers = []

    def notify_agent_retry(self, agent, *, reason, attempt, limit, detail=""):
        self.retries.append((agent, reason, attempt, limit, detail))

    def notify_agent_failover(self, agent, *, target, message="não respondeu"):
        self.failovers.append((agent, target, message))


def test_notify_agent_retry_uses_structured_channel_when_available():
    """Renderer estruturado recebe os campos separados, sem frase reparseável."""
    renderer = StructuredRenderer()
    app = make_app(renderer=renderer)

    system_layer_from_app(app).notify_agent_retry("codex", "no_response", 1, 2)

    assert renderer.retries == [("codex", "no_response", 1, 2, "")]
    assert renderer.warning_messages == []


def test_notify_agent_failover_uses_structured_channel_when_available():
    """Failover estruturado não vira mensagem de sistema quando há canal próprio."""
    renderer = StructuredRenderer()
    app = make_app(renderer=renderer)

    system_layer_from_app(app).notify_agent_failover("codex", "claude")

    assert renderer.failovers == [("codex", "claude", "não respondeu")]
    assert renderer.system_messages == []


def test_notify_agent_retry_falls_back_to_warning_text_for_legacy_renderer():
    """Renderer legado (sem canal estruturado) recebe frase pt-BR via warning."""
    renderer = DummyRenderer()
    app = make_app(renderer=renderer)

    system_layer_from_app(app).notify_agent_retry("codex", "no_response", 1, 2)

    assert renderer.warning_messages == ["sem resposta · tentativa 1/2"]


def test_notify_agent_failover_falls_back_to_system_text_for_legacy_renderer():
    """Renderer legado recebe frase pt-BR de failover via system message."""
    renderer = DummyRenderer()
    app = make_app(renderer=renderer)

    system_layer_from_app(app).notify_agent_failover("codex", "claude")

    assert renderer.system_messages == ["codex não respondeu, continuando com claude"]

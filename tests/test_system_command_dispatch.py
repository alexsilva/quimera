"""Caracterização do despacho público, sem depender da tabela de implementação."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quimera.app.agent_pool import AgentPool
from quimera.app.system_layer import AppSystemLayer


@pytest.fixture
def layer():
    renderer = SimpleNamespace(
        show_system=Mock(),
        show_warning=Mock(),
        flush=Mock(),
        open_config=Mock(),
    )
    return AppSystemLayer(
        agent_pool=AgentPool(["cli", "openai-compat", "codexcloud", "mcp"]),
        renderer=renderer,
        clear_screen=Mock(),
        context_manager=SimpleNamespace(show=Mock(), edit=Mock(), handle_context_branch=Mock()),
        approval_handler_getter=Mock(return_value=SimpleNamespace(
            pre_approve=Mock(), set_approve_all=Mock(),
        )),
    )


@pytest.mark.parametrize("name", ["bugs", "stats", "task", "debate", "workflow"])
@pytest.mark.parametrize("suffix", ["", " argumento", "  argumento", " \targumento"])
def test_delegated_command_passes_trimmed_input_without_rewriting_arguments(layer, name, suffix):
    handler = Mock(return_value=True)
    setattr(layer, f"{name}_command_handler", handler)
    command = f"/{name}{suffix}"

    assert layer.handle_command(f" \t{command}\n ") is True

    handler.assert_called_once_with(command)


@pytest.mark.parametrize("name", ["bugs", "stats", "task", "debate", "workflow"])
@pytest.mark.parametrize("result", [False, None, "resposta"])
def test_delegated_command_preserves_return_value_policy(layer, name, result):
    setattr(layer, f"{name}_command_handler", Mock(return_value=result))

    expected = bool(result) if name in {"bugs", "stats"} else True
    assert layer.handle_command(f"/{name}") is expected


@pytest.mark.parametrize("name", ["bugs", "stats", "task", "debate", "workflow"])
def test_callback_is_resolved_at_each_call_instead_of_captured_at_construction(layer, name):
    original = Mock(return_value=True)
    replacement = Mock(return_value=True)
    attribute = f"{name}_command_handler"
    setattr(layer, attribute, original)
    assert layer.handle_command(f"/{name}") is True
    setattr(layer, attribute, replacement)
    assert layer.handle_command(f"/{name} novo") is True

    original.assert_called_once_with(f"/{name}")
    replacement.assert_called_once_with(f"/{name} novo")


@pytest.mark.parametrize("name", ["bugs", "stats", "task", "debate", "workflow"])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_dispatch_does_not_swallow_callback_exceptions(layer, name, error_type):
    error = error_type("erro do handler")
    setattr(layer, f"{name}_command_handler", Mock(side_effect=error))

    with pytest.raises(error_type) as caught:
        layer.handle_command(f"/{name}")
    assert caught.value is error


@pytest.mark.parametrize("command", [
    "/help", "/agents", "/clear", "/reload", "/approve", "/approve-all", "/config",
    "/g", "/r", "/y", "/a", "/aa",
])
def test_exact_commands_and_aliases_do_not_accept_arguments(layer, command):
    assert layer.handle_command(f"{command} argumento") is False


@pytest.mark.parametrize("command", [
    "/bugs", "/stats", "/connect", "/disconnect", "/prompt", "/debate", "/workflow",
    "/reset", "/policy", "/sandbox", "/context-edit", "/context-branch", "/context",
    "/stop",
])
@pytest.mark.parametrize("suffix", ["suffix", "\targumento", "\nargumento", "/argumento"])
def test_argument_commands_require_literal_space_boundary(layer, command, suffix):
    assert layer.handle_command(command + suffix) is False


@pytest.mark.parametrize("command", ["/tasking", "/task/1", "/task\targumento", "/task\nargumento"])
def test_task_keeps_its_historical_unrestricted_prefix_matching(layer, command):
    layer.task_command_handler = Mock(return_value=False)

    assert layer.handle_command(command) is True

    layer.task_command_handler.assert_called_once_with(command)


@pytest.mark.parametrize("command", [
    "/help", "/agents", "/bugs", "/stats", "/connect cli", "/disconnect cli", "/clear",
    "/reload", "/prompt", "/task", "/debate", "/workflow", "/reset", "/approve-all",
    "/approve", "/policy", "/sandbox", "/context-edit", "/context-branch", "/context",
    "/stop",
    "s/cli", "o/cli", "r/", "/g", "/r", "/aa",
])
def test_renderer_guard_keeps_all_other_commands_out_of_handlers(layer, command):
    layer._display.renderer = None
    layer.task_command_handler = Mock()

    assert layer.handle_command(command) is False

    layer.task_command_handler.assert_not_called()
    assert layer.agent_pool.frozen_agent is None


@pytest.mark.parametrize("renderer_available", [True, False])
def test_config_is_consumed_even_before_renderer_initialization(layer, renderer_available):
    renderer = layer._display.renderer
    if not renderer_available:
        layer._display.renderer = None

    assert layer.handle_command(" \t/config\n") is True

    assert renderer.open_config.call_count == int(renderer_available)


def test_config_is_consumed_when_renderer_does_not_offer_a_modal(layer):
    layer._display.renderer = SimpleNamespace()
    assert layer.handle_command("/config") is True


def test_stop_command_uses_bound_selective_stopper(layer):
    result = SimpleNamespace(message="Execução #2 codex parada.")
    stopper = Mock(return_value=result)
    layer.bind_chat_execution_stopper(stopper)

    assert layer.handle_command("/stop #2") is True

    stopper.assert_called_once_with("#2")
    layer._display.renderer.show_system.assert_called_once_with(result.message)


def test_stop_command_is_consumed_when_lifecycle_is_unavailable(layer):
    assert layer.handle_command("/stop") is True

    assert "indisponível" in layer._display.renderer.show_warning.call_args.args[0]


def test_renderer_provider_is_resolved_once_per_dispatch_and_not_cached(layer):
    first = layer._display.renderer
    second = SimpleNamespace(show_system=Mock(), open_config=Mock())
    provider = Mock(spec=[], side_effect=[first, second, None])
    layer._display.renderer = provider

    assert layer.handle_command("/agents") is True
    assert layer.handle_command("/config") is True
    assert layer.handle_command("/agents") is False

    assert provider.call_count == 3
    first.show_system.assert_called_once()
    second.open_config.assert_called_once_with()
    second.show_system.assert_not_called()


@pytest.mark.parametrize(("alias", "target"), [
    ("/g", "help"), ("/r", "context"), ("/y", "approve"), ("/a", "approve"),
    ("/aa", "approve-all"),
])
def test_alias_dispatches_the_same_action(layer, alias, target):
    approval = layer.approval_handler_getter.return_value

    assert layer.handle_command(f"  {alias}  ") is True

    if target == "help":
        message = layer._display.renderer.show_system.call_args.args[0]
        assert "Comandos:" in message
    elif target == "context":
        layer.context_manager.show.assert_called_once_with()
    elif target == "approve":
        approval.pre_approve.assert_called_once_with()
        approval.set_approve_all.assert_not_called()
    else:
        approval.set_approve_all.assert_called_once_with(True)
        approval.pre_approve.assert_not_called()


@pytest.mark.parametrize("command", ["", "texto normal", "/nao-existe", "/HELP", "/e", "s/", "o/"])
def test_unhandled_inputs_remain_available_to_the_chat(layer, command):
    assert layer.handle_command(command) is False


@pytest.mark.parametrize("agent", ["cli", "openai-compat", "codexcloud", "mcp"])
@pytest.mark.parametrize("prefix", ["s/", "o/"])
def test_routing_modes_preserve_trailing_message_for_every_agent_family(layer, agent, prefix):
    assert layer.handle_command(f"{prefix}{agent}  mensagem\nem duas linhas ") == "mensagem\nem duas linhas"
    assert layer.agent_pool.frozen_agent == agent
    assert layer.agent_pool.orchestrator_agent == (agent if prefix == "o/" else None)


@pytest.mark.parametrize("command", ["r/", "r/qualquer-coisa", "r/ argumento"])
def test_rotation_reset_keeps_prefix_matching(layer, command):
    layer.agent_pool.set_orchestrator("cli")

    assert layer.handle_command(command) is True

    assert layer.agent_pool.frozen_agent is None
    assert layer.agent_pool.orchestrator_agent is None


def test_context_prefixes_do_not_steal_each_others_subcommands(layer):
    assert layer.handle_command("/context-edit ignorado") is True
    assert layer.handle_command("/context-branch main") is True
    assert layer.handle_command("/context show") is True

    layer.context_manager.edit.assert_called_once_with()
    layer.context_manager.handle_context_branch.assert_called_once_with("/context-branch main")
    layer.context_manager.show.assert_called_once_with()

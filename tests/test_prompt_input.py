"""Tests for quimera.app.prompt_input — T-008: migração de input."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from quimera.app.prompt_input import (
    InputGate,
    _PT_AVAILABLE,
)


# ---------------------------------------------------------------------------
# Toolbar HTML
# ---------------------------------------------------------------------------

class TestBuildToolbar:
    def test_toolbar_none_without_prompt_toolkit(self):
        gate = InputGate.__new__(InputGate)
        gate._renderer = None
        gate._lock = threading.Lock()
        gate._session = None
        with patch("quimera.app.prompt_input._PT_AVAILABLE", False):
            assert gate._build_toolbar() is None

    def test_placeholder_none_without_prompt_toolkit(self):
        gate = InputGate.__new__(InputGate)
        gate._renderer = None
        gate._toolbar_context_resolver = None
        gate._lock = threading.Lock()
        gate._session = None
        with patch("quimera.app.prompt_input._PT_AVAILABLE", False):
            assert gate._build_placeholder() is None

    @pytest.mark.skipif(not _PT_AVAILABLE, reason="prompt_toolkit não disponível")
    def test_toolbar_includes_responder_model_and_cwd_when_context_available(self):
        gate = InputGate(
            toolbar_context_resolver=lambda: {
                "responder": "claude",
                "model": "gpt-5",
                "cwd": "/tmp/projeto",
            }
        )
        toolbar = gate._build_toolbar()
        assert callable(toolbar)
        content = toolbar()
        assert "claude" in str(content)
        assert "responde:" not in str(content)
        assert "gpt-5" in str(content)
        assert "/tmp/projeto" in str(content)

    @pytest.mark.skipif(not _PT_AVAILABLE, reason="prompt_toolkit não disponível")
    def test_toolbar_includes_theme_when_context_available(self):
        gate = InputGate(
            toolbar_context_resolver=lambda: {
                "theme": "chat",
            }
        )
        toolbar = gate._build_toolbar()
        assert callable(toolbar)
        assert "tema:chat" in str(toolbar())

    @pytest.mark.skipif(not _PT_AVAILABLE, reason="prompt_toolkit não disponível")
    def test_toolbar_includes_parallel_status_when_context_available(self):
        gate = InputGate(
            toolbar_context_resolver=lambda: {
                "parallel": "paralelo:1/1 · fila:2",
            }
        )
        toolbar = gate._build_toolbar()
        assert callable(toolbar)
        content = str(toolbar())
        assert "paralelo:1/1" in content
        assert "fila:2" in content


class TestKeyBindings:
    def test_key_bindings_none_without_prompt_toolkit(self):
        gate = InputGate.__new__(InputGate)
        gate._theme_cycle_handler = lambda: None
        with patch("quimera.app.prompt_input._PT_AVAILABLE", False):
            assert gate._build_key_bindings() is None

    @pytest.mark.skipif(not _PT_AVAILABLE, reason="prompt_toolkit não disponível")
    def test_key_bindings_none_without_theme_handler(self):
        gate = InputGate()
        assert gate._build_key_bindings() is None

    @pytest.mark.skipif(not _PT_AVAILABLE, reason="prompt_toolkit não disponível")
    def test_ctrl_t_binding_calls_handler_and_invalidates_prompt(self):
        gate = InputGate()
        handler = MagicMock()
        gate.set_theme_cycle_handler(handler)

        key_bindings = gate._build_key_bindings()
        assert key_bindings is not None
        bindings = key_bindings.bindings
        assert len(bindings) == 3

        normalized_keys = {tuple(str(k) for k in binding.keys) for binding in bindings}
        assert normalized_keys == {
            ("Keys.ControlT",),
            ("Keys.Escape", "t"),
            ("Keys.F6",),
        }
        assert all(bool(binding.eager()) is True for binding in bindings)

        for binding in bindings:
            event = MagicMock()
            binding.handler(event)
            event.app.invalidate.assert_called_once_with()
        assert handler.call_count == 3


# ---------------------------------------------------------------------------
# Renderer flush (coordenação Rich.Live)
# ---------------------------------------------------------------------------

class TestFlushRenderer:
    def test_flush_called_on_renderer(self):
        renderer = MagicMock()
        gate = InputGate(renderer=renderer)
        gate._flush_renderer()
        renderer.flush.assert_called_once()

    def test_flush_tolerates_missing_flush(self):
        renderer = object()  # sem método flush
        gate = InputGate(renderer=renderer)
        gate._flush_renderer()  # não deve lançar

    def test_flush_tolerates_none_renderer(self):
        gate = InputGate(renderer=None)
        gate._flush_renderer()  # não deve lançar

    def test_flush_tolerates_flush_exception(self):
        renderer = MagicMock()
        renderer.flush.side_effect = RuntimeError("erro de flush")
        gate = InputGate(renderer=renderer)
        gate._flush_renderer()  # exceção deve ser suprimida


# ---------------------------------------------------------------------------
# __call__ — gate de input
# ---------------------------------------------------------------------------

class TestInputGateCall:
    def test_fallback_to_builtin_input_when_no_session(self):
        gate = InputGate.__new__(InputGate)
        gate._renderer = None
        gate._lock = threading.Lock()
        gate._session = None

        with patch("builtins.input", return_value="resposta") as mock_input:
            result = gate(">>> ")
        mock_input.assert_called_once_with(">>> ")
        assert result == "resposta"

    def test_flush_called_before_prompt(self):
        renderer = MagicMock()
        gate = InputGate.__new__(InputGate)
        gate._renderer = renderer
        gate._lock = threading.Lock()
        gate._session = None

        calls = []
        renderer.flush.side_effect = lambda: calls.append("flush")

        with patch("builtins.input", side_effect=lambda p: calls.append("input") or "x"):
            gate(">>> ")

        assert calls[0] == "flush", "flush deve ocorrer antes do input"
        assert "input" in calls

    @pytest.mark.skipif(not _PT_AVAILABLE, reason="prompt_toolkit não disponível")
    def test_uses_session_when_available(self):
        gate = InputGate()
        gate._session = MagicMock()
        gate._session.prompt.return_value = "via session"

        result = gate(">>> ")
        assert result == "via session"
        gate._session.prompt.assert_called_once()

    @pytest.mark.skipif(not _PT_AVAILABLE, reason="prompt_toolkit não disponível")
    def test_session_receives_toolbar_and_placeholder(self):
        gate = InputGate()
        gate._session = MagicMock()
        gate._session.prompt.return_value = ""

        gate(">>> ")
        _, kwargs = gate._session.prompt.call_args
        assert "bottom_toolbar" in kwargs
        assert "placeholder" in kwargs


# ---------------------------------------------------------------------------
# _SlashCommandCompleter — argument_resolver
# ---------------------------------------------------------------------------

class TestSlashCommandCompleterArgumentResolver:
    """Testa o argument_resolver no _SlashCommandCompleter."""

    def test_no_argument_resolver_returns_nothing_on_space(self):
        """Sem argument_resolver, texto com espaço não produz sugestões."""
        from quimera.app.prompt_input import _SlashCommandCompleter
        resolver = MagicMock(return_value=["/context", "/help"])
        completer = _SlashCommandCompleter(resolver, argument_resolver=None)

        doc = MagicMock()
        doc.text_before_cursor = "/context f"
        results = list(completer.get_completions(doc, None))
        assert results == []

    def test_argument_resolver_called_with_command_and_partial(self):
        """argument_resolver recebe (command, partial) quando há espaço."""
        from quimera.app.prompt_input import _SlashCommandCompleter
        arg_resolver = MagicMock(return_value=["feature_x", "feature_y"])
        completer = _SlashCommandCompleter(MagicMock(return_value=[]), argument_resolver=arg_resolver)

        doc = MagicMock()
        doc.text_before_cursor = "/context-branch f"
        results = list(completer.get_completions(doc, None))

        arg_resolver.assert_called_once_with("/context-branch", "f")
        assert len(results) == 2

    def test_argument_resolver_filters_by_prefix(self):
        """Sugestões são filtradas pelo que o usuário já digitou após o espaço."""
        from quimera.app.prompt_input import _SlashCommandCompleter
        arg_resolver = MagicMock(return_value=["feature_x", "other_branch"])
        completer = _SlashCommandCompleter(MagicMock(return_value=[]), argument_resolver=arg_resolver)

        doc = MagicMock()
        doc.text_before_cursor = "/context-branch f"
        results = list(completer.get_completions(doc, None))

        assert len(results) == 1
        assert results[0].text == "feature_x"

    def test_argument_resolver_tolerates_exception(self):
        """Exceção no argument_resolver não propaga; retorna lista vazia."""
        from quimera.app.prompt_input import _SlashCommandCompleter

        def failing_resolver(cmd, partial):
            raise RuntimeError("fail")

        completer = _SlashCommandCompleter(MagicMock(return_value=[]), argument_resolver=failing_resolver)

        doc = MagicMock()
        doc.text_before_cursor = "/context-branch x"
        results = list(completer.get_completions(doc, None))
        assert results == []

    def test_no_space_delegates_to_command_resolver(self):
        """Sem espaço, comportamento padrão de completar comandos slash."""
        from quimera.app.prompt_input import _SlashCommandCompleter
        cmd_resolver = MagicMock(return_value=["/context", "/context-branch", "/help"])
        arg_resolver = MagicMock()
        completer = _SlashCommandCompleter(cmd_resolver, argument_resolver=arg_resolver)

        doc = MagicMock()
        doc.text_before_cursor = "/context"
        results = list(completer.get_completions(doc, None))

        # command_resolver foi chamado, argument_resolver não
        cmd_resolver.assert_called_once()
        arg_resolver.assert_not_called()
        texts = [r.text for r in results]
        assert "/context" in texts
        assert "/context-branch" in texts

    def test_non_slash_returns_empty(self):
        """Texto sem / não aciona nenhum resolver."""
        from quimera.app.prompt_input import _SlashCommandCompleter
        cmd_resolver = MagicMock()
        arg_resolver = MagicMock()
        completer = _SlashCommandCompleter(cmd_resolver, argument_resolver=arg_resolver)

        doc = MagicMock()
        doc.text_before_cursor = "hello"
        results = list(completer.get_completions(doc, None))
        assert results == []
        cmd_resolver.assert_not_called()
        arg_resolver.assert_not_called()


# ---------------------------------------------------------------------------
# InputGate — argument_resolver integration
# ---------------------------------------------------------------------------

class TestInputGateArgumentResolver:
    """Testa passagem de argument_resolver do InputGate para o completer."""

    def test_build_completer_returns_none_without_command_resolver(self):
        """Sem command_resolver, _build_completer retorna None."""
        gate = InputGate(argument_resolver=MagicMock())
        assert gate._build_completer() is None

    def test_build_completer_includes_argument_resolver(self):
        """_build_completer retorna _SlashCommandCompleter com argument_resolver."""
        from quimera.app.prompt_input import _SlashCommandCompleter
        arg_resolver = MagicMock()
        gate = InputGate(
            command_resolver=MagicMock(return_value=["/test"]),
            argument_resolver=arg_resolver,
        )
        completer = gate._build_completer()
        assert completer is not None
        assert isinstance(completer, _SlashCommandCompleter)
        assert completer._argument_resolver is arg_resolver

    def test_set_argument_resolver_updates_completer(self):
        """set_argument_resolver altera o resolver usado no completer."""
        from quimera.app.prompt_input import _SlashCommandCompleter
        gate = InputGate(command_resolver=MagicMock(return_value=["/test"]))
        arg_resolver = MagicMock()
        gate.set_argument_resolver(arg_resolver)

        completer = gate._build_completer()
        assert completer._argument_resolver is arg_resolver

    def test_argument_resolver_none_by_default(self):
        """Sem argument_resolver no __init__, o atributo é None."""
        gate = InputGate(command_resolver=MagicMock(return_value=["/test"]))
        assert gate._argument_resolver is None

        completer = gate._build_completer()
        assert completer._argument_resolver is None

    def test_set_argument_resolver_can_be_set_to_none(self):
        """set_argument_resolver(None) limpa o resolver."""
        gate = InputGate(
            command_resolver=MagicMock(return_value=["/test"]),
            argument_resolver=MagicMock(),
        )
        gate.set_argument_resolver(None)
        assert gate._argument_resolver is None


# ---------------------------------------------------------------------------
# Core — _command_argument_resolver integration
# ---------------------------------------------------------------------------

class TestCoreCommandArgumentResolver:
    """Testa o _command_argument_resolver de core.py."""

    def test_returns_empty_for_unknown_command(self):
        """Comando não reconhecido retorna lista vazia."""
        from quimera.app.core import QuimeraApp
        resolver = QuimeraApp._command_argument_resolver
        result = resolver(None, "/unknown", "")
        assert result == []

    def test_returns_branches_for_context_branch(self, tmp_path):
        """Para /context-branch, retorna branches do diretório data/context."""
        from quimera.app.core import QuimeraApp
        from quimera.workspace import Workspace

        ws_root = tmp_path / "test_ws"
        ws_root.mkdir(parents=True, exist_ok=True)
        ws = Workspace(ws_root)
        ctx_root = ws._root / "data" / "context"
        ctx_root.mkdir(parents=True, exist_ok=True)
        (ctx_root / "feature_x").mkdir(exist_ok=True)
        (ctx_root / "feature_y").mkdir(exist_ok=True)

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = ws

        result = app._command_argument_resolver("/context-branch", "")
        assert "feature_x" in result
        assert "feature_y" in result

    def test_includes_active_branch(self, tmp_path):
        """A branch ativa do workspace também aparece nas sugestões."""
        from quimera.app.core import QuimeraApp
        from quimera.workspace import Workspace

        ws = Workspace(tmp_path)
        ws._branch = "current_feature"

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = ws
        # Garantir que o ctx_dir existe para não gerar erro
        (ws._root / "data" / "context").mkdir(parents=True, exist_ok=True)

        result = app._command_argument_resolver("/context-branch", "")
        assert "current_feature" in result

    def test_returns_connected_agents_for_disconnect(self):
        """/disconnect sugere agentes com conexão persistida."""
        from quimera.app.core import QuimeraApp
        from unittest.mock import MagicMock

        app = QuimeraApp.__new__(QuimeraApp)
        app.system_layer = MagicMock()
        app.system_layer.list_connected_agents.return_value = ["chatgpt", "codex"]
        result = app._command_argument_resolver("/disconnect", "")
        assert result == ["chatgpt", "codex"]
        app.system_layer.list_connected_agents.assert_called_once_with()

    def test_returns_bugs_subcommands_for_bugs_command(self):
        """Para /bugs, retorna ações suportadas de diagnóstico."""
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        result = app._command_argument_resolver("/bugs", "")
        assert result == ["list", "show", "close", "analyze", "stats"]

    def test_returns_empty_for_disconnect_without_connections(self):
        """Sem conexões persistidas, /disconnect não sugere argumentos."""
        from quimera.app.core import QuimeraApp
        from unittest.mock import MagicMock

        app = QuimeraApp.__new__(QuimeraApp)
        app.system_layer = MagicMock()
        app.system_layer.list_connected_agents.return_value = []
        result = app._command_argument_resolver("/disconnect", "")
        assert result == []
        app.system_layer.list_connected_agents.assert_called_once_with()


# ---------------------------------------------------------------------------
# Singleton — InputGate instanciado em core.py
# ---------------------------------------------------------------------------

class TestCoreIntegration:
    def test_input_gate_instantiated_in_core(self):
        """core.py deve expor input_gate como instância de InputGate."""
        from quimera.app import core as core_module
        import inspect

        src = inspect.getsource(core_module)
        assert "InputGate" in src, "core.py deve importar e usar InputGate"

    def test_input_services_uses_input_gate(self):
        """AppInputServices deve receber input_resolver que retorna o InputGate."""
        from quimera.app.inputs import AppInputServices
        import inspect

        src = inspect.getsource(AppInputServices)
        assert "input_resolver" in src or "input_gate" in src

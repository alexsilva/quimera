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
        assert toolbar is not None
        assert "claude" in str(toolbar)
        assert "gpt-5" in str(toolbar)
        assert "/tmp/projeto" in str(toolbar)


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

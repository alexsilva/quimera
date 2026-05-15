"""Testes para quimera.app.dispatch — 100% de cobertura."""
from unittest.mock import MagicMock, Mock, call, patch, PropertyMock, sentinel
import pytest
import time
import re
import threading

from quimera.app.dispatch import AppDispatchServices
from quimera.app.tool_loop import (
    _coerce_tool_error,
    _invalid_tool_signature,
    _resolve_tool_error_type,
)
from quimera.runtime.errors import (
    ToolError,
    ToolValidationError,
    ToolEnvironmentError,
    ToolLogicError,
    ToolRateLimitError,
)


# =============================================================================
# Funções helpers livres
# =============================================================================

class TestCoerceToolError:
    """_coerce_tool_error"""

    def test_none_returns_none(self):
        assert _coerce_tool_error(None) is None

    def test_toolerror_returns_unchanged(self):
        te = ToolError("msg")
        assert _coerce_tool_error(te) is te

    def test_string_validation_heuristic(self):
        """'validação' → ToolValidationError"""
        result = _coerce_tool_error("erro de validação: campo obrigatório")
        assert isinstance(result, ToolValidationError)

    def test_string_campo_heuristic(self):
        """'campo' → ToolValidationError"""
        result = _coerce_tool_error("campo inválido")
        assert isinstance(result, ToolValidationError)

    def test_string_formato_heuristic(self):
        """'formato' → ToolValidationError"""
        result = _coerce_tool_error("formato incorreto")
        assert isinstance(result, ToolValidationError)

    def test_string_arquivo_heuristic(self):
        """'arquivo' → ToolEnvironmentError"""
        result = _coerce_tool_error("arquivo não encontrado")
        assert isinstance(result, ToolEnvironmentError)

    def test_string_permissao_heuristic(self):
        """'permissão' → ToolEnvironmentError"""
        result = _coerce_tool_error("permissão negada")
        assert isinstance(result, ToolEnvironmentError)

    def test_string_nao_encontrado_heuristic(self):
        """'não encontrado' → ToolEnvironmentError"""
        result = _coerce_tool_error("recurso não encontrado")
        assert isinstance(result, ToolEnvironmentError)

    def test_string_regra_heuristic(self):
        """'regra' → ToolLogicError"""
        result = _coerce_tool_error("regra violada")
        assert isinstance(result, ToolLogicError)

    def test_string_logica_heuristic(self):
        """'lógica' → ToolLogicError"""
        result = _coerce_tool_error("erro lógica")
        assert isinstance(result, ToolLogicError)

    def test_string_contradiz_heuristic(self):
        """'contradiz' → ToolLogicError"""
        result = _coerce_tool_error("contradiz regra anterior")
        assert isinstance(result, ToolLogicError)

    def test_string_rate_limit_heuristic(self):
        """'rate limit' → ToolRateLimitError"""
        result = _coerce_tool_error("rate limit excedido")
        assert isinstance(result, ToolRateLimitError)

    def test_string_throttling_heuristic(self):
        """'throttling' → ToolRateLimitError"""
        result = _coerce_tool_error("throttling ativo")
        assert isinstance(result, ToolRateLimitError)

    def test_unknown_string_returns_unchanged(self):
        """String sem heuristicas conhecidas retorna a string original"""
        result = _coerce_tool_error("some random string")
        assert result == "some random string"

    def test_non_string_non_toolerror_returns_unchanged(self):
        assert _coerce_tool_error(42) == 42
        assert _coerce_tool_error([1, 2]) == [1, 2]


class TestInvalidToolSignature:
    """_invalid_tool_signature"""

    def test_returns_tuple_of_three(self):
        tool_result = MagicMock()
        tool_result.error = "some error"
        tool_result.tool_name = "test_tool"
        sig = _invalid_tool_signature(tool_result, "policy")
        assert len(sig) == 3
        assert sig[0] == "policy"
        assert sig[1] == "test_tool"
        assert "some error" in sig[2]

    def test_truncates_long_error_text(self):
        tool_result = MagicMock()
        tool_result.error = "x" * 500
        tool_result.tool_name = ""
        sig = _invalid_tool_signature(tool_result, "policy")
        assert len(sig[2]) <= 256

    def test_normalizes_whitespace(self):
        tool_result = MagicMock()
        tool_result.error = "  ERROR   MESSAGE  "
        tool_result.tool_name = ""
        sig = _invalid_tool_signature(tool_result, "policy")
        assert "  " not in sig[2]

    def test_lowercases_error(self):
        tool_result = MagicMock()
        tool_result.error = "ERROR text"
        tool_result.tool_name = ""
        sig = _invalid_tool_signature(tool_result, "policy")
        assert sig[2] == "error text"


class TestResolveToolErrorType:
    """_resolve_tool_error_type"""

    def test_error_type_attr_returns_it(self):
        tr = MagicMock()
        tr.error_type = "policy"
        assert _resolve_tool_error_type(tr) == "policy"

    def test_empty_error_type_returns_none(self):
        tr = MagicMock()
        tr.error_type = ""
        assert _resolve_tool_error_type(tr) == "none"

    def test_none_error_type_returns_none(self):
        tr = MagicMock()
        tr.error_type = None
        assert _resolve_tool_error_type(tr) == "none"


class TestSanitizeSpyTurnDetail:
    """_sanitize_spy_turn_detail — método de classe"""

    def test_non_dict_returns_none(self):
        assert AppDispatchServices._sanitize_spy_turn_detail("not dict") is None

    def test_truncates_tools_list(self):
        tools = [{"tool_call_id": str(i)} for i in range(20)]
        detail = {"turn_id": "t1", "tools": tools}
        result = AppDispatchServices._sanitize_spy_turn_detail(detail)
        assert result is not None
        assert len(result["tools"]) == AppDispatchServices._MAX_SPY_TOOLS  # 12
        assert result["truncated_tools"] is True

    def test_no_truncation_if_within_limit(self):
        tools = [{"tool_call_id": str(i)} for i in range(3)]
        detail = {"turn_id": "t1", "tools": tools}
        result = AppDispatchServices._sanitize_spy_turn_detail(detail)
        assert len(result["tools"]) == 3
        assert result["truncated_tools"] is False

    def test_non_list_tools_defaults_empty(self):
        detail = {"turn_id": "t1", "tools": None}
        result = AppDispatchServices._sanitize_spy_turn_detail(detail)
        assert result["tools"] == []

    def test_non_dict_tool_skipped(self):
        detail = {"turn_id": "t1", "tools": ["not a dict"]}
        result = AppDispatchServices._sanitize_spy_turn_detail(detail)
        assert result["tools"] == []

    def test_truncate_spy_text_short_string(self):
        assert AppDispatchServices._truncate_spy_text("hello") == "hello"

    def test_truncate_spy_text_long_string(self):
        long_str = "a" * 500
        result = AppDispatchServices._truncate_spy_text(long_str)
        assert len(result) <= AppDispatchServices._MAX_SPY_TEXT_CHARS

    def test_truncate_spy_text_non_string(self):
        assert AppDispatchServices._truncate_spy_text(42) == 42

    def test_sanitize_spy_map_non_dict(self):
        assert AppDispatchServices._sanitize_spy_map("not dict") is None

    def test_sanitize_spy_map_empty_dict(self):
        assert AppDispatchServices._sanitize_spy_map({}) is None

    def test_sanitize_spy_map_truncates_items(self):
        payload = {str(i): i for i in range(20)}
        result = AppDispatchServices._sanitize_spy_map(payload)
        assert result is not None
        assert len(result) <= AppDispatchServices._MAX_SPY_MAP_ITEMS

    def test_sanitize_spy_map_preserves_simple_types(self):
        payload = {"a": 1, "b": 2.5, "c": True, "d": None}
        result = AppDispatchServices._sanitize_spy_map(payload)
        assert result["a"] == 1
        assert result["b"] == 2.5
        assert result["c"] is True
        assert result["d"] is None

    def test_sanitize_spy_map_truncates_long_values(self):
        payload = {"key": "x" * 500}
        result = AppDispatchServices._sanitize_spy_map(payload)
        assert len(result["key"]) <= AppDispatchServices._MAX_SPY_TEXT_CHARS

    def test_sanitize_spy_map_converts_non_string_value(self):
        payload = {"key": object()}
        result = AppDispatchServices._sanitize_spy_map(payload)
        assert isinstance(result["key"], str)


# =============================================================================
# AppDispatchServices — resolve_agent_response
# =============================================================================

@pytest.fixture
def dispatch_app():
    """Cria app mock com todos os atributos que dispatch precisa."""
    app = MagicMock()
    app.tool_executor = MagicMock()
    app.task_services = MagicMock()
    app.task_services.truncate_payload = MagicMock(side_effect=lambda x: x)
    app.get_agent_plugin = MagicMock(return_value=MagicMock())
    app.prompt_builder = MagicMock()
    app.agent_client = MagicMock()
    app.agent_client._user_cancelled = False
    app.agent_client._cancel_event = threading.Event()
    app.renderer = MagicMock()
    app.shared_state = {}
    app.session_state = {
        "session_id": "test-session",
        "handoffs_sent": 0,
        "handoffs_received": 0,
    }
    app.session_call_index = 0
    app.round_index = 0
    app.history = []
    app.execution_mode = "standard"
    app.debug_prompt_metrics = False
    app.session_metrics = MagicMock()
    app._output_lock = MagicMock()
    app._counter_lock = MagicMock()
    app.MAX_RETRIES = 2
    app.RETRY_BACKOFF_SECONDS = 0.01
    app.RATE_LIMIT_BACKOFF_SECONDS = 0.01
    app.session_services = MagicMock()
    app.print_response = MagicMock()
    app.record_failure = MagicMock()
    return app


class TestResolveAgentResponse:
    """Testes para AppDispatchServices.resolve_agent_response"""

    def test_empty_response_returns_none(self, dispatch_app):
        ds = AppDispatchServices(dispatch_app)
        assert ds.resolve_agent_response("agent1", None) is None
        assert ds.resolve_agent_response("agent1", "") == ""

    def test_no_tool_result_returns_raw(self, dispatch_app):
        dispatch_app.tool_executor.maybe_execute_from_response = MagicMock(
            return_value=("raw", None)
        )
        ds = AppDispatchServices(dispatch_app)
        result = ds.resolve_agent_response("agent1", "hello")
        assert result == "hello"

    def test_tool_result_with_error_coerced(self, dispatch_app):
        """tool_result.error é string coerida para ToolError"""
        tool_result = MagicMock()
        tool_result.error = "erro de validação"
        tool_result.ok = False
        # Retorna tool_result no hop 0, depois sem tool no hop 1
        dispatch_app.tool_executor.maybe_execute_from_response = MagicMock(
            side_effect=[
                ("some cmd", tool_result),
                (None, None),
            ]
        )
        # Para que não atinja threshold de loop
        plugin = dispatch_app.get_agent_plugin("agent1")
        plugin.tool_use_reliability = "high"
        ds = AppDispatchServices(dispatch_app)
        result = ds.resolve_agent_response("agent1", "some cmd")
        # Deve ter saido pelo segundo hop (sem tool)
        assert result is not None

    def test_consecutive_invalid_loop_aborts(self, dispatch_app):
        """consecutive_invalid_signature_count >= max → aborta com mensagem"""
        tool_result = MagicMock()
        tool_result.error = "erro de validação"
        tool_result.ok = False
        tool_result.error_type = "policy"
        dispatch_app.tool_executor.maybe_execute_from_response = MagicMock(
            return_value=("some cmd", tool_result)
        )
        plugin = dispatch_app.get_agent_plugin("agent1")
        plugin.tool_use_reliability = "low"  # threshold baixo
        ds = AppDispatchServices(dispatch_app)
        result = ds.resolve_agent_response("agent1", "some cmd")
        assert "loop de ferramenta inválida" in (result or "")

    def test_max_tool_hops_hits_returns_abort_message(self, dispatch_app):
        """Atinge max_tool_hops → retorna mensagem de limite"""
        tool_result = MagicMock()
        tool_result.error = None
        tool_result.ok = True
        tool_result.to_model_payload = MagicMock(return_value="{}")
        dispatch_app.tool_executor.maybe_execute_from_response = MagicMock(
            return_value=("some cmd", tool_result)
        )
        plugin = dispatch_app.get_agent_plugin("agent1")
        plugin.tool_use_reliability = "low"  # max_tool_hops baixo
        ds = AppDispatchServices(dispatch_app)
        result = ds.resolve_agent_response("agent1", "some cmd")
        assert "limite de execuções de ferramenta" in (result or "")

    def test_visible_text_printed_and_persisted(self, dispatch_app):
        """visible_text é printado e persistido"""
        tool_result = MagicMock()
        tool_result.error = None
        tool_result.ok = True
        tool_result.to_model_payload = MagicMock(return_value="{}")

        def _maybe_exec(resp):
            if hasattr(_maybe_exec, "called"):
                return (None, None)
            _maybe_exec.called = True
            return (resp, tool_result)

        dispatch_app.tool_executor.maybe_execute_from_response = MagicMock(side_effect=_maybe_exec)
        plugin = dispatch_app.get_agent_plugin("agent1")
        plugin.tool_use_reliability = "high"
        ds = AppDispatchServices(dispatch_app)
        ds.resolve_agent_response("agent1", "some text with tool", show_output=True, persist_history=True)
        dispatch_app.print_response.assert_called()
        dispatch_app.session_services.persist_message.assert_called()

    def test_visible_text_show_output_false(self, dispatch_app):
        """show_output=False → não printa"""
        tool_result = MagicMock()
        tool_result.error = None
        tool_result.ok = True
        tool_result.to_model_payload = MagicMock(return_value="{}")

        def _maybe_exec(resp):
            if hasattr(_maybe_exec, "called"):
                return (None, None)
            _maybe_exec.called = True
            return (resp, tool_result)

        dispatch_app.tool_executor.maybe_execute_from_response = MagicMock(side_effect=_maybe_exec)
        plugin = dispatch_app.get_agent_plugin("agent1")
        plugin.tool_use_reliability = "high"
        ds = AppDispatchServices(dispatch_app)
        ds.resolve_agent_response("agent1", "some text with tool", show_output=False)
        dispatch_app.print_response.assert_not_called()

    def test_finally_resets_approve_all(self, dispatch_app):
        """finally chama reset_approve_all_after_cycle"""
        approval_handler = MagicMock()
        dispatch_app.tool_executor.approval_handler = approval_handler
        dispatch_app.tool_executor.maybe_execute_from_response = MagicMock(
            return_value=("hello", None)
        )
        ds = AppDispatchServices(dispatch_app)
        ds.resolve_agent_response("agent1", "hello")
        approval_handler.reset_approve_all_after_cycle.assert_called_once()

    def test_followup_handoff_is_truncated_when_too_large(self, dispatch_app):
        tool_result = MagicMock()
        tool_result.error = None
        tool_result.ok = True
        tool_result.to_model_payload = MagicMock(return_value=("x" * 9000))

        dispatch_app.tool_executor.maybe_execute_from_response = MagicMock(
            side_effect=[
                ("tool output", tool_result),
                (None, None),
            ]
        )
        dispatch_app._call_agent = MagicMock(return_value="after-tool")
        plugin = dispatch_app.get_agent_plugin("agent1")
        plugin.tool_use_reliability = "high"
        ds = AppDispatchServices(dispatch_app)
        result = ds.resolve_agent_response("agent1", "initial")
        assert result == "after-tool"
        handoff_arg = dispatch_app._call_agent.call_args.kwargs["handoff"]
        assert handoff_arg.startswith("(histórico truncado)...\n\n")

    def test_call_agent_fallback(self, dispatch_app):
        """app sem _call_agent usa call_agent_low_level"""
        tool_result = MagicMock()
        tool_result.error = None
        tool_result.ok = True
        tool_result.to_model_payload = MagicMock(return_value="{}")

        def _maybe_exec(resp):
            if hasattr(_maybe_exec, "called"):
                return (None, None)
            _maybe_exec.called = True
            return (resp, tool_result)

        dispatch_app.tool_executor.maybe_execute_from_response = MagicMock(side_effect=_maybe_exec)
        plugin = dispatch_app.get_agent_plugin("agent1")
        plugin.tool_use_reliability = "high"
        # Remove _call_agent para forçar o fallback
        if hasattr(dispatch_app, "_call_agent"):
            del dispatch_app._call_agent
        ds = AppDispatchServices(dispatch_app)
        # O hop tool_result gera followup_handoff que chama call_agent_low_level
        # Como call_agent_low_level não está mockado, ele vai tentar chamar de verdade
        # Vamos mocká-lo para evitar erro
        with patch.object(ds, "call_agent_low_level", return_value="response from low level"):
            result = ds.resolve_agent_response("agent1", "some cmd")
            assert result == "response from low level"

    def test_user_cancelled_before_tool_hop_returns_none(self, dispatch_app):
        dispatch_app.agent_client._user_cancelled = True
        dispatch_app.tool_executor.maybe_execute_from_response = MagicMock(
            side_effect=AssertionError("não deveria executar ferramenta após cancelamento")
        )
        ds = AppDispatchServices(dispatch_app)
        result = ds.resolve_agent_response("agent1", "some cmd")
        assert result is None
        dispatch_app.tool_executor.maybe_execute_from_response.assert_not_called()

    def test_user_cancelled_before_followup_tool_call_aborts(self, dispatch_app):
        tool_result = MagicMock()
        tool_result.error = None
        tool_result.ok = True
        tool_result.to_model_payload = MagicMock(return_value="{}")

        def _maybe_exec(_):
            dispatch_app.agent_client._user_cancelled = True
            return ("tool output", tool_result)

        dispatch_app.tool_executor.maybe_execute_from_response = MagicMock(side_effect=_maybe_exec)
        dispatch_app._call_agent = MagicMock(return_value="não deveria chamar")
        plugin = dispatch_app.get_agent_plugin("agent1")
        plugin.tool_use_reliability = "high"

        ds = AppDispatchServices(dispatch_app)
        result = ds.resolve_agent_response("agent1", "some cmd")
        assert result is None
        dispatch_app._call_agent.assert_not_called()


# =============================================================================
# AppDispatchServices — call_agent
# =============================================================================

class TestCallAgent:
    """Testes para AppDispatchServices.call_agent"""

    def test_successful_single_call(self, dispatch_app):
        dispatch_app.MAX_RETRIES = 1
        ds = AppDispatchServices(dispatch_app)
        with patch.object(ds, "call_agent_low_level", return_value="response"), \
             patch.object(ds, "resolve_agent_response", return_value="result"):
            result = ds.call_agent("agent1")
        assert result == "result"

    def test_retry_on_none_low_level(self, dispatch_app):
        """response None → retry até max_retries, depois record_failure"""
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.01
        ds = AppDispatchServices(dispatch_app)
        with patch.object(ds, "call_agent_low_level", return_value=None), \
             patch.object(ds, "resolve_agent_response") as mock_resolve, \
             patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.call_agent("agent1")
        assert result is None
        mock_resolve.assert_not_called()  # nunca chamado pois low_level retornou None
        dispatch_app.record_failure.assert_called_once_with("agent1")

    def test_retry_on_none_low_level_uses_linear_backoff_without_rate_limit(self, dispatch_app):
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.5
        dispatch_app.agent_client.rate_limit_detected = False
        ds = AppDispatchServices(dispatch_app)
        with patch.object(ds, "call_agent_low_level", return_value=None), \
             patch("quimera.app.agent_call_service.time.sleep") as mock_sleep:
            result = ds.call_agent("agent1")
        assert result is None
        mock_sleep.assert_called_with(0.5)

    def test_none_low_level_with_user_cancelled_after_call_aborts(self, dispatch_app):
        dispatch_app.MAX_RETRIES = 2
        ds = AppDispatchServices(dispatch_app)

        def _low_level(*args, **kwargs):
            dispatch_app.agent_client._user_cancelled = True
            return None

        with patch.object(ds, "call_agent_low_level", side_effect=_low_level):
            result = ds.call_agent("agent1")
        assert result is None

    def test_retry_on_none_resolve(self, dispatch_app):
        """resolve_agent_response retorna None → retry"""
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.01
        ds = AppDispatchServices(dispatch_app)
        with patch.object(ds, "call_agent_low_level", return_value="response"), \
             patch.object(ds, "resolve_agent_response", return_value=None) as mock_resolve, \
             patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.call_agent("agent1")
        assert result is None
        assert mock_resolve.call_count >= 1  # chamado pelo menos uma vez

    def test_retry_on_none_resolve_uses_linear_backoff_without_rate_limit(self, dispatch_app):
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.5
        dispatch_app.agent_client.rate_limit_detected = False
        ds = AppDispatchServices(dispatch_app)
        with patch.object(ds, "call_agent_low_level", return_value="response"), \
             patch.object(ds, "resolve_agent_response", return_value=None), \
             patch("quimera.app.agent_call_service.time.sleep") as mock_sleep:
            result = ds.call_agent("agent1")
        assert result is None
        mock_sleep.assert_called_with(0.5)

    def test_none_resolve_with_user_cancelled_after_resolve_aborts(self, dispatch_app):
        dispatch_app.MAX_RETRIES = 2
        ds = AppDispatchServices(dispatch_app)

        def _resolve(*args, **kwargs):
            dispatch_app.agent_client._user_cancelled = True
            return None

        with patch.object(ds, "call_agent_low_level", return_value="response"), \
             patch.object(ds, "resolve_agent_response", side_effect=_resolve):
            result = ds.call_agent("agent1")
        assert result is None

    def test_user_cancelled_aborts_immediately(self, dispatch_app):
        """_user_cancelled True → aborta sem retry"""
        dispatch_app.agent_client._user_cancelled = True
        dispatch_app.MAX_RETRIES = 2
        ds = AppDispatchServices(dispatch_app)
        with patch.object(ds, "call_agent_low_level", return_value=None), \
             patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.call_agent("agent1")
        assert result is None

    def test_rate_limit_backoff(self, dispatch_app):
        """rate_limit_detected → usa RATE_LIMIT_BACKOFF_SECONDS"""
        dispatch_app.agent_client.rate_limit_detected = True
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.5
        ds = AppDispatchServices(dispatch_app)
        with patch.object(ds, "call_agent_low_level", return_value=None), \
             patch("quimera.app.agent_call_service.time.sleep") as mock_sleep:
            result = ds.call_agent("agent1")
        assert result is None
        # rate_limit_detected=True deve usar RATE_LIMIT_BACKOFF_SECONDS (0.01)
        rate_calls = [c for c in mock_sleep.call_args_list if abs(c[0][0] - 0.01) < 0.001]
        assert len(rate_calls) >= 1

    def test_exception_during_call_retries_and_raises(self, dispatch_app):
        """Exception no low_level → retry → exhausted → raise"""
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.01
        ds = AppDispatchServices(dispatch_app)
        with patch.object(ds, "call_agent_low_level", side_effect=ValueError("boom")), \
             patch("quimera.app.agent_call_service.time.sleep"):
            with pytest.raises(ValueError, match="boom"):
                ds.call_agent("agent1")

    def test_exception_with_user_cancelled_returns_none(self, dispatch_app):
        """Exception + _user_cancelled → return None"""
        dispatch_app.MAX_RETRIES = 2
        ds = AppDispatchServices(dispatch_app)

        class _CancelledOnSecondCall:
            call_count = 0
            def __call__(self, *a, **kw):
                _CancelledOnSecondCall.call_count += 1
                if _CancelledOnSecondCall.call_count == 1:
                    raise ValueError("boom")
                return None

        dispatch_app.agent_client._user_cancelled = True
        with patch.object(ds, "call_agent_low_level", _CancelledOnSecondCall()), \
             patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.call_agent("agent1")
        assert result is None

    def test_exception_marks_cancelled_and_returns_none(self, dispatch_app):
        dispatch_app.MAX_RETRIES = 2
        ds = AppDispatchServices(dispatch_app)

        def _boom(*args, **kwargs):
            dispatch_app.agent_client._user_cancelled = True
            raise RuntimeError("cancelled mid-call")

        with patch.object(ds, "call_agent_low_level", side_effect=_boom):
            result = ds.call_agent("agent1")
        assert result is None

    def test_user_cancelled_prevents_retry_loop(self, dispatch_app):
        """_user_cancelled True → low_level não é chamado de novo, sem fallback infinito"""
        dispatch_app.MAX_RETRIES = 3
        ds = AppDispatchServices(dispatch_app)
        low_level = MagicMock(return_value=None)
        ds.call_agent_low_level = low_level
        dispatch_app.agent_client._user_cancelled = True
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.call_agent("agent1")
        assert result is None
        # zero chamadas low_level — abortou antes de qualquer tentativa
        assert low_level.call_count == 0, (
            f"call_agent_low_level chamado {low_level.call_count}x em vez de 0"
        )

    def test_cancelled_during_call_does_not_retry(self, dispatch_app):
        """cancelamento durante call_agent_low_level → aborta sem tentar de novo"""
        dispatch_app.MAX_RETRIES = 3
        ds = AppDispatchServices(dispatch_app)

        def _cancelled_then_boom(*args, **kwargs):
            dispatch_app.agent_client._user_cancelled = True
            raise ValueError("boom")

        low_level = MagicMock(side_effect=_cancelled_then_boom)
        ds.call_agent_low_level = low_level
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.call_agent("agent1")
        assert result is None
        # exatamente 1 chamada (a que lançou), sem retry
        assert low_level.call_count == 1

    def test_with_handoff_options(self, dispatch_app):
        """handoff dict é passado corretamente"""
        dispatch_app.MAX_RETRIES = 1
        ds = AppDispatchServices(dispatch_app)
        with patch.object(ds, "call_agent_low_level", return_value="resp") as mock_ll, \
             patch.object(ds, "resolve_agent_response", return_value="result"):
            result = ds.call_agent("agent1", handoff={"handoff_id": "h123"})
        assert result == "result"
        # Verifica que handoff foi passado
        mock_ll.assert_called_once_with(
            "agent1", silent=False, show_output=True, handoff={"handoff_id": "h123"}
        )

    def test_exception_at_resolve_retries(self, dispatch_app):
        """Exception no resolve_agent_response → retry"""
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.01
        ds = AppDispatchServices(dispatch_app)
        with patch.object(ds, "call_agent_low_level", return_value="response"), \
             patch.object(ds, "resolve_agent_response", side_effect=[RuntimeError("resolve fail"), "ok"]), \
             patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.call_agent("agent1")
        assert result == "ok"


# =============================================================================
# AppDispatchServices — call_agent_low_level
# =============================================================================

class TestCallAgentLowLevel:
    """Testes para AppDispatchServices.call_agent_low_level"""

    @pytest.fixture
    def ll_app(self, dispatch_app):
        app = dispatch_app
        app.get_agent_plugin = MagicMock(return_value=MagicMock())
        app.prompt_builder.build = MagicMock(return_value="prompt_text")
        app.agent_client.call = MagicMock(return_value="agent response")
        app.renderer.start_message_stream = MagicMock()
        app.renderer.update_message_stream = MagicMock()
        app.renderer.finish_message_stream = MagicMock()
        app.renderer.abort_message_stream = MagicMock()
        app.renderer.flush = MagicMock()
        app.agent_client.flush_pending_summary = MagicMock()
        app.session_state = {
            "session_id": "test",
            "handoffs_sent": 0,
            "handoffs_received": 0,
        }
        app.task_services.refresh_task_shared_state = MagicMock()
        return app

    def test_basic_call(self, ll_app):
        ds = AppDispatchServices(ll_app)
        result = ds.call_agent_low_level("agent1")
        assert result == "agent response"

    def test_silent_does_not_start_stream(self, ll_app):
        ds = AppDispatchServices(ll_app)
        ds.call_agent_low_level("agent1", silent=True)
        ll_app.renderer.start_message_stream.assert_not_called()

    def test_on_text_chunk_starts_stream(self, ll_app):
        """chunks são bufferizados e entregues via show_message"""
        def _call(agent, prompt, silent=False, on_text_chunk=None):
            if on_text_chunk:
                on_text_chunk("hello")
                on_text_chunk(" world")
            return "response"
        ll_app.agent_client.call = _call
        ds = AppDispatchServices(ll_app)
        result = ds.call_agent_low_level("agent1")
        assert result == "response"
        ll_app.renderer.show_message.assert_not_called()

    def test_stream_result_none_shows_buffered(self, ll_app):
        """stream com result None — gateway não renderiza, caller faz"""
        def _call(agent, prompt, silent=False, on_text_chunk=None):
            if on_text_chunk:
                on_text_chunk("hello")
            return None
        ll_app.agent_client.call = _call
        ds = AppDispatchServices(ll_app)
        result = ds.call_agent_low_level("agent1")
        assert result is None
        ll_app.renderer.show_message.assert_not_called()

    def test_debug_prompt_metrics(self, ll_app):
        """debug_prompt_metrics=True chama log_prompt_metrics"""
        ll_app.debug_prompt_metrics = True
        ll_app.prompt_builder.build = MagicMock(return_value=("prompt_text", {"tokens": 10}))
        ll_app.agent_client.log_prompt_metrics = MagicMock()
        ds = AppDispatchServices(ll_app)
        ds.call_agent_low_level("agent1")
        ll_app.agent_client.log_prompt_metrics.assert_called_once()

    def test_handoff_passed_to_build(self, ll_app):
        ds = AppDispatchServices(ll_app)
        ds.call_agent_low_level("agent1", handoff={"route_target": "agent2"})
        # build foi chamado com handoff
        ll_app.prompt_builder.build.assert_called_once()
        args, kwargs = ll_app.prompt_builder.build.call_args
        assert args[3] == {"route_target": "agent2"}  # 4º arg posicional

    def test_handoff_only(self, ll_app):
        ds = AppDispatchServices(ll_app)
        ds.call_agent_low_level("agent1", handoff_only=True)
        kwargs = ll_app.prompt_builder.build.call_args.kwargs
        assert kwargs.get("handoff_only") is True

    def test_flush_pending_summary_called(self, ll_app):
        ds = AppDispatchServices(ll_app)
        ds.call_agent_low_level("agent1")
        ll_app.agent_client.flush_pending_summary.assert_called_once()

    def test_counter_lock(self, ll_app):
        """_counter_lock None → não crasha"""
        ll_app._counter_lock = None
        ds = AppDispatchServices(ll_app)
        result = ds.call_agent_low_level("agent1")
        assert result == "agent response"

    def test_redisplay_user_prompt_after_stream(self, ll_app):
        """_redisplay_user_prompt_if_needed chamado após finish"""
        ll_app._redisplay_user_prompt_if_needed = MagicMock()

        def _call(agent, prompt, silent=False, on_text_chunk=None):
            if on_text_chunk:
                on_text_chunk("hello")
            return "response"
        ll_app.agent_client.call = _call
        ds = AppDispatchServices(ll_app)
        ds.call_agent_low_level("agent1")
        ll_app._redisplay_user_prompt_if_needed.assert_called_once_with(clear_first=False)

    def test_primary_false(self, ll_app):
        ds = AppDispatchServices(ll_app)
        ds.call_agent_low_level("agent1", primary=False)
        kwargs = ll_app.prompt_builder.build.call_args.kwargs
        assert kwargs.get("primary") is False

    def test_show_output_false_ignores_text_chunks(self, ll_app):
        def _call(agent, prompt, silent=False, on_text_chunk=None):
            if on_text_chunk:
                on_text_chunk("hello")
            return "response"

        ll_app.agent_client.call = _call
        ds = AppDispatchServices(ll_app)
        ds.call_agent_low_level("agent1", show_output=False)
        ll_app.renderer.start_message_stream.assert_not_called()
        ll_app.renderer.update_message_stream.assert_not_called()

    def test_cancelled_before_low_level_call_returns_none(self, ll_app):
        ll_app.agent_client._user_cancelled = True
        ds = AppDispatchServices(ll_app)
        result = ds.call_agent_low_level("agent1")
        assert result is None
        ll_app.agent_client.call.assert_not_called()

    def test_failed_result_increments_failed_counter(self, ll_app):
        ll_app.agent_client.call = MagicMock(return_value=None)
        ll_app.session_state.update(
            {
                "handoffs_sent": 0,
                "total_latency": 0.0,
                "handoffs_succeeded": 0,
                "handoffs_failed": 0,
            }
        )
        ds = AppDispatchServices(ll_app)
        ds.call_agent_low_level("agent1")
        assert ll_app.session_state["handoffs_failed"] == 1
        assert ll_app.session_state["handoffs_succeeded"] == 0

    def test_success_result_increments_succeeded_counter(self, ll_app):
        ll_app.agent_client.call = MagicMock(return_value="ok")
        ll_app.session_state.update(
            {
                "handoffs_sent": 0,
                "total_latency": 0.0,
                "handoffs_succeeded": 0,
                "handoffs_failed": 0,
            }
        )
        ds = AppDispatchServices(ll_app)
        ds.call_agent_low_level("agent1")
        assert ll_app.session_state["handoffs_succeeded"] == 1
        assert ll_app.session_state["handoffs_failed"] == 0


class TestPrintResponse:
    def test_print_response_with_text_shows_message(self, dispatch_app):
        ds = AppDispatchServices(dispatch_app)
        dispatch_app._redisplay_user_prompt_if_needed = MagicMock()
        dispatch_app._clear_user_prompt_line_if_needed = MagicMock()
        ds.print_response("agent1", "hello")
        dispatch_app.renderer.show_message.assert_called_once_with("agent1", "hello")
        dispatch_app.renderer.show_no_response.assert_not_called()
        dispatch_app._redisplay_user_prompt_if_needed.assert_called_once_with(clear_first=False)

    def test_print_response_without_text_shows_no_response(self, dispatch_app):
        ds = AppDispatchServices(dispatch_app)
        dispatch_app._redisplay_user_prompt_if_needed = MagicMock()
        dispatch_app._clear_user_prompt_line_if_needed = MagicMock()
        ds.print_response("agent1", None)
        dispatch_app.renderer.show_no_response.assert_called_once_with("agent1")
        dispatch_app.renderer.show_message.assert_not_called()
        dispatch_app._redisplay_user_prompt_if_needed.assert_called_once_with(clear_first=False)


# =============================================================================
# AppDispatchServices — _update_spy_telemetry
# =============================================================================

class TestUpdateSpyTelemetry:
    """_update_spy_telemetry"""

    def test_no_agent_client_does_nothing(self, dispatch_app):
        dispatch_app.agent_client = None
        ds = AppDispatchServices(dispatch_app)
        # Não deve crashar
        ds._update_spy_telemetry("agent1")

    def test_no_last_spy_turn_detail_does_nothing(self, dispatch_app):
        dispatch_app.agent_client.last_spy_turn_detail = None
        ds = AppDispatchServices(dispatch_app)
        ds._update_spy_telemetry("agent1")
        assert "spy_last_turn_detail" not in dispatch_app.shared_state

    def test_sets_spy_in_shared_state(self, dispatch_app):
        dispatch_app.agent_client.last_spy_turn_detail = {
            "turn_id": "t1",
            "tools": [{"tool_call_id": "c1"}],
        }
        ds = AppDispatchServices(dispatch_app)
        ds._update_spy_telemetry("agent1")
        assert "spy_last_turn_detail" in dispatch_app.shared_state
        assert dispatch_app.shared_state["spy_last_turn_detail"]["agent"] == "agent1"

    def test_sets_spy_in_session_state(self, dispatch_app):
        dispatch_app.agent_client.last_spy_turn_detail = {
            "turn_id": "t1",
            "tools": [],
        }
        ds = AppDispatchServices(dispatch_app)
        ds._update_spy_telemetry("agent1")
        assert "last_spy_turn_detail" in dispatch_app.session_state

    def test_shared_state_not_dict_skips(self, dispatch_app):
        dispatch_app.shared_state = None
        dispatch_app.agent_client.last_spy_turn_detail = {
            "turn_id": "t1",
            "tools": [],
        }
        ds = AppDispatchServices(dispatch_app)
        ds._update_spy_telemetry("agent1")
        # Não deve crashar

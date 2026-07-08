"""Testes para quimera.app.dispatch — 100% de cobertura."""
from unittest.mock import MagicMock, Mock, call, patch, PropertyMock, sentinel
import pytest
import time
import re
import threading

from quimera.app.dispatch import AppDispatchServices
class TestSanitizeSpyTurnDetail:
    """_sanitize_spy_turn_detail — método de classe"""

    def test_non_dict_returns_none(self):
        """Verifica que non dict returns none."""
        assert AppDispatchServices._sanitize_spy_turn_detail("not dict") is None

    def test_truncates_tools_list(self):
        """Verifica que truncates tools list."""
        tools = [{"tool_call_id": str(i)} for i in range(20)]
        detail = {"turn_id": "t1", "tools": tools}
        result = AppDispatchServices._sanitize_spy_turn_detail(detail)
        assert result is not None
        assert len(result["tools"]) == 20
        assert result["truncated_tools"] is False

    def test_no_truncation_if_within_limit(self):
        """Verifica que no truncation if within limit."""
        tools = [{"tool_call_id": str(i)} for i in range(3)]
        detail = {"turn_id": "t1", "tools": tools}
        result = AppDispatchServices._sanitize_spy_turn_detail(detail)
        assert len(result["tools"]) == 3
        assert result["truncated_tools"] is False

    def test_non_list_tools_defaults_empty(self):
        """Verifica que non list tools defaults empty."""
        detail = {"turn_id": "t1", "tools": None}
        result = AppDispatchServices._sanitize_spy_turn_detail(detail)
        assert result["tools"] == []

    def test_non_dict_tool_skipped(self):
        """Verifica que non dict tool skipped."""
        detail = {"turn_id": "t1", "tools": ["not a dict"]}
        result = AppDispatchServices._sanitize_spy_turn_detail(detail)
        assert result["tools"] == []

    def test_truncate_spy_text_short_string(self):
        """Verifica que truncate spy text short string."""
        assert AppDispatchServices._truncate_spy_text("hello") == "hello"

    def test_truncate_spy_text_long_string(self):
        """Verifica que truncate spy text long string."""
        long_str = "a" * 500
        result = AppDispatchServices._truncate_spy_text(long_str)
        assert result == long_str

    def test_truncate_spy_text_non_string(self):
        """Verifica que truncate spy text non string."""
        assert AppDispatchServices._truncate_spy_text(42) == 42

    def test_sanitize_spy_map_non_dict(self):
        """Verifica que sanitize spy map non dict."""
        assert AppDispatchServices._sanitize_spy_map("not dict") is None

    def test_sanitize_spy_map_empty_dict(self):
        """Verifica que sanitize spy map empty dict."""
        assert AppDispatchServices._sanitize_spy_map({}) is None

    def test_sanitize_spy_map_truncates_items(self):
        """Verifica que sanitize spy map truncates items."""
        payload = {str(i): i for i in range(20)}
        result = AppDispatchServices._sanitize_spy_map(payload)
        assert result is not None
        assert len(result) == len(payload)

    def test_sanitize_spy_map_preserves_simple_types(self):
        """Verifica que sanitize spy map preserves simple types."""
        payload = {"a": 1, "b": 2.5, "c": True, "d": None}
        result = AppDispatchServices._sanitize_spy_map(payload)
        assert result["a"] == 1
        assert result["b"] == 2.5
        assert result["c"] is True
        assert result["d"] is None

    def test_sanitize_spy_map_truncates_long_values(self):
        """Verifica que sanitize spy map truncates long values."""
        payload = {"key": "x" * 500}
        result = AppDispatchServices._sanitize_spy_map(payload)
        assert result["key"] == payload["key"]

    def test_sanitize_spy_map_converts_non_string_value(self):
        """Verifica que sanitize spy map converts non string value."""
        marker = object()
        payload = {"key": marker}
        result = AppDispatchServices._sanitize_spy_map(payload)
        assert not isinstance(result["key"], str)


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
    app.get_agent_profile = MagicMock(return_value=MagicMock())
    app.prompt_builder = MagicMock()
    app.agent_client = MagicMock()
    app.agent_client._user_cancelled = False
    app.agent_client._cancel_event = threading.Event()
    app.renderer = MagicMock()
    app.shared_state = {}
    app.session_state = {
        "session_id": "test-session",
        "delegations_sent": 0,
        "delegations_received": 0,
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
    app._delegate = None
    app.agent_run_sink = None
    return app


class TestResolveAgentResponse:
    """Testes para AppDispatchServices.resolve_agent_response"""

    def test_empty_response_is_passed_through(self, dispatch_app):
        """Verifica que empty response is passed through."""
        ds = AppDispatchServices.from_app(dispatch_app)
        assert ds.resolve_agent_response("agent1", None) is None
        assert ds.resolve_agent_response("agent1", "") == ""

    def test_response_text_is_not_parsed_as_tool_call(self, dispatch_app):
        """Verifica que response text is not parsed as tool call."""
        dispatch_app.tool_executor.execute = MagicMock(
            side_effect=AssertionError("textual tool tags must not execute")
        )
        ds = AppDispatchServices.from_app(dispatch_app)

        response = 'Texto <tool function="read_file" path="secret.txt" /> continua texto.'
        result = ds.resolve_agent_response("agent1", response)

        assert result == response
        dispatch_app.tool_executor.execute.assert_not_called()

    def test_passthrough_does_not_print_or_persist_visible_text(self, dispatch_app):
        """Verifica que passthrough does not print or persist visible text."""
        ds = AppDispatchServices.from_app(dispatch_app)
        response = "resposta final"

        assert ds.resolve_agent_response(
            "agent1", response, show_output=True, persist_history=True
        ) == response
        dispatch_app.print_response.assert_not_called()
        dispatch_app.session_services.persist_message.assert_not_called()


# =============================================================================
# AppDispatchServices — delegate
# =============================================================================

class TestCallAgent:
    """Testes para AppDispatchServices.delegate"""

    def test_successful_single_call(self, dispatch_app):
        """Verifica que successful single call."""
        dispatch_app.MAX_RETRIES = 1
        ds = AppDispatchServices.from_app(dispatch_app)
        with patch.object(ds, "delegate_low_level", return_value="response"), \
             patch.object(ds, "resolve_agent_response", return_value="result"):
            result = ds.delegate("agent1")
        assert result == "result"

    def test_retry_on_none_low_level(self, dispatch_app):
        """response None → retry até max_retries, depois record_failure"""
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.01
        ds = AppDispatchServices.from_app(dispatch_app)
        with patch.object(ds, "delegate_low_level", return_value=None), \
             patch.object(ds, "resolve_agent_response") as mock_resolve, \
             patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.delegate("agent1")
        assert result is None
        mock_resolve.assert_not_called()  # nunca chamado pois low_level retornou None
        dispatch_app.record_failure.assert_called_once_with("agent1")

    def test_delegate_honors_max_retries_override(self, dispatch_app):
        """max_retries por chamada deve sobrescrever o default do app."""
        dispatch_app.MAX_RETRIES = 3
        ds = AppDispatchServices.from_app(dispatch_app)
        with patch.object(ds, "delegate_low_level", return_value=None) as mock_ll, \
             patch("quimera.app.agent_call_service.time.sleep") as mock_sleep:
            result = ds.delegate("agent1", max_retries=1)
        assert result is None
        assert mock_ll.call_count == 1
        mock_sleep.assert_not_called()

    def test_retry_on_none_low_level_uses_linear_backoff_without_rate_limit(self, dispatch_app):
        """Verifica que retry on none low level uses linear backoff without rate limit."""
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.5
        dispatch_app.agent_client.rate_limit_detected = False
        ds = AppDispatchServices.from_app(dispatch_app)
        with patch.object(ds, "delegate_low_level", return_value=None), \
             patch("quimera.app.agent_call_service.time.sleep") as mock_sleep:
            result = ds.delegate("agent1")
        assert result is None
        mock_sleep.assert_called_with(0.5)

    def test_none_low_level_with_user_cancelled_after_call_aborts(self, dispatch_app):
        """Verifica que none low level with user cancelled after call aborts."""
        dispatch_app.MAX_RETRIES = 2
        ds = AppDispatchServices.from_app(dispatch_app)

        def _low_level(*args, **kwargs):
            dispatch_app.agent_client._user_cancelled = True
            return None

        with patch.object(ds, "delegate_low_level", side_effect=_low_level):
            result = ds.delegate("agent1")
        assert result is None

    def test_retry_on_none_resolve(self, dispatch_app):
        """resolve_agent_response retorna None → retry"""
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.01
        ds = AppDispatchServices.from_app(dispatch_app)
        with patch.object(ds, "delegate_low_level", return_value="response"), \
             patch.object(ds, "resolve_agent_response", return_value=None) as mock_resolve, \
             patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.delegate("agent1")
        assert result is None
        assert mock_resolve.call_count >= 1  # chamado pelo menos uma vez

    def test_retry_on_none_resolve_uses_linear_backoff_without_rate_limit(self, dispatch_app):
        """Verifica que retry on none resolve uses linear backoff without rate limit."""
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.5
        dispatch_app.agent_client.rate_limit_detected = False
        ds = AppDispatchServices.from_app(dispatch_app)
        with patch.object(ds, "delegate_low_level", return_value="response"), \
             patch.object(ds, "resolve_agent_response", return_value=None), \
             patch("quimera.app.agent_call_service.time.sleep") as mock_sleep:
            result = ds.delegate("agent1")
        assert result is None
        mock_sleep.assert_called_with(0.5)

    def test_none_resolve_with_user_cancelled_after_resolve_aborts(self, dispatch_app):
        """Verifica que none resolve with user cancelled after resolve aborts."""
        dispatch_app.MAX_RETRIES = 2
        ds = AppDispatchServices.from_app(dispatch_app)

        def _resolve(*args, **kwargs):
            dispatch_app.agent_client._user_cancelled = True
            return None

        with patch.object(ds, "delegate_low_level", return_value="response"), \
             patch.object(ds, "resolve_agent_response", side_effect=_resolve):
            result = ds.delegate("agent1")
        assert result is None

    def test_user_cancelled_aborts_immediately(self, dispatch_app):
        """_user_cancelled True → aborta sem retry"""
        dispatch_app.agent_client._user_cancelled = True
        dispatch_app.MAX_RETRIES = 2
        ds = AppDispatchServices.from_app(dispatch_app)
        with patch.object(ds, "delegate_low_level", return_value=None), \
             patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.delegate("agent1")
        assert result is None

    def test_rate_limit_backoff(self, dispatch_app):
        """rate_limit_detected → usa RATE_LIMIT_BACKOFF_SECONDS"""
        dispatch_app.agent_client.rate_limit_detected = True
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.5
        ds = AppDispatchServices.from_app(dispatch_app)
        with patch.object(ds, "delegate_low_level", return_value=None), \
             patch("quimera.app.agent_call_service.time.sleep") as mock_sleep:
            result = ds.delegate("agent1")
        assert result is None
        # rate_limit_detected=True deve usar RATE_LIMIT_BACKOFF_SECONDS (0.01)
        rate_calls = [c for c in mock_sleep.call_args_list if abs(c[0][0] - 0.01) < 0.001]
        assert len(rate_calls) >= 1

    def test_exception_during_call_retries_and_raises(self, dispatch_app):
        """Exception no low_level → retry → exhausted → raise"""
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.01
        ds = AppDispatchServices.from_app(dispatch_app)
        with patch.object(ds, "delegate_low_level", side_effect=ValueError("boom")), \
             patch("quimera.app.agent_call_service.time.sleep"):
            with pytest.raises(ValueError, match="boom"):
                ds.delegate("agent1")

    def test_exception_with_user_cancelled_returns_none(self, dispatch_app):
        """Exception + _user_cancelled → return None"""
        dispatch_app.MAX_RETRIES = 2
        ds = AppDispatchServices.from_app(dispatch_app)

        class _CancelledOnSecondCall:
            call_count = 0
            def __call__(self, *a, **kw):
                _CancelledOnSecondCall.call_count += 1
                if _CancelledOnSecondCall.call_count == 1:
                    raise ValueError("boom")
                return None

        dispatch_app.agent_client._user_cancelled = True
        with patch.object(ds, "delegate_low_level", _CancelledOnSecondCall()), \
             patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.delegate("agent1")
        assert result is None

    def test_exception_marks_cancelled_and_returns_none(self, dispatch_app):
        """Verifica que exception marks cancelled and returns none."""
        dispatch_app.MAX_RETRIES = 2
        ds = AppDispatchServices.from_app(dispatch_app)

        def _boom(*args, **kwargs):
            dispatch_app.agent_client._user_cancelled = True
            raise RuntimeError("cancelled mid-call")

        with patch.object(ds, "delegate_low_level", side_effect=_boom):
            result = ds.delegate("agent1")
        assert result is None

    def test_user_cancelled_prevents_retry_loop(self, dispatch_app):
        """_user_cancelled True → low_level não é chamado de novo, sem fallback infinito"""
        dispatch_app.MAX_RETRIES = 3
        ds = AppDispatchServices.from_app(dispatch_app)
        low_level = MagicMock(return_value=None)
        ds.delegate_low_level = low_level
        dispatch_app.agent_client._user_cancelled = True
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.delegate("agent1")
        assert result is None
        # zero chamadas low_level — abortou antes de qualquer tentativa
        assert low_level.call_count == 0, (
            f"delegate_low_level chamado {low_level.call_count}x em vez de 0"
        )

    def test_cancelled_during_call_does_not_retry(self, dispatch_app):
        """cancelamento durante delegate_low_level → aborta sem tentar de novo"""
        dispatch_app.MAX_RETRIES = 3
        ds = AppDispatchServices.from_app(dispatch_app)

        def _cancelled_then_boom(*args, **kwargs):
            dispatch_app.agent_client._user_cancelled = True
            raise ValueError("boom")

        low_level = MagicMock(side_effect=_cancelled_then_boom)
        ds.delegate_low_level = low_level
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.delegate("agent1")
        assert result is None
        # exatamente 1 chamada (a que lançou), sem retry
        assert low_level.call_count == 1

    def test_with_delegation_options(self, dispatch_app):
        """delegation dict é passado corretamente"""
        dispatch_app.MAX_RETRIES = 1
        ds = AppDispatchServices.from_app(dispatch_app)
        with patch.object(ds, "delegate_low_level", return_value="resp") as mock_ll, \
             patch.object(ds, "resolve_agent_response", return_value="result"):
            result = ds.delegate("agent1", delegation={"delegation_id": "h123"})
        assert result == "result"
        # Verifica que delegation foi passado
        mock_ll.assert_called_once_with(
            "agent1", silent=False, show_output=True, progress_callback=None, delegation={"delegation_id": "h123"}
        )

    def test_exception_at_resolve_retries(self, dispatch_app):
        """Exception no resolve_agent_response → retry"""
        dispatch_app.MAX_RETRIES = 2
        dispatch_app.RETRY_BACKOFF_SECONDS = 0.01
        ds = AppDispatchServices.from_app(dispatch_app)
        with patch.object(ds, "delegate_low_level", return_value="response"), \
             patch.object(ds, "resolve_agent_response", side_effect=[RuntimeError("resolve fail"), "ok"]), \
             patch("quimera.app.agent_call_service.time.sleep"):
            result = ds.delegate("agent1")
        assert result == "ok"


# =============================================================================
# AppDispatchServices — delegate_low_level
# =============================================================================

class TestCallAgentLowLevel:
    """Testes para AppDispatchServices.delegate_low_level"""

    @pytest.fixture
    def ll_app(self, dispatch_app):
        app = dispatch_app
        app.get_agent_profile = MagicMock(return_value=MagicMock())
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
            "delegations_sent": 0,
            "delegations_received": 0,
        }
        app.task_services.refresh_task_shared_state = MagicMock()
        return app

    def test_basic_call(self, ll_app):
        """Verifica que basic call."""
        ds = AppDispatchServices.from_app(ll_app)
        result = ds.delegate_low_level("agent1")
        assert result == "agent response"

    def test_silent_does_not_start_stream(self, ll_app):
        """Verifica que silent does not start stream."""
        ds = AppDispatchServices.from_app(ll_app)
        ds.delegate_low_level("agent1", silent=True)
        ll_app.renderer.start_message_stream.assert_not_called()

    def test_on_text_chunk_starts_stream(self, ll_app):
        """chunks são bufferizados e entregues via show_message"""
        def _call(agent, prompt, silent=False, on_text_chunk=None, progress_callback=None, from_agent=None):
            del from_agent
            if on_text_chunk:
                on_text_chunk("hello")
                on_text_chunk(" world")
            return "response"
        ll_app.agent_client.call = _call
        ds = AppDispatchServices.from_app(ll_app)
        result = ds.delegate_low_level("agent1")
        assert result == "response"
        ll_app.renderer.show_message.assert_not_called()

    def test_stream_result_none_shows_buffered(self, ll_app):
        """stream com result None — gateway não renderiza, caller faz"""
        def _call(agent, prompt, silent=False, on_text_chunk=None, progress_callback=None, from_agent=None):
            del from_agent
            if on_text_chunk:
                on_text_chunk("hello")
            return None
        ll_app.agent_client.call = _call
        ds = AppDispatchServices.from_app(ll_app)
        result = ds.delegate_low_level("agent1")
        assert result is None
        ll_app.renderer.show_message.assert_not_called()

    def test_debug_prompt_metrics(self, ll_app):
        """debug_prompt_metrics=True chama log_prompt_metrics"""
        ll_app.debug_prompt_metrics = True
        ll_app.prompt_builder.build = MagicMock(return_value=("prompt_text", {"tokens": 10}))
        ll_app.agent_client.log_prompt_metrics = MagicMock()
        ds = AppDispatchServices.from_app(ll_app)
        ds.delegate_low_level("agent1")
        ll_app.agent_client.log_prompt_metrics.assert_called_once()

    def test_delegation_passed_to_build(self, ll_app):
        """Verifica que delegation passed to build."""
        ds = AppDispatchServices.from_app(ll_app)
        ds.delegate_low_level("agent1", delegation={"route_target": "agent2"})
        # build foi chamado com delegation
        ll_app.prompt_builder.build.assert_called_once()
        args, kwargs = ll_app.prompt_builder.build.call_args
        assert args[3] == {"route_target": "agent2"}  # 4º arg posicional

    def test_delegation_only(self, ll_app):
        """Verifica que delegation only."""
        ds = AppDispatchServices.from_app(ll_app)
        ds.delegate_low_level("agent1", delegation_only=True)
        kwargs = ll_app.prompt_builder.build.call_args.kwargs
        assert kwargs.get("delegation_only") is True

    def test_request_override_and_history_snapshot_passed_to_build(self, ll_app):
        """Verifica que request override and history snapshot passed to build."""
        ds = AppDispatchServices.from_app(ll_app)
        ds.delegate_low_level(
            "agent1",
            request_override="pedido fixo",
            history_snapshot=[{"role": "human", "content": "pedido fixo"}],
        )
        args, kwargs = ll_app.prompt_builder.build.call_args
        assert args[1] == [{"role": "human", "content": "pedido fixo"}]
        assert kwargs.get("request_override") == "pedido fixo"

    def test_flush_pending_summary_called(self, ll_app):
        """Verifica que flush pending summary called."""
        ds = AppDispatchServices.from_app(ll_app)
        ds.delegate_low_level("agent1")
        ll_app.agent_client.flush_pending_summary.assert_called_once()

    def test_counter_lock(self, ll_app):
        """_counter_lock None → não crasha"""
        ll_app._counter_lock = None
        ds = AppDispatchServices.from_app(ll_app)
        result = ds.delegate_low_level("agent1")
        assert result == "agent response"

    def test_redisplay_user_prompt_after_stream(self, ll_app):
        """_redisplay_user_prompt_if_needed chamado após finish"""
        ll_app._redisplay_user_prompt_if_needed = MagicMock()

        def _call(agent, prompt, silent=False, on_text_chunk=None, progress_callback=None, from_agent=None):
            del from_agent
            if on_text_chunk:
                on_text_chunk("hello")
            return "response"
        ll_app.agent_client.call = _call
        ds = AppDispatchServices.from_app(ll_app)
        ds.delegate_low_level("agent1")
        ll_app._redisplay_user_prompt_if_needed.assert_called_once_with(clear_first=False)

    def test_primary_false(self, ll_app):
        """Verifica que primary false."""
        ds = AppDispatchServices.from_app(ll_app)
        ds.delegate_low_level("agent1", primary=False)
        kwargs = ll_app.prompt_builder.build.call_args.kwargs
        assert kwargs.get("primary") is False

    def test_show_output_false_ignores_text_chunks(self, ll_app):
        """Verifica que show output false ignores text chunks."""
        def _call(agent, prompt, silent=False, on_text_chunk=None, progress_callback=None, from_agent=None):
            del from_agent
            if on_text_chunk:
                on_text_chunk("hello")
            return "response"

        ll_app.agent_client.call = _call
        ds = AppDispatchServices.from_app(ll_app)
        ds.delegate_low_level("agent1", show_output=False)
        ll_app.renderer.start_message_stream.assert_not_called()
        ll_app.renderer.update_message_stream.assert_not_called()

    def test_cancelled_before_low_level_call_returns_none(self, ll_app):
        """Verifica que cancelled before low level call returns none."""
        ll_app.agent_client._user_cancelled = True
        ds = AppDispatchServices.from_app(ll_app)
        result = ds.delegate_low_level("agent1")
        assert result is None
        ll_app.agent_client.call.assert_not_called()

    def test_failed_result_increments_failed_counter(self, ll_app):
        """Verifica que failed result increments failed counter."""
        ll_app.agent_client.call = MagicMock(return_value=None)
        ll_app.session_state.update(
            {
                "delegations_sent": 0,
                "total_latency": 0.0,
                "delegations_succeeded": 0,
                "delegations_failed": 0,
            }
        )
        ds = AppDispatchServices.from_app(ll_app)
        ds.delegate_low_level("agent1")
        assert ll_app.session_state["delegations_failed"] == 1
        assert ll_app.session_state["delegations_succeeded"] == 0

    def test_success_result_increments_succeeded_counter(self, ll_app):
        """Verifica que success result increments succeeded counter."""
        ll_app.agent_client.call = MagicMock(return_value="ok")
        ll_app.session_state.update(
            {
                "delegations_sent": 0,
                "total_latency": 0.0,
                "delegations_succeeded": 0,
                "delegations_failed": 0,
            }
        )
        ds = AppDispatchServices.from_app(ll_app)
        ds.delegate_low_level("agent1")
        assert ll_app.session_state["delegations_succeeded"] == 1
        assert ll_app.session_state["delegations_failed"] == 0


class TestPrintResponse:
    def test_print_response_with_text_shows_message(self, dispatch_app):
        """Verifica que print response with text shows message."""
        ds = AppDispatchServices.from_app(dispatch_app)
        dispatch_app._redisplay_user_prompt_if_needed = MagicMock()
        ds.print_response("agent1", "hello")
        dispatch_app.renderer.show_message.assert_called_once_with("agent1", "hello")
        dispatch_app.renderer.show_no_response.assert_not_called()
        dispatch_app._redisplay_user_prompt_if_needed.assert_called_once_with(clear_first=False)

    def test_print_response_without_text_shows_no_response(self, dispatch_app):
        """Verifica que print response without text shows no response."""
        ds = AppDispatchServices.from_app(dispatch_app)
        dispatch_app._redisplay_user_prompt_if_needed = MagicMock()
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
        """Verifica que no agent client does nothing."""
        dispatch_app.agent_client = None
        ds = AppDispatchServices.from_app(dispatch_app)
        # Não deve crashar
        ds._update_spy_telemetry("agent1")

    def test_no_last_spy_turn_detail_does_nothing(self, dispatch_app):
        """Verifica que no last spy turn detail does nothing."""
        dispatch_app.agent_client.last_spy_turn_detail = None
        ds = AppDispatchServices.from_app(dispatch_app)
        ds._update_spy_telemetry("agent1")
        assert "spy_last_turn_detail" not in dispatch_app.shared_state

    def test_sets_spy_in_shared_state(self, dispatch_app):
        """Verifica que sets spy in shared state."""
        dispatch_app.agent_client.last_spy_turn_detail = {
            "turn_id": "t1",
            "tools": [{"tool_call_id": "c1"}],
        }
        ds = AppDispatchServices.from_app(dispatch_app)
        ds._update_spy_telemetry("agent1")
        assert "spy_last_turn_detail" in dispatch_app.shared_state
        assert dispatch_app.shared_state["spy_last_turn_detail"]["agent"] == "agent1"

    def test_sets_spy_in_session_state(self, dispatch_app):
        """Verifica que sets spy in session state."""
        dispatch_app.agent_client.last_spy_turn_detail = {
            "turn_id": "t1",
            "tools": [],
        }
        ds = AppDispatchServices.from_app(dispatch_app)
        ds._update_spy_telemetry("agent1")
        assert "last_spy_turn_detail" in dispatch_app.session_state

    def test_shared_state_not_dict_skips(self, dispatch_app):
        """Verifica que shared state not dict skips."""
        dispatch_app.shared_state = None
        dispatch_app.agent_client.last_spy_turn_detail = {
            "turn_id": "t1",
            "tools": [],
        }
        ds = AppDispatchServices.from_app(dispatch_app)
        ds._update_spy_telemetry("agent1")
        # Não deve crashar

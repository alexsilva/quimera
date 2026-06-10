"""Testes unitários para AgentCallService."""
from unittest.mock import MagicMock, patch
import pytest

from quimera.app.agent_call_service import AgentCallService


class TestAgentCallServiceConstruction:
    def test_default_values(self):
        service = AgentCallService()
        assert service._max_retries == 2
        assert service._retry_backoff == 1.0
        assert service._rate_limit_backoff == 30.0

    def test_custom_values(self):
        record = MagicMock()
        limited = MagicMock(return_value=False)
        before_retry = MagicMock()
        service = AgentCallService(
            max_retries=3, retry_backoff=0.5,
            rate_limit_backoff=10.0,
            record_failure=record, is_rate_limited=limited,
            before_retry=before_retry,
        )
        assert service._max_retries == 3
        assert service._retry_backoff == 0.5
        assert service._rate_limit_backoff == 10.0
        assert service._record_failure is record
        assert service._is_rate_limited is limited
        assert service._before_retry is before_retry

    def test_default_record_failure_is_noop(self):
        service = AgentCallService()
        service._record_failure("agent1")

    def test_default_is_rate_limited_returns_false(self):
        service = AgentCallService()
        assert service._is_rate_limited() is False


class TestComputeBackoff:
    def test_rate_limited_uses_rate_limit_backoff(self):
        service = AgentCallService(
            retry_backoff=1.0, rate_limit_backoff=30.0,
            is_rate_limited=lambda: True,
        )
        assert service._compute_backoff(1) == 30.0

    def test_not_rate_limited_uses_linear_backoff(self):
        service = AgentCallService(
            retry_backoff=2.0, rate_limit_backoff=30.0,
            is_rate_limited=lambda: False,
        )
        assert service._compute_backoff(1) == 2.0
        assert service._compute_backoff(2) == 4.0
        assert service._compute_backoff(3) == 6.0


class TestCall:
    def test_success_first_attempt(self):
        service = AgentCallService(max_retries=2)
        call_fn = MagicMock(return_value="response")
        resolve_fn = MagicMock(return_value="result")
        result = service.call("agent1", call_fn, resolve_fn, lambda: False)
        assert result == "result"
        call_fn.assert_called_once_with("agent1")
        resolve_fn.assert_called_once_with("agent1", "response")

    def test_retry_on_none_call_exhausted(self):
        record = MagicMock()
        service = AgentCallService(max_retries=2, retry_backoff=0.01, record_failure=record)
        call_fn = MagicMock(return_value=None)
        resolve_fn = MagicMock()
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = service.call("agent1", call_fn, resolve_fn, lambda: False)
        assert result is None
        assert call_fn.call_count == 2
        resolve_fn.assert_not_called()
        record.assert_called_once_with("agent1")

    def test_retry_on_none_call_then_succeeds(self):
        service = AgentCallService(max_retries=3, retry_backoff=0.01)
        call_responses = [None, None, "response"]
        call_fn = MagicMock(side_effect=call_responses)
        resolve_fn = MagicMock(return_value="result")
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = service.call("agent1", call_fn, resolve_fn, lambda: False)
        assert result == "result"
        assert call_fn.call_count == 3
        resolve_fn.assert_called_once_with("agent1", "response")

    def test_before_retry_called_on_none_call(self):
        before_retry = MagicMock()
        service = AgentCallService(max_retries=2, retry_backoff=0.01, before_retry=before_retry)
        call_fn = MagicMock(return_value=None)
        with patch("quimera.app.agent_call_service.time.sleep"):
            service.call("agent1", call_fn, MagicMock(), lambda: False)
        before_retry.assert_called_once_with("agent1", 1, "no_response")

    def test_retry_on_none_resolve_exhausted(self):
        record = MagicMock()
        service = AgentCallService(max_retries=2, retry_backoff=0.01, record_failure=record)
        call_fn = MagicMock(return_value="response")
        resolve_fn = MagicMock(return_value=None)
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = service.call("agent1", call_fn, resolve_fn, lambda: False)
        assert result is None
        assert call_fn.call_count == 2
        assert resolve_fn.call_count == 2
        record.assert_called_once_with("agent1")

    def test_retry_on_none_resolve_then_succeeds(self):
        service = AgentCallService(max_retries=3, retry_backoff=0.01)
        call_fn = MagicMock(return_value="response")
        resolve_responses = [None, None, "result"]
        resolve_fn = MagicMock(side_effect=resolve_responses)
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = service.call("agent1", call_fn, resolve_fn, lambda: False)
        assert result == "result"
        assert call_fn.call_count == 3
        assert resolve_fn.call_count == 3

    def test_before_retry_called_on_none_resolve(self):
        before_retry = MagicMock()
        service = AgentCallService(max_retries=2, retry_backoff=0.01, before_retry=before_retry)
        call_fn = MagicMock(return_value="response")
        resolve_fn = MagicMock(return_value=None)
        with patch("quimera.app.agent_call_service.time.sleep"):
            service.call("agent1", call_fn, resolve_fn, lambda: False)
        before_retry.assert_called_once_with("agent1", 1, "resolve_failed")

    def test_exception_during_call_retries_and_raises(self):
        record = MagicMock()
        service = AgentCallService(max_retries=2, retry_backoff=0.01, record_failure=record)
        call_fn = MagicMock(side_effect=ValueError("boom"))
        with patch("quimera.app.agent_call_service.time.sleep"):
            with pytest.raises(ValueError, match="boom"):
                service.call("agent1", call_fn, MagicMock(), lambda: False)
        assert call_fn.call_count == 2
        record.assert_called_once_with("agent1")

    def test_exception_during_call_then_succeeds(self):
        service = AgentCallService(max_retries=3, retry_backoff=0.01)
        call_responses = [ValueError("boom"), ValueError("boom"), "response"]
        def _call(a):
            exc = call_responses.pop(0)
            if isinstance(exc, Exception):
                raise exc
            return exc
        resolve_fn = MagicMock(return_value="result")
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = service.call("agent1", _call, resolve_fn, lambda: False)
        assert result == "result"

    def test_before_retry_called_on_exception(self):
        before_retry = MagicMock()
        service = AgentCallService(max_retries=2, retry_backoff=0.01, before_retry=before_retry)
        call_fn = MagicMock(side_effect=ValueError("boom"))
        with patch("quimera.app.agent_call_service.time.sleep"):
            with pytest.raises(ValueError):
                service.call("agent1", call_fn, MagicMock(), lambda: False)
        before_retry.assert_called_once_with("agent1", 1, "exception")

    def test_exception_during_resolve_retries_and_raises(self):
        record = MagicMock()
        service = AgentCallService(max_retries=2, retry_backoff=0.01, record_failure=record)
        call_fn = MagicMock(return_value="response")
        resolve_fn = MagicMock(side_effect=RuntimeError("resolve fail"))
        with patch("quimera.app.agent_call_service.time.sleep"):
            with pytest.raises(RuntimeError, match="resolve fail"):
                service.call("agent1", call_fn, resolve_fn, lambda: False)
        assert call_fn.call_count == 2
        assert resolve_fn.call_count == 2
        record.assert_called_once_with("agent1")

    def test_exception_during_resolve_then_succeeds(self):
        service = AgentCallService(max_retries=3, retry_backoff=0.01)
        call_fn = MagicMock(return_value="response")
        resolve_responses = [RuntimeError("fail"), RuntimeError("fail"), "result"]
        def _resolve(a, resp):
            exc = resolve_responses.pop(0)
            if isinstance(exc, Exception):
                raise exc
            return exc
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = service.call("agent1", call_fn, _resolve, lambda: False)
        assert result == "result"

    def test_user_cancelled_aborts_before_attempt(self):
        service = AgentCallService(max_retries=3)
        call_fn = MagicMock()
        result = service.call("agent1", call_fn, MagicMock(), lambda: True)
        assert result is None
        call_fn.assert_not_called()

    def test_user_cancelled_mid_retry_aborts(self):
        service = AgentCallService(max_retries=3, retry_backoff=0.01)
        cancel_count = 0
        def _cancel():
            nonlocal cancel_count
            cancel_count += 1
            return cancel_count >= 2
        call_fn = MagicMock(return_value=None)
        resolve_fn = MagicMock()
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = service.call("agent1", call_fn, resolve_fn, _cancel)
        assert result is None
        assert call_fn.call_count == 1

    def test_user_cancelled_after_none_call_aborts(self):
        service = AgentCallService(max_retries=3, retry_backoff=0.01)
        call_fn = MagicMock(return_value=None)
        def _cancel():
            return True
        result = service.call("agent1", call_fn, MagicMock(), _cancel)
        assert result is None

    def test_user_cancelled_before_resolve_aborts(self):
        service = AgentCallService(max_retries=2, retry_backoff=0.01)
        call_fn = MagicMock(return_value="response")
        def _cancel():
            return True
        result = service.call("agent1", call_fn, MagicMock(), _cancel)
        assert result is None

    def test_user_cancelled_after_exception_returns_none(self):
        service = AgentCallService(max_retries=2, retry_backoff=0.01)
        cancel_after = 0
        def _cancel():
            nonlocal cancel_after
            cancel_after += 1
            return cancel_after > 1
        call_fn = MagicMock(side_effect=ValueError("boom"))
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = service.call("agent1", call_fn, MagicMock(), _cancel)
        assert result is None
        assert call_fn.call_count == 1

    def test_rate_limit_backoff_used(self):
        service = AgentCallService(
            max_retries=2, retry_backoff=1.0,
            rate_limit_backoff=0.01,
            is_rate_limited=lambda: True,
        )
        call_fn = MagicMock(return_value=None)
        with patch("quimera.app.agent_call_service.time.sleep") as mock_sleep:
            service.call("agent1", call_fn, MagicMock(), lambda: False)
        mock_sleep.assert_called_with(0.01)

    def test_linear_backoff_without_rate_limit(self):
        service = AgentCallService(
            max_retries=3, retry_backoff=0.5,
            rate_limit_backoff=30.0,
            is_rate_limited=lambda: False,
        )
        call_fn = MagicMock(return_value=None)
        with patch("quimera.app.agent_call_service.time.sleep") as mock_sleep:
            service.call("agent1", call_fn, MagicMock(), lambda: False)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(0.5)
        mock_sleep.assert_any_call(1.0)

    def test_max_retries_one_no_retry(self):
        service = AgentCallService(max_retries=1)
        call_fn = MagicMock(return_value=None)
        resolve_fn = MagicMock()
        record = MagicMock()
        service._record_failure = record
        result = service.call("agent1", call_fn, resolve_fn, lambda: False)
        assert result is None
        call_fn.assert_called_once()
        resolve_fn.assert_not_called()
        record.assert_called_once_with("agent1")

    def test_call_fn_arguments_preserved(self):
        service = AgentCallService(max_retries=1)
        call_fn = MagicMock(return_value="response")
        resolve_fn = MagicMock(return_value="result")
        result = service.call("agent1", call_fn, resolve_fn, lambda: False)
        assert result == "result"
        call_fn.assert_called_once_with("agent1")
        resolve_fn.assert_called_once_with("agent1", "response")

    def test_call_fn_none_then_resolve_succeeds(self):
        record = MagicMock()
        service = AgentCallService(max_retries=3, retry_backoff=0.01, record_failure=record)
        call_responses = [None, "response"]
        call_fn = MagicMock(side_effect=call_responses)
        resolve_fn = MagicMock(return_value="result")
        with patch("quimera.app.agent_call_service.time.sleep"):
            result = service.call("agent1", call_fn, resolve_fn, lambda: False)
        assert result == "result"
        assert call_fn.call_count == 2
        resolve_fn.assert_called_once_with("agent1", "response")
        record.assert_not_called()

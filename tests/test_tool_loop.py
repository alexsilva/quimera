"""Testes para quimera.app.tool_loop — ToolLoopService sem dependência de app."""
from unittest.mock import MagicMock, patch
import pytest

from quimera.app.tool_loop import (
    ToolLoopService,
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
# Fixture
# =============================================================================

def _make_plugin(tool_use_reliability="medium"):
    plugin = MagicMock()
    plugin.tool_use_reliability = tool_use_reliability
    return plugin


def _make_tool_result(ok=True, error=None, error_type="none", payload="{}"):
    tr = MagicMock()
    tr.ok = ok
    tr.error = error
    tr.error_type = error_type
    tr.to_model_payload = MagicMock(return_value=payload)
    return tr


@pytest.fixture
def deps():
    """Conjunto mínimo de dependências injetadas para ToolLoopService."""
    tool_executor = MagicMock()
    plugin_resolver = MagicMock(return_value=_make_plugin("medium"))
    call_agent_fn = MagicMock(return_value="followup response")
    print_response_fn = MagicMock()
    persist_message_fn = MagicMock()
    cancel_checker = MagicMock(return_value=False)
    record_tool_event = MagicMock()
    reset_approve_all = MagicMock()
    return {
        "tool_executor": tool_executor,
        "plugin_resolver": plugin_resolver,
        "call_agent_fn": call_agent_fn,
        "print_response_fn": print_response_fn,
        "persist_message_fn": persist_message_fn,
        "cancel_checker": cancel_checker,
        "record_tool_event": record_tool_event,
        "reset_approve_all": reset_approve_all,
    }


def _make_service(deps):
    return ToolLoopService(**deps)


# =============================================================================
# Testes de resposta sem ferramenta
# =============================================================================

class TestNoTool:
    def test_none_response_returns_none(self, deps):
        svc = _make_service(deps)
        assert svc.execute("agent1", None) is None

    def test_empty_response_returns_empty(self, deps):
        svc = _make_service(deps)
        assert svc.execute("agent1", "") == ""

    def test_response_without_tool_returns_unchanged(self, deps):
        deps["tool_executor"].maybe_execute_from_response = MagicMock(return_value=("raw", None))
        svc = _make_service(deps)
        result = svc.execute("agent1", "hello world")
        assert result == "hello world"
        deps["call_agent_fn"].assert_not_called()

    def test_reset_approve_all_called_even_when_no_tool(self, deps):
        deps["tool_executor"].maybe_execute_from_response = MagicMock(return_value=("raw", None))
        svc = _make_service(deps)
        svc.execute("agent1", "hello world")
        deps["reset_approve_all"].assert_called_once()


# =============================================================================
# Testes de execução com ferramenta bem-sucedida
# =============================================================================

class TestSuccessfulTool:
    def test_single_tool_hop_returns_followup(self, deps):
        tr = _make_tool_result(ok=True)
        call_count = {"n": 0}

        def _maybe_exec(resp):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return (resp, tr)
            return (None, None)

        deps["tool_executor"].maybe_execute_from_response = MagicMock(side_effect=_maybe_exec)
        deps["call_agent_fn"].return_value = "final response"
        svc = _make_service(deps)
        result = svc.execute("agent1", "initial")
        assert result == "final response"

    def test_record_tool_event_called_on_success(self, deps):
        tr = _make_tool_result(ok=True)
        deps["tool_executor"].maybe_execute_from_response = MagicMock(
            side_effect=[("cmd", tr), (None, None)]
        )
        deps["call_agent_fn"].return_value = "done"
        svc = _make_service(deps)
        svc.execute("agent1", "initial")
        deps["record_tool_event"].assert_called()
        call_kwargs = deps["record_tool_event"].call_args_list[0].kwargs
        assert call_kwargs.get("ok") is True
        assert call_kwargs.get("is_invalid") is False

    def test_visible_text_printed_and_persisted(self, deps):
        tr = _make_tool_result(ok=True)
        deps["tool_executor"].maybe_execute_from_response = MagicMock(
            side_effect=[("visible text [TOOL]...", tr), (None, None)]
        )
        deps["call_agent_fn"].return_value = "done"
        with patch("quimera.app.tool_loop.strip_tool_block", return_value="visible text"):
            svc = _make_service(deps)
            svc.execute("agent1", "initial", show_output=True, persist_history=True)
        deps["print_response_fn"].assert_called_once_with("agent1", "visible text")
        deps["persist_message_fn"].assert_called_once_with("agent1", "visible text")

    def test_show_output_false_skips_print(self, deps):
        tr = _make_tool_result(ok=True)
        deps["tool_executor"].maybe_execute_from_response = MagicMock(
            side_effect=[("visible text [TOOL]...", tr), (None, None)]
        )
        deps["call_agent_fn"].return_value = "done"
        with patch("quimera.app.tool_loop.strip_tool_block", return_value="visible text"):
            svc = _make_service(deps)
            svc.execute("agent1", "initial", show_output=False, persist_history=True)
        deps["print_response_fn"].assert_not_called()

    def test_persist_history_false_skips_persist(self, deps):
        tr = _make_tool_result(ok=True)
        deps["tool_executor"].maybe_execute_from_response = MagicMock(
            side_effect=[("text [TOOL]...", tr), (None, None)]
        )
        deps["call_agent_fn"].return_value = "done"
        with patch("quimera.app.tool_loop.strip_tool_block", return_value="text"):
            svc = _make_service(deps)
            svc.execute("agent1", "initial", persist_history=False)
        deps["persist_message_fn"].assert_not_called()

    def test_followup_handoff_truncated_when_too_large(self, deps):
        tr = _make_tool_result(ok=True, payload="x" * 9000)
        deps["tool_executor"].maybe_execute_from_response = MagicMock(
            side_effect=[("cmd", tr), (None, None)]
        )
        deps["call_agent_fn"].return_value = "done"
        svc = _make_service(deps)
        svc.execute("agent1", "initial")
        handoff_arg = deps["call_agent_fn"].call_args.kwargs["handoff"]
        assert handoff_arg.startswith("(histórico truncado)...\n\n")

    def test_call_agent_fn_receives_correct_kwargs(self, deps):
        tr = _make_tool_result(ok=True)
        deps["tool_executor"].maybe_execute_from_response = MagicMock(
            side_effect=[("cmd", tr), (None, None)]
        )
        svc = _make_service(deps)
        svc.execute("agent1", "initial", silent=True)
        _, kwargs = deps["call_agent_fn"].call_args
        assert kwargs["primary"] is False
        assert kwargs["protocol_mode"] == "tool_loop"
        assert kwargs["silent"] is True


# =============================================================================
# Testes de loop inválido (policy)
# =============================================================================

class TestInvalidToolLoop:
    def test_consecutive_invalid_signatures_abort(self, deps):
        deps["plugin_resolver"].return_value = _make_plugin("low")
        tr = _make_tool_result(ok=False, error_type="policy")
        tr.error = "permissão negada"
        tr.tool_name = "shell"
        deps["tool_executor"].maybe_execute_from_response = MagicMock(return_value=("cmd", tr))
        svc = _make_service(deps)
        result = svc.execute("agent1", "initial")
        assert "loop de ferramenta inválida" in (result or "")

    def test_different_invalid_signatures_reset_count(self, deps):
        deps["plugin_resolver"].return_value = _make_plugin("high")
        calls = {"n": 0}

        def _varying_tool(_):
            calls["n"] += 1
            tr = _make_tool_result(ok=False, error_type="policy")
            tr.error = f"erro {calls['n']}"
            tr.tool_name = "shell"
            return ("cmd", tr)

        deps["tool_executor"].maybe_execute_from_response = MagicMock(side_effect=_varying_tool)
        deps["call_agent_fn"].return_value = "cont"
        svc = _make_service(deps)
        # high reliability = alto threshold, loop vai até max_tool_hops
        result = svc.execute("agent1", "initial")
        assert "limite de execuções de ferramenta" in (result or "")

    def test_record_tool_event_called_on_loop_abort(self, deps):
        deps["plugin_resolver"].return_value = _make_plugin("low")
        tr = _make_tool_result(ok=False, error_type="policy")
        tr.error = "permissão"
        tr.tool_name = "shell"
        deps["tool_executor"].maybe_execute_from_response = MagicMock(return_value=("cmd", tr))
        svc = _make_service(deps)
        svc.execute("agent1", "initial")
        abort_calls = [
            c for c in deps["record_tool_event"].call_args_list
            if c.kwargs.get("loop_abort") is True
        ]
        assert len(abort_calls) >= 1
        assert abort_calls[-1].kwargs.get("reason") == "invalid_tool_loop"


# =============================================================================
# Testes de limite máximo de hops
# =============================================================================

class TestMaxToolHops:
    def test_max_hops_reached_returns_abort_message(self, deps):
        deps["plugin_resolver"].return_value = _make_plugin("low")
        tr = _make_tool_result(ok=True)
        deps["tool_executor"].maybe_execute_from_response = MagicMock(return_value=("cmd", tr))
        deps["call_agent_fn"].return_value = "cont"
        svc = _make_service(deps)
        result = svc.execute("agent1", "initial")
        assert "limite de execuções de ferramenta" in (result or "")

    def test_record_tool_event_called_for_max_hops_abort(self, deps):
        deps["plugin_resolver"].return_value = _make_plugin("low")
        tr = _make_tool_result(ok=True)
        deps["tool_executor"].maybe_execute_from_response = MagicMock(return_value=("cmd", tr))
        deps["call_agent_fn"].return_value = "cont"
        svc = _make_service(deps)
        svc.execute("agent1", "initial")
        abort_calls = [
            c for c in deps["record_tool_event"].call_args_list
            if c.kwargs.get("loop_abort") is True
        ]
        assert any(c.kwargs.get("reason") == "max_tool_hops" for c in abort_calls)


# =============================================================================
# Testes de cancelamento
# =============================================================================

class TestCancellation:
    def test_cancel_before_first_hop_returns_none(self, deps):
        deps["cancel_checker"].return_value = True
        deps["tool_executor"].maybe_execute_from_response = MagicMock(
            side_effect=AssertionError("não deve executar ferramenta")
        )
        svc = _make_service(deps)
        result = svc.execute("agent1", "initial")
        assert result is None
        deps["tool_executor"].maybe_execute_from_response.assert_not_called()

    def test_cancel_after_tool_execution_before_followup_returns_none(self, deps):
        tr = _make_tool_result(ok=True)

        def _cancel_after_tool():
            deps["cancel_checker"].return_value = True
            return ("cmd", tr)

        deps["tool_executor"].maybe_execute_from_response = MagicMock(
            side_effect=lambda _: _cancel_after_tool()
        )
        # cancel_checker já retorna True após primeira execução de ferramenta
        svc = _make_service(deps)
        result = svc.execute("agent1", "initial")
        assert result is None
        deps["call_agent_fn"].assert_not_called()

    def test_cancel_after_followup_call_returns_none(self, deps):
        tr = _make_tool_result(ok=True)
        call_count = {"n": 0}

        def _maybe_exec(resp):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return ("cmd", tr)
            return (None, None)

        def _call_agent_fn_cancel(**kwargs):
            deps["cancel_checker"].return_value = True
            return "followup"

        deps["tool_executor"].maybe_execute_from_response = MagicMock(side_effect=_maybe_exec)
        deps["call_agent_fn"].side_effect = lambda agent, **kw: _call_agent_fn_cancel(**kw)
        svc = _make_service(deps)
        result = svc.execute("agent1", "initial")
        assert result is None

    def test_reset_approve_all_called_even_on_cancel(self, deps):
        deps["cancel_checker"].return_value = True
        svc = _make_service(deps)
        svc.execute("agent1", "initial")
        deps["reset_approve_all"].assert_called_once()


# =============================================================================
# Testes de coerção de erro (re-exportados de tool_loop)
# =============================================================================

class TestCoerceToolError:
    def test_none_returns_none(self):
        assert _coerce_tool_error(None) is None

    def test_toolerror_returns_unchanged(self):
        te = ToolError("x")
        assert _coerce_tool_error(te) is te

    def test_validation_heuristic(self):
        assert isinstance(_coerce_tool_error("erro de validação"), ToolValidationError)

    def test_environment_heuristic(self):
        assert isinstance(_coerce_tool_error("arquivo não encontrado"), ToolEnvironmentError)

    def test_logic_heuristic(self):
        assert isinstance(_coerce_tool_error("regra violada"), ToolLogicError)

    def test_rate_limit_heuristic(self):
        assert isinstance(_coerce_tool_error("rate limit excedido"), ToolRateLimitError)

    def test_unknown_string_unchanged(self):
        assert _coerce_tool_error("random string") == "random string"

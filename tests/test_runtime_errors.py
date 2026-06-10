"""Tests for quimera/runtime/errors.py"""
import pytest
from quimera.runtime.errors import (
    ToolError,
    ToolValidationError,
    ToolEnvironmentError,
    ToolLogicError,
    ToolRateLimitError,
    ToolPolicyViolationError,
    TOOL_ERROR_TYPES,
)


class TestToolError:
    def test_base_exception_metadata_default(self):
        """Verifica que ToolError padrão tem metadata vazio."""
        err = ToolError("generic error")
        assert str(err) == "generic error"
        assert err.metadata == {}

    def test_base_exception_with_metadata(self):
        """Verifica que ToolError aceita metadata personalizado."""
        err = ToolError("msg", metadata={"key": "val"})
        assert err.metadata == {"key": "val"}

    def test_base_is_exception(self):
        """Verifica que ToolError é subclasse de Exception."""
        assert issubclass(ToolError, Exception)


class TestToolValidationError:
    def test_default_field_hint_none(self):
        """Verifica que ToolValidationError padrão tem metadata vazio."""
        err = ToolValidationError("invalid")
        assert err.metadata == {}

    def test_with_field(self):
        """Verifica que ToolValidationError aceita field no metadata."""
        err = ToolValidationError("invalid", field="name")
        assert err.metadata == {"field": "name"}

    def test_with_hint(self):
        """Verifica que ToolValidationError aceita hint no metadata."""
        err = ToolValidationError("invalid", hint="use X")
        assert err.metadata == {"hint": "use X"}

    def test_with_both(self):
        """Verifica que ToolValidationError aceita field e hint simultaneamente."""
        err = ToolValidationError("invalid", field="email", hint="format")
        assert err.metadata == {"field": "email", "hint": "format"}

    def test_is_subclass(self):
        """Verifica que ToolValidationError é subclasse de ToolError."""
        assert issubclass(ToolValidationError, ToolError)


class TestToolEnvironmentError:
    def test_default_action_path_none(self):
        """Verifica que ToolEnvironmentError padrão tem metadata vazio."""
        err = ToolEnvironmentError("env fail")
        assert err.metadata == {}

    def test_with_action(self):
        """Verifica que ToolEnvironmentError aceita action no metadata."""
        err = ToolEnvironmentError("env fail", action="read")
        assert err.metadata == {"action": "read"}

    def test_with_path(self):
        """Verifica que ToolEnvironmentError aceita path no metadata."""
        err = ToolEnvironmentError("env fail", path="/tmp/x")
        assert err.metadata == {"path": "/tmp/x"}

    def test_with_both(self):
        """Verifica que ToolEnvironmentError aceita action e path simultaneamente."""
        err = ToolEnvironmentError("env fail", action="write", path="/tmp/x")
        assert err.metadata == {"action": "write", "path": "/tmp/x"}

    def test_is_subclass(self):
        """Verifica que ToolEnvironmentError é subclasse de ToolError."""
        assert issubclass(ToolEnvironmentError, ToolError)


class TestToolLogicError:
    def test_default_rule_context_none(self):
        """Verifica que ToolLogicError padrão tem metadata vazio."""
        err = ToolLogicError("logic fail")
        assert err.metadata == {}

    def test_with_rule(self):
        """Verifica que ToolLogicError aceita rule no metadata."""
        err = ToolLogicError("logic fail", rule="no_recurse")
        assert err.metadata == {"rule": "no_recurse"}

    def test_with_context(self):
        """Verifica que ToolLogicError aceita context no metadata."""
        err = ToolLogicError("logic fail", context={"count": 3})
        assert err.metadata == {"count": 3}

    def test_is_subclass(self):
        """Verifica que ToolLogicError é subclasse de ToolError."""
        assert issubclass(ToolLogicError, ToolError)


class TestToolRateLimitError:
    def test_default_retry_after_none(self):
        """Verifica que ToolRateLimitError padrão tem metadata vazio."""
        err = ToolRateLimitError("rate limited")
        assert err.metadata == {}

    def test_with_retry_after(self):
        """Verifica que ToolRateLimitError aceita retry_after no metadata."""
        err = ToolRateLimitError("rate limited", retry_after=5.0)
        assert err.metadata == {"retry_after": 5.0}

    def test_is_subclass(self):
        """Verifica que ToolRateLimitError é subclasse de ToolError."""
        assert issubclass(ToolRateLimitError, ToolError)


class TestToolPolicyViolationError:
    def test_default_hint_rule_none(self):
        """Verifica que ToolPolicyViolationError padrão tem metadata vazio."""
        err = ToolPolicyViolationError("blocked")
        assert err.metadata == {}

    def test_with_hint(self):
        """Verifica que ToolPolicyViolationError aceita hint no metadata."""
        err = ToolPolicyViolationError("blocked", hint="use allowed cmd")
        assert err.metadata == {"hint": "use allowed cmd"}

    def test_with_rule(self):
        """Verifica que ToolPolicyViolationError aceita rule no metadata."""
        err = ToolPolicyViolationError("blocked", rule="no_chain")
        assert err.metadata == {"rule": "no_chain"}

    def test_with_both(self):
        """Verifica que ToolPolicyViolationError aceita hint e rule simultaneamente."""
        err = ToolPolicyViolationError("blocked", hint="try X", rule="no_chain")
        assert err.metadata == {"hint": "try X", "rule": "no_chain"}

    def test_is_subclass(self):
        """Verifica que ToolPolicyViolationError é subclasse de ToolError."""
        assert issubclass(ToolPolicyViolationError, ToolError)


class TestTOOL_ERROR_TYPES:
    def test_validation_mapping(self):
        """Verifica que TOOL_ERROR_TYPES mapeia validation para ToolValidationError."""
        assert TOOL_ERROR_TYPES["validation"] is ToolValidationError

    def test_environment_mapping(self):
        """Verifica que TOOL_ERROR_TYPES mapeia environment para ToolEnvironmentError."""
        assert TOOL_ERROR_TYPES["environment"] is ToolEnvironmentError

    def test_logic_mapping(self):
        """Verifica que TOOL_ERROR_TYPES mapeia logic para ToolLogicError."""
        assert TOOL_ERROR_TYPES["logic"] is ToolLogicError

    def test_policy_mapping(self):
        """Verifica que TOOL_ERROR_TYPES mapeia policy para ToolPolicyViolationError."""
        assert TOOL_ERROR_TYPES["policy"] is ToolPolicyViolationError

    def test_rate_limit_mapping(self):
        """Verifica que TOOL_ERROR_TYPES mapeia rate_limit para ToolRateLimitError."""
        assert TOOL_ERROR_TYPES["rate_limit"] is ToolRateLimitError

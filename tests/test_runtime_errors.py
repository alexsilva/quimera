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

_METADATA_SUBCLASSES = [
    ToolValidationError,
    ToolEnvironmentError,
    ToolLogicError,
    ToolRateLimitError,
    ToolPolicyViolationError,
]

_SINGLE_FIELD_CASES = [
    (ToolValidationError, {"field": "name"}, {"field": "name"}),
    (ToolValidationError, {"hint": "use X"}, {"hint": "use X"}),
    (ToolEnvironmentError, {"action": "read"}, {"action": "read"}),
    (ToolEnvironmentError, {"path": "/tmp/x"}, {"path": "/tmp/x"}),
    (ToolLogicError, {"rule": "no_recurse"}, {"rule": "no_recurse"}),
    (ToolLogicError, {"context": {"count": 3}}, {"count": 3}),
    (ToolRateLimitError, {"retry_after": 5.0}, {"retry_after": 5.0}),
    (ToolPolicyViolationError, {"hint": "use allowed cmd"}, {"hint": "use allowed cmd"}),
    (ToolPolicyViolationError, {"rule": "no_chain"}, {"rule": "no_chain"}),
]

_MULTI_FIELD_CASES = [
    (ToolValidationError, {"field": "email", "hint": "format"}, {"field": "email", "hint": "format"}),
    (ToolEnvironmentError, {"action": "write", "path": "/tmp/x"}, {"action": "write", "path": "/tmp/x"}),
    (ToolPolicyViolationError, {"hint": "try X", "rule": "no_chain"}, {"hint": "try X", "rule": "no_chain"}),
]


def test_base_error_metadata_default():
    """Verifica que ToolError padrão tem metadata vazio."""
    err = ToolError("generic error")
    assert str(err) == "generic error"
    assert err.metadata == {}


def test_base_error_with_metadata():
    """Verifica que ToolError aceita metadata personalizado."""
    err = ToolError("msg", metadata={"key": "val"})
    assert err.metadata == {"key": "val"}


def test_tool_error_is_exception():
    """Verifica que ToolError é subclasse de Exception."""
    assert issubclass(ToolError, Exception)


@pytest.mark.parametrize("error_cls", _METADATA_SUBCLASSES)
def test_error_metadata_default(error_cls):
    """Verifica que classes derivadas de ToolError têm metadata vazio por padrão."""
    err = error_cls("msg")
    assert err.metadata == {}


@pytest.mark.parametrize(("error_cls", "kwargs", "expected"), _SINGLE_FIELD_CASES)
def test_error_metadata_single_field(error_cls, kwargs, expected):
    """Verifica que classes derivadas mapeiam um campo extra para metadata."""
    err = error_cls("msg", **kwargs)
    assert err.metadata == expected


@pytest.mark.parametrize(("error_cls", "kwargs", "expected"), _MULTI_FIELD_CASES)
def test_error_metadata_multiple_fields(error_cls, kwargs, expected):
    """Verifica que classes derivadas aceitam múltiplos campos simultaneamente."""
    err = error_cls("msg", **kwargs)
    assert err.metadata == expected


@pytest.mark.parametrize("error_cls", _METADATA_SUBCLASSES)
def test_error_is_tool_error_subclass(error_cls):
    """Verifica que todas as classes derivadas são subclasses de ToolError."""
    assert issubclass(error_cls, ToolError)


@pytest.mark.parametrize(
    ("key", "error_cls"),
    [
        ("validation", ToolValidationError),
        ("environment", ToolEnvironmentError),
        ("logic", ToolLogicError),
        ("policy", ToolPolicyViolationError),
        ("rate_limit", ToolRateLimitError),
    ],
)
def test_tool_error_types_mapping(key, error_cls):
    """Verifica que TOOL_ERROR_TYPES mapeia cada chave para a classe correta."""
    assert TOOL_ERROR_TYPES[key] is error_cls
import pytest

from quimera.runtime.models import ToolCall, ToolResult
from quimera.runtime.registry import ToolRegistry


def dummy_handler(call: ToolCall) -> ToolResult:
    return ToolResult(tool_name=call.name, status="success", output="done")


def test_registry_register_and_get():
    """Verifica que register e get funcionam para ferramentas registradas."""
    registry = ToolRegistry()
    registry.register("test", dummy_handler)
    assert registry.get("test") == dummy_handler


def test_registry_get_not_found():
    """Verifica que get levanta KeyError para ferramenta não registrada."""
    registry = ToolRegistry()
    with pytest.raises(KeyError, match="Ferramenta não registrada: missing"):
        registry.get("missing")


def test_registry_names():
    """Verifica que names retorna ferramentas em ordem alfabética."""
    registry = ToolRegistry()
    registry.register("b", dummy_handler)
    registry.register("a", dummy_handler)
    assert registry.names() == ["a", "b"]

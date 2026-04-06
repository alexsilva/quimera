import pytest
from quimera.runtime.registry import ToolRegistry
from quimera.runtime.models import ToolCall, ToolResult

def dummy_handler(call: ToolCall) -> ToolResult:
    return ToolResult(tool_name=call.name, status="success", output="done")

def test_registry_register_and_get():
    registry = ToolRegistry()
    registry.register("test", dummy_handler)
    assert registry.get("test") == dummy_handler

def test_registry_get_not_found():
    registry = ToolRegistry()
    with pytest.raises(KeyError, match="Ferramenta não registrada: missing"):
        registry.get("missing")

def test_registry_names():
    registry = ToolRegistry()
    registry.register("b", dummy_handler)
    registry.register("a", dummy_handler)
    assert registry.names() == ["a", "b"]

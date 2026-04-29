from quimera.runtime.errors import ToolValidationError
from quimera.runtime.models import ToolResult, ToolCall


def test_tool_result_to_model_payload():
    result = ToolResult(
        ok=True,
        tool_name="test",
        content="success",
        error=None,
        exit_code=0,
        duration_ms=100,
        truncated=False,
        data={"meta": "info"}
    )
    payload = result.to_model_payload()
    assert payload["ok"] is True
    assert payload["tool_name"] == "test"
    assert payload["content"] == "success"
    assert payload["data"] == {"meta": "info"}


def test_tool_call_init():
    call = ToolCall(name="test", arguments={"a": 1})
    assert call.name == "test"
    assert call.arguments == {"a": 1}
    assert call.metadata == {}


def test_tool_result_to_model_payload_includes_error_metadata():
    result = ToolResult(
        ok=False,
        tool_name="test",
        error=ToolValidationError("Campo inválido", field="path", hint="use caminho absoluto"),
    )

    payload = result.to_model_payload()

    assert payload["error"] == "Campo inválido"
    assert payload["error_type"] == "validation"
    assert payload["error_metadata"] == {"field": "path", "hint": "use caminho absoluto"}


def test_tool_result_to_prompt_payload_is_minimal_and_truncated():
    result = ToolResult(
        ok=False,
        tool_name="run_shell",
        content="a" * 80,
        error="b" * 80,
        exit_code=7,
        truncated=False,
        data={"session_id": 1},
    )

    payload = result.to_prompt_payload(max_chars=40)

    assert set(payload) == {"ok", "content", "error", "truncated", "exit_code"}
    assert payload["ok"] is False
    assert payload["exit_code"] == 7
    assert payload["truncated"] is True
    assert "resultado com 80 caracteres" in payload["content"]
    assert "resultado com 80 caracteres" in payload["error"]

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

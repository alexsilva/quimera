from quimera.runtime.errors import ToolPolicyViolationError
from quimera.runtime.models import ToolResult, ToolCall


def test_tool_call_init():
    """Verifica que ToolCall é inicializado com name e arguments."""
    call = ToolCall(name="test", arguments={"a": 1})
    assert call.name == "test"
    assert call.arguments == {"a": 1}
    assert call.metadata == {}


def test_tool_result_to_prompt_payload_is_minimal_and_truncated():
    """Verifica que to_prompt_payload retorna subconjunto mínimo de campos com truncamento."""
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

    assert set(payload) == {"ok", "content", "error", "error_type", "hint", "truncated", "exit_code"}
    assert payload["ok"] is False
    assert payload["exit_code"] == 7
    assert payload["error_type"] == "generic"
    assert payload["hint"] is None
    assert payload["truncated"] is True
    assert "resultado com 80 caracteres" in payload["content"]
    assert "resultado com 80 caracteres" in payload["error"]


def test_tool_result_to_prompt_payload_includes_policy_type_and_hint():
    """Verifica que to_prompt_payload inclui error_type policy e hint."""
    result = ToolResult(
        ok=False,
        tool_name="run_shell",
        error=ToolPolicyViolationError(
            "Comando bloqueado: operador de encadeamento proibido: '&&'",
            hint="Use um comando por vez.",
        ),
    )

    payload = result.to_prompt_payload(max_chars=200)

    assert payload["error_type"] == "policy"
    assert payload["hint"] == "Use um comando por vez."

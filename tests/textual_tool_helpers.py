"""Mensagens de tool no contrato estruturado, para os testes do feed Textual."""
from __future__ import annotations

from typing import Any

from quimera.domain.tool_line import build_tool_line


def tool_msg(
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    status: str = "running",
    tool_call_id: str | None = None,
    duration_ms: int | None = None,
    tail: str | None = None,
    transport: str = "",
    exit_code: int | None = None,
) -> dict[str, Any]:
    """``message_dict`` de ``tool_preview`` como os produtores o enviam."""
    line = build_tool_line(
        tool,
        args or {},
        status=status,
        tool_call_id=tool_call_id,
        duration_ms=duration_ms,
        tail=tail,
        transport=transport,
        exit_code=exit_code,
    )
    message: dict[str, Any] = {"content": line.to_text(), "tool_line": line.to_payload()}
    if tool_call_id:
        message["tool_call_id"] = tool_call_id
    return message


def tool_texts(tools: list[dict[str, Any]] | None) -> list[str]:
    """Texto de cada entrada do bloco de tools (marcador incluso)."""
    return [str(entry.get("text") or "") for entry in tools or ()]

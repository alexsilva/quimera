from __future__ import annotations

import json
import re
from typing import Any

from .models import ToolCall

TOOL_BLOCK_PATTERN = re.compile(r"```tool\s*(\{.*?\})\s*```", re.DOTALL)


class ToolCallParseError(Exception):
    pass


def extract_tool_call(response: str | None) -> ToolCall | None:
    if not response:
        return None

    match = TOOL_BLOCK_PATTERN.search(response)
    if not match:
        return None

    try:
        payload: dict[str, Any] = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ToolCallParseError("Bloco tool inválido") from exc

    name = payload.get("name")
    arguments = payload.get("arguments", {})
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise ToolCallParseError("Payload de tool inválido")

    return ToolCall(
        name=name,
        arguments=arguments,
        call_id=payload.get("id"),
        metadata=payload.get("metadata") or {},
    )


def strip_tool_block(response: str) -> str:
    return TOOL_BLOCK_PATTERN.sub("", response).strip()

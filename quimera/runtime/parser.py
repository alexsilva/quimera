from __future__ import annotations

import json
import re
from typing import Any

from .models import ToolCall

TOOL_FENCE_PATTERN = re.compile(r"```tool\s*([\s\S]*?)```")


class ToolCallParseError(Exception):
    pass


def _parse_json_object(text: str) -> dict[str, Any]:
    """Extrai o primeiro objeto JSON completo de *text* usando o decoder nativo,
    que conta chaves balanceadas — suporta objetos aninhados corretamente."""
    start = text.find("{")
    if start == -1:
        raise ToolCallParseError("Nenhum objeto JSON encontrado no bloco tool")
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise ToolCallParseError("Bloco tool inválido") from exc
    if not isinstance(obj, dict):
        raise ToolCallParseError("Bloco tool inválido: esperado objeto JSON")
    return obj


def extract_tool_call(response: str | None) -> ToolCall | None:
    if not response:
        return None

    match = TOOL_FENCE_PATTERN.search(response)
    if not match:
        return None

    payload = _parse_json_object(match.group(1))

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
    return TOOL_FENCE_PATTERN.sub("", response).strip()

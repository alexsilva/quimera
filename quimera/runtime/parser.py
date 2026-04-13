"""Componentes de `quimera.runtime.parser`."""
from __future__ import annotations

import json
import re
from typing import Any

from .models import ToolCall

TOOL_TAG_PATTERN = re.compile(r"<tool\b([^>]*)\/>|<tool\b([^>]*)>([\s\S]*?)</tool>", re.IGNORECASE)
TOOL_ATTR_PATTERN = re.compile(r'([A-Za-z_][\w-]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s/>]+))', re.DOTALL)


class ToolCallParseError(Exception):
    """Implementa `ToolCallParseError`."""
    pass


def _parse_json_object(text: str) -> dict[str, Any]:
    """Extrai o primeiro objeto JSON completo de *text*."""
    start = text.find("{")
    if start == -1:
        raise ToolCallParseError("Nenhum objeto JSON encontrado no payload da tool")
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise ToolCallParseError("Payload de tool inválido") from exc
    if not isinstance(obj, dict):
        raise ToolCallParseError("Payload de tool inválido: esperado objeto JSON")
    return obj


def _parse_tool_tag_attributes(raw_attrs: str) -> dict[str, str]:
    """Interpreta tool tag attributes."""
    attrs: dict[str, str] = {}
    for match in TOOL_ATTR_PATTERN.finditer(raw_attrs or ""):
        key = match.group(1)
        value = match.group(2) or match.group(3) or match.group(4) or ""
        attrs[key] = value
    return attrs


def _build_tool_call_from_tag(raw_attrs: str, body: str = "") -> ToolCall:
    """Monta tool call from tag."""
    attrs = _parse_tool_tag_attributes(raw_attrs)
    name = attrs.pop("function", None) or attrs.pop("name", None)
    if not isinstance(name, str) or not name.strip():
        raise ToolCallParseError("Payload de tool inválido")

    call_id = attrs.pop("id", None)
    raw_arguments = attrs.pop("arguments", None)
    arguments: dict[str, Any]
    if raw_arguments is not None:
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ToolCallParseError("Payload de tool inválido") from exc
        if not isinstance(parsed, dict):
            raise ToolCallParseError("Payload de tool inválido")
        arguments = parsed
    else:
        arguments = dict(attrs)

    stripped_body = body.strip()
    if stripped_body:
        if not arguments:
            if stripped_body.startswith("{"):
                parsed_body = _parse_json_object(stripped_body)
                if "name" in parsed_body or "arguments" in parsed_body:
                    nested_name = parsed_body.get("name")
                    nested_args = parsed_body.get("arguments", {})
                    if nested_name and nested_name != name:
                        raise ToolCallParseError("Payload de tool inválido")
                    if not isinstance(nested_args, dict):
                        raise ToolCallParseError("Payload de tool inválido")
                    arguments = nested_args
                    call_id = parsed_body.get("id", call_id)
                else:
                    arguments = parsed_body
            else:
                raise ToolCallParseError("Payload de tool inválido")
        else:
            raise ToolCallParseError("Payload de tool inválido")

    return ToolCall(
        name=name,
        arguments=arguments,
        call_id=call_id,
    )


def extract_tool_call(response: str | None) -> ToolCall | None:
    """Executa extract tool call."""
    if not response:
        return None

    tag_match = TOOL_TAG_PATTERN.search(response)
    if not tag_match:
        return None
    raw_attrs = tag_match.group(1) or tag_match.group(2) or ""
    body = tag_match.group(3) or ""
    return _build_tool_call_from_tag(raw_attrs, body)


def strip_tool_block(response: str) -> str:
    """Remove tool block."""
    return TOOL_TAG_PATTERN.sub("", response).strip()

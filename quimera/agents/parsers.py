"""Parsers de formatos de saída dos agentes CLI (stream-json, codex-json, opencode-json)."""
import json
import logging

from quimera.agent_events import _SyntheticToolResult

_logger = logging.getLogger(__name__)


def parse_stream_json(raw: str, agent: str, tool_event_callback=None) -> str | None:
    """Parseia output em stream-json do CLI, extrai texto final e dispara callbacks de tool."""
    result_text = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "result":
            if event.get("is_error"):
                _logger.warning("[stream-json] agent=%s reported error: %s", agent, event.get("result"))
                return None
            result_text = event.get("result") or ""
        elif etype == "assistant":
            content = event.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "tool_use" and tool_event_callback:
                    tool_name = block.get("name", "unknown")
                    _logger.debug("[stream-json] agent=%s used tool=%s", agent, tool_name)
                    tool_event_callback(agent, result=_SyntheticToolResult(ok=True))
    return result_text


def parse_codex_json(raw: str, agent: str, tool_event_callback=None) -> str | None:
    """Parseia output JSONL do `codex exec --json`, extrai último agent_message e registra tool calls."""
    result_text = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "item.completed":
            item = event.get("item", {})
            itype = item.get("type")
            if itype == "agent_message":
                result_text = item.get("text") or ""
            elif itype == "command_execution" and tool_event_callback:
                cmd = item.get("command", "unknown")
                ok = item.get("exit_code") == 0
                _logger.debug("[codex-json] agent=%s ran command=%s ok=%s", agent, cmd, ok)
                tool_event_callback(agent, result=_SyntheticToolResult(ok=ok))
    return result_text


def parse_opencode_json(raw: str, agent: str, tool_event_callback=None) -> str | None:
    """Parseia eventos JSON do `opencode run --format=json` e recompõe o texto final."""
    text_parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type")
        part = event.get("part", {}) or {}
        ptype = part.get("type")

        if etype == "text" or ptype == "text":
            text = part.get("text") or ""
            if text:
                text_parts.append(text)
            continue

        if not tool_event_callback:
            continue

        tool_name = (
            part.get("tool")
            or part.get("tool_name")
            or part.get("name")
            or event.get("tool")
            or event.get("tool_name")
            or event.get("name")
        )
        marker = " ".join(filter(None, [str(etype or ""), str(ptype or "")])).lower()
        if tool_name and any(token in marker for token in {"tool", "call"}):
            _logger.debug("[opencode-json] agent=%s used tool=%s", agent, tool_name)
            tool_event_callback(agent, result=_SyntheticToolResult(ok=True))

    if not text_parts:
        return None
    return "\n".join(text_parts).strip() or None

"""Parsers de formatos de saída dos agentes CLI (stream-json, codex-json, opencode-json)."""
import json
import logging
import re
import time
from dataclasses import dataclass

from quimera.agent_events import _SyntheticToolResult
from quimera.agents.text_filters import _is_rate_limit_signal

_logger = logging.getLogger(__name__)

# Mensagens do Codex CLI para cota da conta esgotada ("You've hit your usage
# limit", "Your workspace is out of credits", "You hit your spend cap").
_QUOTA_MESSAGE_RE = re.compile(
    r"\bout of (?:usage )?credits\b|\bhit your (?:usage limit|spend cap)\b|\busage limit\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ProviderRateLimit:
    """Rate limit ou cota esgotada que o CLI relatou no próprio stream de eventos."""

    message: str
    retry_after: float | None = None


def stream_rate_limit(raw: str | None, *, now: float | None = None) -> ProviderRateLimit | None:
    """Detecta no stdout do CLI a recusa do provedor por rate limit ou cota.

    Claude CLI (stream-json): evento ``result`` com ``is_error`` e
    ``api_error_status`` 429; o prazo vem do ``rate_limit_event`` rejeitado
    (``resetsAt``, epoch em segundos). Codex CLI (``exec --json``):
    ``turn.failed`` com mensagem de limite de uso, crédito ou rate limit.
    """
    resets_at: float | None = None
    for event in _json_events(raw or ""):
        etype = event.get("type")
        if etype == "rate_limit_event":
            info = event.get("rate_limit_info") or {}
            if info.get("status") == "rejected" and isinstance(info.get("resetsAt"), (int, float)):
                resets_at = float(info["resetsAt"])
        elif etype == "result" and event.get("is_error") and event.get("api_error_status") == 429:
            retry_after = None
            if resets_at is not None:
                retry_after = max(resets_at - (time.time() if now is None else now), 0.0)
            return ProviderRateLimit(str(event.get("result") or "rate limit do provedor"), retry_after)
        elif etype == "turn.failed":
            message = str((event.get("error") or {}).get("message") or "")
            if _QUOTA_MESSAGE_RE.search(message) or _is_rate_limit_signal(message):
                return ProviderRateLimit(message)
    return None


def _json_events(raw: str):
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def parse_stream_json(raw: str, agent: str, tool_event_callback=None) -> str | None:
    """Parseia output em stream-json do CLI, extrai texto final e dispara callbacks de tool."""
    result_text = None
    text_parts: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "result":
            if event.get("is_error") or event.get("status") == "error":
                _logger.warning("[stream-json] agent=%s reported error: %s", agent, event.get("result") or event.get("error"))
                return None
            val = event.get("result")
            if val is not None:
                result_text = val
        elif etype == "message" and event.get("role") == "assistant":
            content = event.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text") or "")
        elif etype == "tool_use":
            if tool_event_callback:
                tool_name = event.get("tool_name", "unknown")
                _logger.debug("[stream-json] agent=%s used tool=%s", agent, tool_name)
                tool_event_callback(agent, result=_SyntheticToolResult(ok=True))
        elif etype == "assistant":
            content = event.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text") or "")
                elif block.get("type") == "tool_use" and tool_event_callback:
                    tool_name = block.get("name", "unknown")
                    _logger.debug("[stream-json] agent=%s used tool=%s", agent, tool_name)
                    tool_event_callback(agent, result=_SyntheticToolResult(ok=True))

    if result_text is not None:
        return result_text
    if text_parts:
        return "".join(text_parts).strip() or None
    return None


def parse_codex_json(raw: str, agent: str, tool_event_callback=None) -> str | None:
    """Parseia output JSONL do `codex exec --json`, extrai último agent_message e registra tool calls."""
    result_text = None
    for raw_line in raw.splitlines():
        line = raw_line.strip()
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
    for raw_line in raw.splitlines():
        line = raw_line.strip()
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

"""Componentes de `quimera.plugins.claude`."""
import json
from pathlib import Path

from quimera.agent_events import SpyEvent
from quimera.plugins.base import AgentPlugin, register
from quimera.plugins.spy_utils import truncate_spy_text


def _truncate_text(value: str, limit: int = 160) -> str:
    return truncate_spy_text(value, limit=limit)


def _format_claude_spy_event(line: str) -> list[SpyEvent]:
    """Resume eventos stream-json do Claude em mensagens úteis para o modo spy."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []

    etype = event.get("type")
    if etype == "result":
        if event.get("is_error"):
            detail = _truncate_text(str(event.get("result") or "erro"))
            return [SpyEvent(kind="context", text=f"execução falhou: {detail}", transient=True)]
        return [SpyEvent(kind="context", text="execução concluída", transient=True)]

    if etype != "assistant":
        return []

    messages: list[SpyEvent] = []
    content = event.get("message", {}).get("content", [])
    for block in content:
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                messages.append(SpyEvent(kind="response", text=text, final=True))
        elif btype == "tool_use":
            tool_name = block.get("name") or "ferramenta"
            messages.append(SpyEvent(kind="tool", text=f"usando {tool_name}"))
    return messages


plugin = AgentPlugin(
    name="claude",
    prefix="/claude",
    icon="🔮",
    runtime_rw_paths=[str(Path.home() / ".claude")],
    cmd=["claude", "--permission-mode=bypassPermissions", "--output-format=stream-json", "--verbose", "-p"],
    output_format="stream-json",
    style=("magenta", "Claude"),
    capabilities=["architecture", "code_review", "planning", "documentation", "code_editing"],
    preferred_task_types=["architecture", "code_review", "documentation", "code_edit", "general"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    tool_use_reliability="high",
    supports_code_editing=True,
    spy_stdout_formatter=_format_claude_spy_event,
    supports_long_context=True,
    base_tier=3,
)
register(plugin)

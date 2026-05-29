"""Componentes de `quimera.plugins.gemini`."""
import json
from quimera.agent_events import SpyEvent
from quimera.plugins.base import AgentPlugin, register
from quimera.plugins.spy_utils import format_agent_message_lines


def _format_gemini_spy_event(line: str) -> list[SpyEvent]:
    """Resume eventos stream-json do Gemini em mensagens curtas para o modo SUMMARY."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []

    etype = event.get("type")
    if etype == "assistant":
        content = event.get("message", {}).get("content", [])
        for block in content:
            if block.get("type") == "text":
                text = block.get("text") or ""
                if text:
                    return format_agent_message_lines(text)
            if block.get("type") == "tool_use":
                tool_name = block.get("name", "unknown")
                return [SpyEvent(kind="tool", text=f"usando {tool_name}")]
    return []


class GeminiPlugin(AgentPlugin):
    """Plugin do Gemini (sem suporte a MCP)."""


plugin = GeminiPlugin(
    name="gemini",
    prefix="/gemini",
    icon="🧭",
    cmd=["gemini", "--approval-mode=yolo", "--skip-trust", "--output-format=stream-json", "-p"],
    style=("cyan", "Gemini"),
    prompt_as_arg=True,
    output_format="stream-json",
    spy_stdout_formatter=_format_gemini_spy_event,
    stderr_noise_patterns=[
        r"Warning: Basic terminal detected",
        r"Warning: 256-color support not detected",
        r"YOLO mode is enabled",
        r"Ripgrep is not available",
    ],
    capabilities=["code_review", "documentation", "general_reasoning", "code_editing", "complex_refactoring",
                  "multimodal_analysis"],
    preferred_task_types=["code_review", "documentation", "general", "code_edit", "architecture", "bug_investigation"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    tool_use_reliability="high",
    supports_code_editing=True,
    supports_long_context=True,
    base_tier=3,
)
register(plugin)

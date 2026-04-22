"""Componentes de `quimera.plugins.opencode`."""
import json
from pathlib import Path

from quimera.agent_events import SpyEvent
from quimera.plugins.base import AgentPlugin, register
from quimera.plugins.spy_utils import format_agent_message_lines


def _format_opencode_spy_event(line: str) -> list[SpyEvent]:
    """Resume eventos JSON do OpenCode em mensagens curtas para o modo SUMMARY."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []

    etype = event.get("type")
    part = event.get("part", {}) or {}
    ptype = part.get("type")

    if etype == "step_start" or ptype == "step-start":
        return [SpyEvent(kind="context", text="iniciando execução", transient=True)]
    if etype == "step_finish" or ptype == "step-finish":
        reason = (part.get("reason") or "").strip().lower()
        if reason in {"error", "failed", "fail"}:
            return [SpyEvent(kind="context", text="execução falhou", transient=True)]
        return [SpyEvent(kind="context", text="execução concluída", transient=True)]

    if etype == "text" or ptype == "text":
        return format_agent_message_lines(part.get("text") or "")

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
        return [SpyEvent(kind="tool", text=f"usando {tool_name}")]

    return []

_OPENCODE_RW_PATHS = [
    str(Path.home() / ".local" / "share" / "opencode"),
    str(Path.home() / ".local" / "state" / "opencode"),
]

plugin = AgentPlugin(
    name="opencode-pickle",
    prefix="/opencode-pickle",
    icon="🥒",
    runtime_rw_paths=_OPENCODE_RW_PATHS,
    cmd=["opencode", "--model=opencode/big-pickle", "run", "--format=json"],
    output_format="opencode-json",
    style=("blue", "OpenCodePickle"),
    capabilities=["general_coding", "code_review", "code_editing"],
    preferred_task_types=["code_edit", "code_review"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    supports_code_editing=True,
    spy_stdout_formatter=_format_opencode_spy_event,
    supports_long_context=False, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-gpt",
    prefix="/opencode-gpt",
    icon="✏️",
    runtime_rw_paths=_OPENCODE_RW_PATHS,
    cmd=["opencode", "--model=opencode/gpt-5-nano", "run", "--format=json"],
    output_format="opencode-json",
    style=("blue", "OpenCodeGPT"),
    capabilities=["general_reasoning", "code_review", "documentation"],
    preferred_task_types=["documentation", "code_review"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    supports_code_editing=False,
    spy_stdout_formatter=_format_opencode_spy_event,
    supports_long_context=False, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-mimo-omni",
    prefix="/opencode-mimo-omni",
    icon="🧐",
    runtime_rw_paths=_OPENCODE_RW_PATHS,
    cmd=["opencode", "--model=opencode/mimo-v2-omni-free", "run", "--format=json"],
    output_format="opencode-json",
    style=("blue", "OpenCodeMimoOmni"),
    capabilities=["general_reasoning", "code_review"],
    preferred_task_types=["code_review"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    supports_code_editing=True,
    spy_stdout_formatter=_format_opencode_spy_event,
    supports_long_context=False, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-omni-pro",
    prefix="/opencode-omni-pro",
    icon="🏛",
    runtime_rw_paths=_OPENCODE_RW_PATHS,
    cmd=["opencode", "--model=opencode/mimo-v2-pro-free", "run", "--format=json"],
    output_format="opencode-json",
    style=("blue", "OpenCodeOmniPro"),
    capabilities=["architecture", "code_review", "planning"],
    preferred_task_types=["architecture", "code_review"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    supports_code_editing=True,
    spy_stdout_formatter=_format_opencode_spy_event,
    supports_long_context=True, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-minimax",
    prefix="/opencode-minimax",
    icon="📚",
    runtime_rw_paths=_OPENCODE_RW_PATHS,
    cmd=["opencode", "--model=opencode/minimax-m2.5-free", "run", "--format=json"],
    output_format="opencode-json",
    style=("blue", "OpenCodeMiniMax"),
    capabilities=["documentation", "code_review", "general_reasoning"],
    preferred_task_types=["documentation", "code_review"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    supports_code_editing=True,
    spy_stdout_formatter=_format_opencode_spy_event,
    supports_long_context=False, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-nemotron",
    prefix="/opencode-nemotron",
    icon="🐞",
    runtime_rw_paths=_OPENCODE_RW_PATHS,
    cmd=["opencode", "--model=opencode/nemotron-3-super-free", "run", "--format=json"],
    output_format="opencode-json",
    style=("blue", "OpenCodeNemotron"),
    capabilities=["bug_investigation", "code_review", "general_reasoning"],
    preferred_task_types=["bug_investigation", "code_review"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    supports_code_editing=True,
    spy_stdout_formatter=_format_opencode_spy_event,
    supports_long_context=False, base_tier=2,
)
register(plugin)

plugin = AgentPlugin(
    name="opencode-qwen",
    prefix="/opencode-qwen",
    icon="⚙",
    runtime_rw_paths=_OPENCODE_RW_PATHS,
    cmd=["opencode", "--model=opencode/qwen3.6-plus-free", "run", "--format=json"],
    output_format="opencode-json",
    style=("blue", "OpenCodeQwen"),
    capabilities=["code_editing", "code_review", "bug_investigation", "general_coding"],
    preferred_task_types=["code_edit", "code_review", "bug_investigation"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    supports_code_editing=True,
    spy_stdout_formatter=_format_opencode_spy_event,
    supports_long_context=False, base_tier=2,
)
register(plugin)

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

# Campos fixos compartilhados por todos os plugins opencode.
_OPENCODE_DEFAULTS = dict(
    runtime_rw_paths=_OPENCODE_RW_PATHS,
    output_format="opencode-json",
    spy_stdout_formatter=_format_opencode_spy_event,
    supports_tools=True,
    has_builtin_tools=True,
    supports_code_editing=True,
    supports_long_context=False,
    base_tier=2,
)

# Plugin base genérico: cmd sem modelo fixo — usado como template para --base.
register(AgentPlugin(
    name="opencode",
    prefix="/opencode",
    icon="⚙️",
    style=("blue", "OpenCode"),
    cmd=["opencode", "--model=", "run", "--format=json"],
    capabilities=["general_coding", "code_review", "code_editing"],
    preferred_task_types=["code_edit", "code_review"],
    **_OPENCODE_DEFAULTS,
))

# Variantes concretas — só diferem em nome, modelo, ícone e estilo visual.
_PLUGIN_SPECS = [
    dict(
        name="opencode-pickle",
        model="opencode/big-pickle",
        icon="🥒",
        style=("blue", "OpenCodePickle"),
    ),
]

for _spec in _PLUGIN_SPECS:
    _name = _spec["name"]
    register(AgentPlugin(
        name=_name,
        prefix=f"/{_name}",
        icon=_spec.get("icon", "⚙️"),
        style=_spec["style"],
        cmd=["opencode", f"--model={_spec['model']}", "run", "--format=json"],
        capabilities=["general_coding", "code_review", "code_editing"],
        preferred_task_types=["code_edit", "code_review"],
        **_OPENCODE_DEFAULTS,
    ))

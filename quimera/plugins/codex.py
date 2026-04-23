"""Componentes de `quimera.plugins.codex`."""
import json
import shlex
from pathlib import Path

from quimera.agent_events import SpyEvent
from quimera.plugins.base import AgentPlugin, CliConnection, register
from quimera.plugins.spy_utils import (
    format_agent_message_lines,
    format_command_output_preview,
    truncate_spy_text,
)

def _truncate_text(value: str, limit: int = 160) -> str:
    return truncate_spy_text(value, limit=limit)


def _describe_command(command: str, phase: str, exit_code: int | None = None) -> SpyEvent:
    command = (command or "").strip()
    if not command:
        return SpyEvent(kind="context", text=f"comando {phase}", transient=True)

    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()

    summary = _truncate_text(command)
    if phase == "concluído":
        if exit_code == 0 or exit_code is None:
            return SpyEvent(kind="tool", text=f"✓ {summary}")
        return SpyEvent(kind="tool", text=f"✗ {summary} (exit {exit_code})")
    return SpyEvent(kind="tool", text=f"$ {summary}")


def _describe_file_change(item: dict, phase: str) -> SpyEvent:
    target = item.get("path") or item.get("file_path") or item.get("target") or ""
    subject = target or "arquivo"
    if phase == "concluído":
        return SpyEvent(kind="tool", text=f"✓ editar {subject}")
    return SpyEvent(kind="tool", text=f"editar {subject}")


def _format_codex_spy_event(line: str) -> list[SpyEvent]:
    """Resume eventos JSONL do Codex em mensagens curtas para o modo spy."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []

    etype = event.get("type")
    if etype in {"turn.started", "session.started"}:
        return [SpyEvent(kind="context", text="iniciando execução", transient=True)]
    if etype == "turn.completed":
        return [SpyEvent(kind="context", text="execução concluída", transient=True)]

    if etype not in {"item.started", "item.completed"}:
        return []

    item = event.get("item", {})
    itype = item.get("type")
    if not itype:
        return []

    phase = "iniciado" if etype == "item.started" else "concluído"
    if itype == "command_execution":
        command = item.get("command") or ""
        events = [_describe_command(command, phase, item.get("exit_code"))]
        if etype == "item.completed":
            events.extend(format_command_output_preview(command, item.get("aggregated_output") or ""))
        return events

    if itype == "reasoning":
        text = (item.get("text") or item.get("summary") or "").strip()
        if not text:
            return [SpyEvent(kind="context", text=f"raciocínio {phase}", transient=True)]
        return [SpyEvent(kind="context", text=_truncate_text(text.splitlines()[0]), transient=True)]

    if itype == "agent_message":
        text = (item.get("text") or "").strip()
        if not text:
            return []
        return format_agent_message_lines(text)

    if itype in {"file_change", "patch_application"}:
        return [_describe_file_change(item, phase)]

    if itype in {"tool_call", "function_call"}:
        name = item.get("name") or item.get("tool_name") or "ferramenta"
        return [SpyEvent(kind="tool", text=f"usando {name}")]

    return [SpyEvent(kind="context", text=f"{itype} {phase}", transient=True)]


class CodexPlugin(AgentPlugin):
    """Plugin do Codex com retomada automática da última sessão por workspace."""

    def effective_cmd(self) -> list[str]:
        """Prefere `codex exec resume --last` sem mover a lógica para fora do plugin."""
        connection = self.effective_connection()
        if isinstance(connection, CliConnection):
            cmd = list(connection.cmd)
            prompt_as_arg = connection.prompt_as_arg
        else:
            cmd = list(self.cmd)
            prompt_as_arg = self.prompt_as_arg

        if cmd[:2] != ["codex", "exec"] or (len(cmd) >= 3 and cmd[2] == "resume"):
            return cmd

        resumed = ["codex", "exec", "resume", "--last", *cmd[2:]]
        if not prompt_as_arg:
            resumed.append("-")
        return resumed


register(CodexPlugin(
    name="codex",
    prefix="/codex",
    aliases=["/code"],
    icon="🔷",
    runtime_rw_paths=[str(Path.home() / ".codex")],
    cmd=["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--json"],
    output_format="codex-json",
    prompt_as_arg=False,
    style=("blue", "Codex"),
    capabilities=["code_editing", "code_review", "test_execution", "bug_investigation", "tool_use"],
    preferred_task_types=["code_edit", "code_review", "test_execution", "bug_investigation", "general"],
    supports_tools=True,
    has_builtin_tools=True,
    tool_use_reliability="high",
    supports_code_editing=True,
    supports_long_context=True,
    base_tier=2,
    spy_stdout_formatter=_format_codex_spy_event,
    stderr_noise=frozenset({
        "Reading additional input from stdin...",
        "Reading prompt from stdin...",
    }),
))

"""Componentes de `quimera.plugins.codex`."""
import json
import shlex
from pathlib import Path

from quimera.plugins.base import AgentPlugin, register


def _truncate_text(value: str, limit: int = 160) -> str:
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _describe_command(command: str, phase: str, exit_code: int | None = None) -> str:
    command = (command or "").strip()
    if not command:
        return f"contexto: comando {phase}"

    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()

    head = parts[0] if parts else command
    category = "executando comando"
    if head in {"rg", "grep", "find", "fd", "ls", "tree", "sed", "cat", "head", "tail"}:
        category = "inspecionando arquivos"
    elif head in {"pytest", "tox", "nox"}:
        category = "rodando testes"
    elif head == "git":
        git_subcommand = parts[1] if len(parts) > 1 else ""
        if git_subcommand in {"status", "diff", "show", "log"}:
            category = "checando repositório"
        else:
            category = "executando git"
    elif head == "python":
        if any(part == "pytest" for part in parts[1:]):
            category = "rodando testes"
        elif "compileall" in parts:
            category = "validando sintaxe"
        else:
            category = "executando python"
    elif head in {"bash", "sh"}:
        category = "executando script"

    summary = _truncate_text(command)
    if phase == "concluído":
        if exit_code == 0 or exit_code is None:
            return f"ferramenta: ✓ {summary}"
        return f"ferramenta: ✗ {summary} (exit {exit_code})"
    return f"ferramenta: $ {summary}"


def _describe_file_change(item: dict, phase: str) -> str:
    target = item.get("path") or item.get("file_path") or item.get("target") or ""
    subject = target or "arquivo"
    if phase == "concluído":
        return f"ferramenta: ✓ editar {subject}"
    return f"ferramenta: editar {subject}"


def _format_agent_message_lines(text: str) -> list[str]:
    """Quebra mensagens do agente em eventos curtos e preserva marcadores simples."""
    messages: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower() == "clear":
            messages.append("clear")
            continue
        messages.append(f"resposta: {_truncate_text(line)}")
    return messages


def _format_codex_spy_event(line: str) -> list[str]:
    """Resume eventos JSONL do Codex em mensagens curtas para o modo spy."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []

    etype = event.get("type")
    if etype in {"turn.started", "session.started"}:
        return ["contexto: iniciando execução"]
    if etype == "turn.completed":
        return ["contexto: execução concluída"]

    if etype not in {"item.started", "item.completed"}:
        return []

    item = event.get("item", {})
    itype = item.get("type")
    if not itype:
        return []

    phase = "iniciado" if etype == "item.started" else "concluído"
    if itype == "command_execution":
        message = _describe_command(item.get("command") or "", phase, item.get("exit_code"))
        return [message] if message else []

    if itype == "reasoning":
        text = (item.get("text") or item.get("summary") or "").strip()
        if not text:
            return [f"contexto: raciocínio {phase}"]
        return [f"contexto: {_truncate_text(text.splitlines()[0])}"]

    if itype == "agent_message":
        text = (item.get("text") or "").strip()
        if not text:
            return []
        return _format_agent_message_lines(text)

    if itype in {"file_change", "patch_application"}:
        return [_describe_file_change(item, phase)]

    if itype in {"tool_call", "function_call"}:
        name = item.get("name") or item.get("tool_name") or "ferramenta"
        return [f"ferramenta: usando {name}"]

    return [f"contexto: {itype} {phase}"]


plugin = AgentPlugin(
    name="codex",
    prefix="/codex",
    aliases=["/code"],
    icon="🔷",
    runtime_rw_paths=[str(Path.home() / ".codex")],
    cmd=["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--json"],
    output_format="codex-json",
    # O `codex exec` tenta ler stdin adicional quando recebe prompt por argv
    # e detecta stdin redirecionado. No Quimera, usar stdin como canal único
    # evita esse modo ambíguo e garante EOF explícito após o prompt.
    prompt_as_arg=False,
    style=("blue", "Codex"),
    capabilities=["code_editing", "code_review", "test_execution", "bug_investigation", "tool_use"],
    preferred_task_types=["code_edit", "code_review", "test_execution", "bug_investigation", "general"],
    avoid_task_types=[],
    supports_tools=True,
    tool_use_reliability="high",
    supports_code_editing=True,
    spy_stdout_formatter=_format_codex_spy_event,
    stderr_noise=frozenset({
        "Reading additional input from stdin...",
        "Reading prompt from stdin...",
    }),
    supports_long_context=True, base_tier=2,
)
register(plugin)

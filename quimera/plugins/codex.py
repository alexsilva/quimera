"""Componentes de `quimera.plugins.codex`."""
import json
import re
import shlex
from pathlib import Path

from quimera.agent_events import SpyEvent
from quimera.plugins.base import AgentPlugin, CliConnection, register
from quimera.plugins.spy_utils import (
    format_agent_message_lines,
    format_command_output_preview,
    truncate_spy_text,
)

_CODEX_STDERR_NOISE_PATTERNS = (
    r"\bOrphan function call output for call id:\s*call_[A-Za-z0-9]+\b",
)


def _extract_model_from_codex_config(config_path: Path | None = None) -> str | None:
    """Lê o modelo padrão do Codex em ~/.codex/config.toml."""
    path = config_path or (Path.home() / ".codex" / "config.toml")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None

    # O `model = "..."` relevante é o da seção global (antes do primeiro [table]).
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        match = re.match(r'^model\s*=\s*["\']([^"\']+)["\'](?:\s+#.*)?$', line)
        if match:
            model = (match.group(1) or "").strip()
            if model:
                return model
    return None

def _truncate_text(value: str, limit: int = 160) -> str:
    return truncate_spy_text(value, limit=limit)


def _tool_call_id(item: dict) -> str | None:
    return (
        item.get("id")
        or item.get("tool_call_id")
        or item.get("call_id")
        or item.get("invocation_id")
    )


def _describe_command(command: str, phase: str, exit_code: int | None = None, item: dict | None = None) -> SpyEvent:
    command = (command or "").strip()
    item = item or {}
    data_base = {
        "tool": "exec_command",
        "tool_call_id": _tool_call_id(item),
        "input": {"cmd": command},
    }
    if not command:
        return SpyEvent(kind="context", text=f"comando {phase}", transient=True, data=data_base)

    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()

    summary = _truncate_text(command)
    if phase == "concluído":
        if exit_code == 0 or exit_code is None:
            return SpyEvent(
                kind="tool",
                text=f"✓ {summary}",
                data={**data_base, "operation": "end", "status": "ok", "output_meta": {"exit_code": exit_code}},
            )
        return SpyEvent(
            kind="tool",
            text=f"✗ {summary} (exit {exit_code})",
            data={
                **data_base,
                "operation": "end",
                "status": "error",
                "output_meta": {"exit_code": exit_code},
                "error": {"type": "CommandExitCode", "message": f"exit {exit_code}"},
            },
        )
    return SpyEvent(kind="tool", text=f"$ {summary}", data={**data_base, "operation": "start", "status": "running"})


def _describe_file_change(item: dict, phase: str) -> SpyEvent:
    target = item.get("path") or item.get("file_path") or item.get("target") or ""
    subject = target or "arquivo"
    data_base = {
        "tool": "apply_patch",
        "tool_call_id": _tool_call_id(item),
        "input": {"path": target},
    }
    if phase == "concluído":
        return SpyEvent(
            kind="tool",
            text=f"✓ editar {subject}",
            data={**data_base, "operation": "end", "status": "ok"},
        )
    return SpyEvent(
        kind="tool",
        text=f"editar {subject}",
        data={**data_base, "operation": "start", "status": "running"},
    )


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
        tool_id = _tool_call_id(item)
        events = [_describe_command(command, phase, item.get("exit_code"), item=item)]
        if etype == "item.completed":
            events.extend(
                format_command_output_preview(
                    command,
                    item.get("aggregated_output") or "",
                    tool_call_id=tool_id,
                )
            )
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
        return [
            SpyEvent(
                kind="tool",
                text=f"usando {name}",
                data={
                    "tool": name,
                    "tool_call_id": _tool_call_id(item),
                    "operation": "start",
                    "status": "running",
                },
            )
        ]

    return [SpyEvent(kind="context", text=f"{itype} {phase}", transient=True)]


class CodexPlugin(AgentPlugin):
    """Plugin do Codex com retomada automática da última sessão por workspace."""

    def mcp_server_args(self, socket_path: str) -> list[str]:
        """Retorna overrides de config para registrar MCP via stdio no Codex."""
        proxy_cmd: list[str] = ["-m", "quimera.runtime.mcp", "--connect-socket", socket_path]
        proxy_cmd += self._build_token_args()
        args_toml = json.dumps(proxy_cmd, ensure_ascii=False)
        return [
            "-c",
            'mcp_servers.quimera.command="python"',
            "-c",
            f"mcp_servers.quimera.args={args_toml}",
        ]

    def _with_mcp_server_args(self, cmd: list[str]) -> list[str]:
        """Anexa configuração MCP sem duplicar override já existente."""
        base_cmd = list(cmd)
        socket_path = (self._mcp_socket_path or "").strip()
        if not socket_path:
            return base_cmd
        if any("mcp_servers.quimera." in str(part) for part in base_cmd):
            return base_cmd
        mcp_args = self.mcp_server_args(socket_path)
        if base_cmd and base_cmd[-1] == "-":
            return [*base_cmd[:-1], *mcp_args, base_cmd[-1]]
        return [*base_cmd, *mcp_args]

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
            return self._with_mcp_server_args(cmd)

        resumed = ["codex", "exec", "resume", "--last", *cmd[2:]]
        if not prompt_as_arg:
            resumed.append("-")
        return self._with_mcp_server_args(resumed)

    def resolve_runtime_model(self, *, cwd: str | None = None) -> str | None:
        cli_model = super().resolve_runtime_model(cwd=cwd)
        if cli_model:
            return cli_model
        return _extract_model_from_codex_config()


register(CodexPlugin(
    name="codex",
    prefix="/codex",
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
    stderr_noise_patterns=_CODEX_STDERR_NOISE_PATTERNS,
))

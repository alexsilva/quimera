"""Componentes de `quimera.profiles.codex`."""
import json
from pathlib import Path

from quimera.agent_events import SpyEvent
from quimera.domain.tool_line import STATUS_ERROR, STATUS_OK, build_tool_line, normalize_status
from quimera.profiles.base import ExecutionProfile, CliConnection, register
from quimera.profiles.spy_utils import (
    format_agent_message_lines,
    format_command_output_preview,
    normalize_spy_text,
    truncate_spy_text,
)
from quimera.runtime.config import DEFAULT_MCP_CLIENT_TOOL_TIMEOUT_SECONDS, MCP_PROXY_ENV_VARS

_CODEX_STDERR_NOISE_PATTERNS = (
    r"\bOrphan function call output for call id:\s*call_[A-Za-z0-9]+\b",
)



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
    tool_call_id = _tool_call_id(item)
    data_base = {
        "tool": "exec_command",
        "tool_call_id": tool_call_id,
        "input": {"cmd": command},
    }
    if not command:
        return SpyEvent(kind="context", text=f"comando {phase}", transient=True, data=data_base)

    if phase == "concluído":
        failed = exit_code is not None and exit_code != 0
        line = build_tool_line(
            "exec_command",
            {"cmd": command},
            status=STATUS_ERROR if failed else STATUS_OK,
            tool_call_id=tool_call_id,
            exit_code=exit_code,
            tail=item.get("aggregated_output"),
        )
        data = {
            **data_base,
            "operation": "end",
            "status": line.status,
            "output_meta": {"exit_code": exit_code},
            "line": line.to_payload(),
        }
        if failed:
            data["error"] = {"type": "CommandExitCode", "message": f"exit {exit_code}"}
        return SpyEvent(kind="tool", text=line.to_text(), data=data)
    line = build_tool_line("exec_command", {"cmd": command}, tool_call_id=tool_call_id)
    return SpyEvent(
        kind="tool",
        text=line.to_text(),
        data={**data_base, "operation": "start", "status": "running", "line": line.to_payload()},
    )


def _describe_file_change(item: dict, phase: str) -> SpyEvent:
    target = item.get("path") or item.get("file_path") or item.get("target") or ""
    tool_call_id = _tool_call_id(item)
    data_base = {
        "tool": "apply_patch",
        "tool_call_id": tool_call_id,
        "input": {"path": target},
    }
    done = phase == "concluído"
    line = build_tool_line(
        "apply_patch",
        {"path": target},
        status=STATUS_OK if done else "running",
        tool_call_id=tool_call_id,
    )
    return SpyEvent(
        kind="tool",
        text=line.to_text(),
        data={
            **data_base,
            "operation": "end" if done else "start",
            "status": line.status,
            "line": line.to_payload(),
        },
    )


def _tool_arguments(item: dict) -> dict:
    """Argumentos da tool como dict; aceita string JSON."""
    raw = item.get("arguments")
    if raw is None:
        raw = item.get("input") if item.get("input") is not None else item.get("args")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _describe_tool_call(item: dict, phase: str) -> SpyEvent:
    """Chamada de tool genérica (``tool_call``/``function_call``/``mcp_tool_call``)."""
    name = item.get("name") or item.get("tool_name") or item.get("tool") or "ferramenta"
    server = str(item.get("server") or "")
    if item.get("type") == "mcp_tool_call" and server and server != "quimera":
        name = f"{server}__{name}"
    arguments = _tool_arguments(item)
    tool_call_id = _tool_call_id(item)
    data_base = {"tool": name, "tool_call_id": tool_call_id, "input": arguments}
    if phase != "concluído":
        line = build_tool_line(name, arguments, tool_call_id=tool_call_id)
        return SpyEvent(
            kind="tool",
            text=line.to_text(),
            data={**data_base, "operation": "start", "status": "running", "line": line.to_payload()},
        )
    error = item.get("error")
    raw_status = normalize_status(item.get("status"))
    status = STATUS_ERROR if error or raw_status == STATUS_ERROR else STATUS_OK
    line = build_tool_line(name, arguments, status=status, tool_call_id=tool_call_id, error=error)
    data = {**data_base, "operation": "end", "status": status, "line": line.to_payload()}
    if error:
        message = error.get("message") if isinstance(error, dict) else error
        data["error"] = {"type": "ToolError", "message": str(message)}
    return SpyEvent(kind="tool", text=line.to_text(), data=data)


def _format_codex_spy_event(line: str) -> list[SpyEvent]:
    """Resume eventos JSONL do Codex em mensagens curtas para o modo spy."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []

    etype = event.get("type")
    if etype in {"turn.started", "session.started", "turn.completed"}:
        return []

    if etype not in {"item.started", "item.completed"}:
        return []

    item = event.get("item", {})
    itype = item.get("type")
    if not itype:
        return []
    if itype == "error":
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
        return [SpyEvent(kind="context", text=normalize_spy_text(text), transient=True)]

    if itype == "agent_message":
        text = (item.get("text") or "").strip()
        if not text:
            return []
        return format_agent_message_lines(text)

    if itype in {"file_change", "patch_application"}:
        return [_describe_file_change(item, phase)]

    if itype in {"tool_call", "function_call", "mcp_tool_call"}:
        return [_describe_tool_call(item, phase)]

    return [SpyEvent(kind="context", text=f"{itype} {phase}", transient=True)]


class CodexProfile(ExecutionProfile):
    """Profile do Codex com uma sessão própria por chamada do Quimera."""

    def configure_with_model(self, model_id: str) -> CliConnection:
        """Retorna conexão CLI do Codex com --model aplicado."""
        normalized = (model_id or "").strip()
        if not normalized:
            raise ValueError("model_id não pode ser vazio.")
        connection = self.effective_connection()
        if not isinstance(connection, CliConnection):
            raise ValueError(f"Profile '{self.name}' não usa driver CLI.")

        cmd = list(connection.cmd)
        for idx, arg in enumerate(cmd):
            if arg.startswith("--model="):
                cmd[idx] = f"--model={normalized}"
                return CliConnection(cmd=cmd, prompt_as_arg=connection.prompt_as_arg, output_format=connection.output_format)
            if arg in {"--model", "-m"} and idx + 1 < len(cmd):
                cmd[idx + 1] = normalized
                return CliConnection(cmd=cmd, prompt_as_arg=connection.prompt_as_arg, output_format=connection.output_format)

        if cmd[:2] == ["codex", "exec"]:
            cmd = ["codex", "exec", "--model", normalized, *cmd[2:]]
        elif cmd and Path(cmd[0]).name == "codex":
            cmd = [cmd[0], "--model", normalized, *cmd[1:]]
        else:
            cmd = ["codex", "exec", "--model", normalized, *cmd]
        return CliConnection(cmd=cmd, prompt_as_arg=connection.prompt_as_arg, output_format=connection.output_format)

    def mcp_server_args(self, socket_path: str) -> list[str]:
        """Retorna overrides de config para registrar MCP via stdio no Codex."""
        proxy_cmd = self._build_mcp_proxy_command(socket_path)
        command_toml = json.dumps(proxy_cmd[0], ensure_ascii=False)
        args_toml = json.dumps(proxy_cmd[1:], ensure_ascii=False)
        return [
            "-c",
            f"mcp_servers.quimera.command={command_toml}",
            "-c",
            f"mcp_servers.quimera.args={args_toml}",
            "-c",
            f"mcp_servers.quimera.tool_timeout_sec={DEFAULT_MCP_CLIENT_TOOL_TIMEOUT_SECONDS}",
            *self._mcp_env_vars_args(),
        ]

    @staticmethod
    def _mcp_env_vars_args() -> list[str]:
        """Repassa ao proxy MCP o ambiente do prelude.

        O Codex entrega aos servidores MCP só um ambiente mínimo; sem
        ``env_vars`` o run_id não chega e as tools da execução caem na
        execução errada quando o mesmo agente roda duas vezes.
        """
        return ["-c", f"mcp_servers.quimera.env_vars={json.dumps(list(MCP_PROXY_ENV_VARS))}"]

    def mcp_http_server_args(self, url: str) -> list[str]:
        """Retorna uma lista vazia de argumentos MCP HTTP para o Codex CLI."""
        _ = url
        return []

    def _with_mcp_server_args(self, cmd: list[str]) -> list[str]:
        """Anexa configuração MCP sem duplicar override já existente."""
        base_cmd = list(cmd)
        socket_path = (self._mcp_socket_path or "").strip()
        if not socket_path:
            return base_cmd
        if any("mcp_servers.quimera." in str(part) for part in base_cmd):
            timeout_key = "mcp_servers.quimera.tool_timeout_sec"
            missing_args: list[str] = []
            if not any(timeout_key in str(part) for part in base_cmd):
                missing_args += [
                    "-c",
                    f"{timeout_key}={DEFAULT_MCP_CLIENT_TOOL_TIMEOUT_SECONDS}",
                ]
            if not any("mcp_servers.quimera.env_vars" in str(part) for part in base_cmd):
                missing_args += self._mcp_env_vars_args()
            if not missing_args:
                return base_cmd
            if base_cmd and base_cmd[-1] == "-":
                return [*base_cmd[:-1], *missing_args, base_cmd[-1]]
            return [*base_cmd, *missing_args]
        mcp_args = self.mcp_server_args(socket_path)
        if base_cmd and base_cmd[-1] == "-":
            return [*base_cmd[:-1], *mcp_args, base_cmd[-1]]
        return [*base_cmd, *mcp_args]

    def effective_cmd(self) -> list[str]:
        """Inicia uma sessão nova, salvo retomada configurada explicitamente.

        O Quimera já envia o contexto no prompt. Retomar a última sessão do
        workspace mistura agentes e disputa seu writer com chamadas ativas.
        """
        connection = self.effective_connection()
        if isinstance(connection, CliConnection):
            cmd = list(connection.cmd)
            prompt_as_arg = connection.prompt_as_arg
        else:
            cmd = list(self.cmd)
            prompt_as_arg = self.prompt_as_arg

        if cmd[:2] != ["codex", "exec"] or (len(cmd) >= 3 and cmd[2] == "resume"):
            return self._with_mcp_server_args(cmd)

        if not prompt_as_arg and cmd[-1] != "-":
            cmd.append("-")
        return self._with_mcp_server_args(cmd)



register(CodexProfile(
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

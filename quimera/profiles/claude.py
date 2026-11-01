"""Componentes de `quimera.profiles.claude`."""
import json
from pathlib import Path

from quimera.agent_events import SpyEvent
from quimera.profiles.base import CliConnection, ExecutionProfile, register
from quimera.domain.tool_line import build_tool_line
from quimera.profiles.spy_utils import format_agent_message_lines, truncate_spy_text


def _claude_runtime_rw_paths() -> list[str]:
    """Retorna paths de estado do Claude que precisam permanecer graváveis."""
    home = Path.home()
    return [
        str(home / ".claude"),
        str(home / ".claude.json"),
        str(home / ".local" / "share" / "claude"),
    ]


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

    if etype == "user":
        return _format_claude_tool_results(event)

    if etype != "assistant":
        return []

    messages: list[SpyEvent] = []
    content = event.get("message", {}).get("content", [])
    for block in content:
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                messages.extend(format_agent_message_lines(text))
        elif btype == "tool_use":
            tool_name = block.get("name") or "ferramenta"
            inp = block.get("input") or {}
            tool_call_id = block.get("id")
            tool_line = build_tool_line(tool_name, inp, tool_call_id=tool_call_id)
            messages.append(
                SpyEvent(
                    kind="tool",
                    text=tool_line.to_text(),
                    transient=True,
                    data={
                        "tool": tool_name,
                        "operation": "start",
                        "status": "running",
                        "input": inp,
                        "tool_call_id": tool_call_id,
                        "line": tool_line.to_payload(),
                    },
                )
            )
    return messages


def _tool_result_text(content) -> str:
    """Texto de um bloco ``tool_result``: string ou lista de blocos de texto."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _format_claude_tool_results(event: dict) -> list[SpyEvent]:
    """Fecha as tools nativas: o Claude devolve o resultado em mensagens ``user``."""
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    messages: list[SpyEvent] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_call_id = block.get("tool_use_id")
        if not tool_call_id:
            continue
        failed = bool(block.get("is_error"))
        lines = [ln for ln in _tool_result_text(block.get("content")).splitlines() if ln.strip()]
        tail = _truncate_text(lines[-1]) if lines else ""
        messages.append(
            SpyEvent(
                kind="tool",
                text="✗" if failed else "✓",
                transient=True,
                data={
                    "tool": "",
                    "operation": "end",
                    "status": "error" if failed else "ok",
                    "tool_call_id": tool_call_id,
                    "tail": tail,
                },
            )
        )
    return messages


class ClaudeProfile(ExecutionProfile):
    def format_stdin_input(self, prompt) -> str:
        """Formata o prompt conforme o protocolo da conexão efetiva.

        Conexões antigas usam entrada textual, mesmo com saída stream-json.
        Enviar um envelope JSON nesse caso faz o Claude recebê-lo como parte
        do contexto, em vez de interpretar seu conteúdo como o prompt.
        """
        connection = self.effective_connection()
        cmd = (
            connection.cmd
            if isinstance(connection, CliConnection)
            else self.cmd
        )
        input_format = "text"
        for index, arg in enumerate(cmd):
            if arg == "--":
                break
            if arg.startswith("--input-format="):
                input_format = arg.split("=", 1)[1]
            elif arg == "--input-format" and index + 1 < len(cmd):
                input_format = cmd[index + 1]

        if input_format != "stream-json":
            return str(prompt)
        event = {
            "type": "user",
            "message": {"role": "user", "content": str(prompt)},
        }
        return json.dumps(event, ensure_ascii=False) + "\n"

    def configure_with_model(self, model_id: str) -> CliConnection:
        """Retorna conexão CLI do Claude com --model aplicado."""
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
            if arg == "--model" and idx + 1 < len(cmd):
                cmd[idx + 1] = normalized
                return CliConnection(cmd=cmd, prompt_as_arg=connection.prompt_as_arg, output_format=connection.output_format)

        if cmd:
            cmd = [cmd[0], "--model", normalized, *cmd[1:]]
        else:
            cmd = ["claude", "--model", normalized]
        return CliConnection(cmd=cmd, prompt_as_arg=connection.prompt_as_arg, output_format=connection.output_format)

    def mcp_server_args(self, socket_path: str) -> list[str]:
        """Retorna flags para conectar o Claude ao MCP local do Quimera."""
        proxy_cmd = self._build_mcp_proxy_command(socket_path)
        config = {
            "mcpServers": {
                "quimera": {
                    "type": "stdio",
                    "command": proxy_cmd[0],
                    "args": proxy_cmd[1:],
                }
            }
        }
        return ["--mcp-config", json.dumps(config)]

    def mcp_http_server_args(self, url: str) -> list[str]:
        """Retorna uma lista vazia de argumentos MCP HTTP para o Claude CLI."""
        _ = url
        return []



profile = ClaudeProfile(
    name="claude",
    prefix="/claude",
    icon="🔮",
    runtime_rw_paths=_claude_runtime_rw_paths(),
    cmd=[
        "claude",
        "--permission-mode=bypassPermissions",
        "--output-format=stream-json",
        "--verbose",
        "--print",
        "--input-format=stream-json",
    ],
    prompt_as_arg=False,
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
register(profile)

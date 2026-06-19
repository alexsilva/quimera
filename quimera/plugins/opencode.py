"""Componentes de `quimera.plugins.opencode`."""
import json
from pathlib import Path
from typing import Optional

from quimera.agent_events import SpyEvent
from quimera.plugins.base import AgentPlugin, CliConnection, Connection, register
from quimera.plugins.spy_utils import describe_tool_input, format_agent_message_lines


def _format_opencode_spy_event(line: str) -> list[SpyEvent]:
    """Resume eventos JSON do OpenCode em mensagens curtas para o modo SUMMARY."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []

    etype = event.get("type")
    part = event.get("part", {}) or {}
    ptype = part.get("type")

    # O OpenCode pode emitir múltiplos step_start/step_finish durante uma única execução
    # (por subetapas). Exibir isso no spy gera ruído repetitivo de "iniciando/concluída".
    # O início/fim global já é coberto pelo pipeline comum do AgentClient.
    if etype == "step_start" or ptype == "step-start":
        return []
    if etype == "step_finish" or ptype == "step-finish":
        return []

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
        inp = part.get("input") or part.get("args") or event.get("input") or event.get("args") or {}
        detail = describe_tool_input(tool_name, inp)
        text = detail if detail else f"usando {tool_name}"
        return [SpyEvent(kind="tool", text=text, transient=True)]

    return []


_OPENCODE_RW_PATHS = [
    str(Path.home() / ".local" / "share" / "opencode"),
    str(Path.home() / ".local" / "state" / "opencode")
]

# Padrões de ruído de stderr específicos do runtime bun (linha com número variável).
# O path real é "/$bunfs/..." — o "/" antes de "$bunfs" estava ausente nos padrões originais.
_BUN_STDERR_NOISE_PATTERNS = (
    r"^\s*at .+\(/?bunfs/",   # stack frame com função: "  at fn (/$bunfs/...)"
    r"^\s*at /?bunfs/",       # frame direto sem função: "  at /$bunfs/..."
)

class OpenCodePlugin(AgentPlugin):
    """Plugin do OpenCode com suporte a MCP via OPENCODE_CONFIG_CONTENT."""

    def mcp_server_args(self, socket_path: str) -> list[str]:
        """OpenCode não aceita MCP via CLI args."""
        return []

    def _mcp_config_content(self, socket_path: str) -> Optional[str]:
        """Gera JSON de config para ativar MCP do Quimera."""
        if not (socket_path or "").strip():
            return None
        proxy_cmd: list[str] = [
            "python", "-m", "quimera.runtime.mcp",
            "--connect-socket", socket_path,
        ]
        proxy_cmd += self._build_token_args()
        config = {
            "mcp": {
                "quimera": {
                    "type": "local",
                    "command": proxy_cmd,
                    "enabled": True,
                }
            }
        }
        return json.dumps(config)

    def env_for_cli(self) -> dict:
        """Retorna variáveis de ambiente do OpenCode para conectar ao MCP socket."""
        socket_path = (self._mcp_socket_path or "").strip()
        if not socket_path:
            return {}
        config_content = self._mcp_config_content(socket_path)
        if not config_content:
            return {}
        return {"OPENCODE_CONFIG_CONTENT": config_content}


register(OpenCodePlugin(
    name="opencode",
    prefix="/opencode",
    icon="⚙️",
    style=("blue", "OpenCode"),
    cmd=["opencode", "--model=", "run", "--format=json", "--thinking"],
    capabilities=["general_coding", "code_review", "code_editing"],
    preferred_task_types=["code_edit", "code_review"],
    runtime_rw_paths=_OPENCODE_RW_PATHS,
    output_format="opencode-json",
    spy_stdout_formatter=_format_opencode_spy_event,
    supports_tools=True,
    has_builtin_tools=True,
    supports_code_editing=True,
    supports_long_context=False,
    supports_warm_pool=False,
    base_tier=2,
    stderr_noise_patterns=_BUN_STDERR_NOISE_PATTERNS,
))

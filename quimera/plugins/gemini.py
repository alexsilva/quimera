"""Componentes de `quimera.plugins.gemini`."""
import json

from quimera.plugins.base import AgentPlugin, register


class GeminiPlugin(AgentPlugin):
    """Plugin do Gemini com suporte a MCP."""

    def mcp_server_args(self, socket_path: str) -> list[str]:
        """Retorna overrides de config para registrar MCP via stdio no agy."""
        proxy_cmd: list[str] = ["-m", "quimera.runtime.mcp_server", "--connect-socket", socket_path]
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


plugin = GeminiPlugin(
    name="gemini",
    prefix="/gemini",
    icon="🧭",
    cmd=["agy", "--dangerously-skip-permissions", "-p"],
    style=("cyan", "Gemini"),
    prompt_as_arg=True,
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

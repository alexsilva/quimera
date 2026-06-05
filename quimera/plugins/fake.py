"""Plugins fake embutidos para testes interativos locais."""
import sys

from quimera.plugins.base import AgentPlugin, register


class FakeOpenAIMCPCliPlugin(AgentPlugin):
    """Plugin CLI fake que recebe MCP socket/token via ambiente."""

    def env_for_cli(self) -> dict:
        env = {
            "QUIMERA_FAKE_OPENAI_BASE_URL": self.effective_base_url() or "http://127.0.0.1:8765/v1",
            "QUIMERA_FAKE_OPENAI_MODEL": self.effective_model() or "quimera-fake-tools",
        }
        if self._mcp_socket_path:
            env["QUIMERA_FAKE_MCP_SOCKET"] = self._mcp_socket_path
        if self._mcp_token:
            env["QUIMERA_FAKE_MCP_TOKEN"] = self._mcp_token
        return env


class FakeMCPHandoffCliPlugin(AgentPlugin):
    """Plugin CLI fake que delega para outro agente via call_agent no MCP."""

    def env_for_cli(self) -> dict:
        env = {"QUIMERA_FAKE_HANDOFF_TARGET": "fake-openai"}
        if self._mcp_socket_path:
            env["QUIMERA_FAKE_MCP_SOCKET"] = self._mcp_socket_path
        if self._mcp_token:
            env["QUIMERA_FAKE_MCP_TOKEN"] = self._mcp_token
        return env


def register_fake_plugins(registry=None) -> tuple[str, ...]:
    """Registra plugins fake no registry informado ou no registry global."""
    target_register = registry.register if registry is not None else register

    target_register(AgentPlugin(
        name="fake-cli",
        prefix="/fake-cli",
        icon="🧪",
        style=("magenta", "Fake CLI"),
        cmd=[sys.executable, "-m", "quimera.devtools.fake_agents", "cli", "--role", "tester"],
        capabilities=["documentation", "code_review", "test_execution"],
        preferred_task_types=["documentation", "code_review", "test_execution"],
        supports_tools=False,
        has_builtin_tools=False,
        supports_code_editing=False,
        supports_task_execution=True,
        supports_warm_pool=False,
        base_tier=1,
    ))

    target_register(FakeMCPHandoffCliPlugin(
        name="fake-cli-handoff",
        prefix="/fake-cli-handoff",
        icon="🧪",
        style=("yellow", "Fake CLI Handoff"),
        cmd=[sys.executable, "-m", "quimera.devtools.fake_agents", "mcp-handoff-cli", "--target-agent", "fake-openai"],
        capabilities=["test_execution", "tool_calling", "mcp", "agent_delegation"],
        preferred_task_types=["test_execution", "bug_investigation", "code_review"],
        supports_tools=True,
        has_builtin_tools=True,
        tool_use_reliability="high",
        supports_code_editing=False,
        supports_task_execution=True,
        supports_warm_pool=False,
        base_tier=1,
    ))

    target_register(AgentPlugin(
        name="fake-openai",
        prefix="/fake-openai",
        icon="🧰",
        style=("green", "Fake OpenAI"),
        driver="openai_compat",
        model="quimera-fake-tools",
        base_url="http://127.0.0.1:8765/v1",
        api_key_env="QUIMERA_FAKE_API_KEY",
        capabilities=["general_coding", "code_review", "test_execution", "tool_calling"],
        preferred_task_types=["test_execution", "bug_investigation", "code_review"],
        supports_tools=True,
        has_builtin_tools=True,
        tool_use_reliability="high",
        supports_code_editing=False,
        supports_task_execution=True,
        supports_warm_pool=False,
        base_tier=1,
    ))

    target_register(FakeOpenAIMCPCliPlugin(
        name="fake-openai-mcp-cli",
        prefix="/fake-openai-mcp-cli",
        icon="🔌",
        style=("cyan", "Fake OpenAI MCP CLI"),
        cmd=[sys.executable, "-m", "quimera.devtools.fake_agents", "openai-mcp-cli"],
        capabilities=["test_execution", "tool_calling", "mcp", "openai_compat"],
        preferred_task_types=["test_execution", "bug_investigation", "code_review"],
        supports_tools=True,
        has_builtin_tools=True,
        tool_use_reliability="high",
        supports_code_editing=False,
        supports_task_execution=True,
        supports_warm_pool=False,
        base_tier=1,
        model="quimera-fake-tools",
        base_url="http://127.0.0.1:8765/v1",
    ))

    return ("fake-cli", "fake-cli-handoff", "fake-openai", "fake-openai-mcp-cli")

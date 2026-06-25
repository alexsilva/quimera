"""Profiles fake embutidos para testes interativos locais."""
import sys

from quimera.profiles.base import ExecutionProfile, register


class FakeOpenAIMCPCliProfile(ExecutionProfile):
    """Profile CLI fake que recebe MCP socket/token via ambiente."""

    def env_for_cli(self) -> dict:
        from quimera import profiles

        fake_openai = profiles.get("fake-openai")
        base_url = (fake_openai.effective_base_url() if fake_openai is not None else None) or self.effective_base_url()
        model = (fake_openai.effective_model() if fake_openai is not None else None) or self.effective_model()
        env = {
            "QUIMERA_FAKE_OPENAI_BASE_URL": base_url or "http://127.0.0.1:8765/v1",
            "QUIMERA_FAKE_OPENAI_MODEL": model or "quimera-fake-tools",
        }
        if self._mcp_socket_path:
            env["QUIMERA_FAKE_MCP_SOCKET"] = self._mcp_socket_path
        if self._mcp_token:
            env["QUIMERA_FAKE_MCP_TOKEN"] = self._mcp_token
        return env


class FakeMCPDelegateCliProfile(ExecutionProfile):
    """Profile CLI fake que delega para outro agente via delegate no MCP."""

    def env_for_cli(self) -> dict:
        env = {"QUIMERA_FAKE_DELEGATE_TARGET": "fake-openai"}
        if self._mcp_socket_path:
            env["QUIMERA_FAKE_MCP_SOCKET"] = self._mcp_socket_path
        if self._mcp_token:
            env["QUIMERA_FAKE_MCP_TOKEN"] = self._mcp_token
        return env


def register_fake_profiles(registry=None) -> tuple[str, ...]:
    """Registra profiles fake no registry informado ou no registry global."""
    target_register = registry.register if registry is not None else register

    target_register(ExecutionProfile(
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

    target_register(FakeMCPDelegateCliProfile(
        name="fake-cli-delegate",
        prefix="/fake-cli-delegate",
        icon="🧪",
        style=("yellow", "Fake CLI Handoff"),
        cmd=[sys.executable, "-m", "quimera.devtools.fake_agents", "mcp-delegate-cli", "--target-agent", "fake-openai"],
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

    target_register(ExecutionProfile(
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

    target_register(FakeOpenAIMCPCliProfile(
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

    return ("fake-cli", "fake-cli-delegate", "fake-openai", "fake-openai-mcp-cli")

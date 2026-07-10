from __future__ import annotations

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.drivers.tool_schemas import get_bridge_schemas, resolve_tool_schemas, set_bridge_schemas
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.mcp.client import (
    HttpMCPTransport,
    MCPClientBridge,
    StdioMCPTransport,
    merge_specs_by_name,
    parse_mcp_client_spec,
    start_mcp_clients,
)
from quimera.runtime.models import ToolCall, ToolResult
from quimera.runtime.tools.mcp_clients import set_bridge


class FakeSession:
    transport_type = "fake"

    def __init__(self) -> None:
        self.calls = []

    def list_tools(self):
        return [
            {
                "name": "search_issue",
                "description": "Search Jira issues.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "jql": {"type": "string"},
                    },
                    "required": ["jql"],
                },
            }
        ]

    def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return {
            "content": [{"type": "text", "text": "PC-1 Example issue"}],
            "isError": False,
        }


class MultiFakeSession(FakeSession):
    def list_tools(self):
        tools = super().list_tools()
        tools.append(
            {
                "name": "transition_issue",
                "description": "Transition Jira issue.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                        "transition_id": {"type": "string"},
                    },
                    "required": ["issue_key", "transition_id"],
                },
            }
        )
        return tools

def teardown_function() -> None:
    set_bridge(None)
    set_bridge_schemas([])


def test_mcp_client_bridge_registers_external_tools_in_executor_registry(tmp_path):
    bridge = MCPClientBridge()
    session = FakeSession()
    bridge._sessions["jira"] = session
    bridge._started = True

    set_bridge(bridge)

    executor = ToolExecutor(
        config=ToolRuntimeConfig(
            workspace_root=tmp_path,
            require_approval_for_mutations=False,
        ),
        approval_handler=None,
    )

    assert "jira_search_issue" in executor.registry.names()

    result = executor.execute(
        ToolCall(name="jira_search_issue", arguments={"jql": "key = PC-1"})
    )

    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.content == "PC-1 Example issue"
    assert session.calls == [("search_issue", {"jql": "key = PC-1"})]


def test_mcp_client_bridge_schemas_are_resolved_when_registered(tmp_path):
    bridge = MCPClientBridge()
    bridge._sessions["jira"] = FakeSession()
    bridge._started = True

    set_bridge(bridge)

    executor = ToolExecutor(
        config=ToolRuntimeConfig(
            workspace_root=tmp_path,
            require_approval_for_mutations=False,
        ),
        approval_handler=None,
    )

    names = [schema["function"]["name"] for schema in resolve_tool_schemas(executor)]

    assert "jira_search_issue" in names
    assert get_bridge_schemas()[0]["function"]["name"] == "jira_search_issue"


def test_parse_atlassian_mcp_remote_stdio_command():
    name, transport = parse_mcp_client_spec(
        "atlassian=stdio:npx -y mcp-remote https://mcp.atlassian.com/v1/sse"
    )

    assert name == "atlassian"
    assert isinstance(transport, StdioMCPTransport)
    assert transport._command == [
        "npx",
        "-y",
        "mcp-remote",
        "https://mcp.atlassian.com/v1/sse",
    ]


def test_parse_http_mcp_client_accepts_simple_bearer_token():
    name, transport = parse_mcp_client_spec(
        "jira=https://rovo.example.test/mcp",
        {"jira": {"MCP_TOKEN": "token-abc"}},
    )

    assert name == "jira"
    assert isinstance(transport, HttpMCPTransport)
    assert transport._token == "token-abc"


def test_merge_mcp_client_specs_adds_new_names_and_replaces_existing_names():
    existing = [
        "jira=stdio:jira-v1",
        "github=stdio:github-v1",
    ]

    merged = merge_specs_by_name(
        existing,
        [
            "github=stdio:github-v2",
            "notion=https://notion.example.test/mcp",
        ],
    )

    assert merged == [
        "jira=stdio:jira-v1",
        "github=stdio:github-v2",
        "notion=https://notion.example.test/mcp",
    ]
    assert existing == [
        "jira=stdio:jira-v1",
        "github=stdio:github-v1",
    ]


def test_start_mcp_clients_connects_and_persists_merged_specs(monkeypatch):
    captured = {}

    class FakeConfig:
        mcp_clients = ["jira=stdio:jira-cmd"]
        mcp_client_env = ["jira=JIRA_TOKEN=old-token"]

        def set_mcp_clients(self, specs):
            captured["persisted_specs"] = specs

        def set_mcp_client_env(self, specs):
            captured["persisted_env_specs"] = specs

    class FakeBridge:
        started = False

    def fake_build_bridge(specs, env_overrides=None):
        captured["connected_specs"] = specs
        captured["env_overrides"] = env_overrides
        return FakeBridge()

    monkeypatch.setattr(
        "quimera.runtime.mcp.client.build_bridge_from_cli",
        fake_build_bridge,
    )

    runtime = start_mcp_clients(
        cli_specs=["github=stdio:github-cmd"],
        cli_env_specs=["github=GITHUB_TOKEN=new-token"],
        config=FakeConfig(),
    )

    expected_specs = [
        "jira=stdio:jira-cmd",
        "github=stdio:github-cmd",
    ]
    expected_env_specs = [
        "jira=JIRA_TOKEN=old-token",
        "github=GITHUB_TOKEN=new-token",
    ]
    assert captured["connected_specs"] == expected_specs
    assert captured["persisted_specs"] == expected_specs
    assert captured["persisted_env_specs"] == expected_env_specs
    assert captured["env_overrides"] == {
        "jira": {"JIRA_TOKEN": "old-token"},
        "github": {"GITHUB_TOKEN": "new-token"},
    }
    assert runtime.specs == tuple(expected_specs)


def test_mcp_client_bridge_registers_all_external_tools_for_native_approval(tmp_path):
    bridge = MCPClientBridge()
    bridge._sessions["jira"] = MultiFakeSession()
    bridge._started = True

    set_bridge(bridge)

    executor = ToolExecutor(
        config=ToolRuntimeConfig(
            workspace_root=tmp_path,
            require_approval_for_mutations=True,
        ),
        approval_handler=None,
    )

    assert "jira_search_issue" in executor.registry.names()
    assert "jira_transition_issue" in executor.registry.names()
    assert executor.would_require_approval(
        ToolCall(name="jira_search_issue", arguments={"jql": "key = PC-1"})
    ) is True

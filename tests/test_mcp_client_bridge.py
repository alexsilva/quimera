from __future__ import annotations

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.drivers.tool_schemas import get_bridge_schemas, resolve_tool_schemas, set_bridge_schemas
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.mcp.client import HttpMCPTransport, MCPClientBridge, StdioMCPTransport, parse_mcp_client_spec
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

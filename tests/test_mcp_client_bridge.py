from __future__ import annotations

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.drivers.tool_schemas import get_bridge_schemas, resolve_tool_schemas, set_bridge_schemas
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.mcp.client import (
    HttpMCPTransport,
    MCPClientBridge,
    StdioMCPTransport,
    build_mcp_remote_command,
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


def test_parse_remote_shortcut_expands_to_mcp_remote_command():
    name, transport = parse_mcp_client_spec(
        "atlassian=remote:https://mcp.atlassian.com/v1/sse"
    )

    assert name == "atlassian"
    assert isinstance(transport, StdioMCPTransport)
    assert transport._command == [
        "npx",
        "-y",
        "mcp-remote",
        "https://mcp.atlassian.com/v1/sse",
    ]
    assert transport._name == "atlassian"


def test_parse_remote_shortcut_preserves_extra_mcp_remote_args():
    _, transport = parse_mcp_client_spec(
        "gh=remote:https://api.githubcopilot.com/mcp/ --transport sse-only"
    )

    assert transport._command == [
        "npx",
        "-y",
        "mcp-remote",
        "https://api.githubcopilot.com/mcp/",
        "--transport",
        "sse-only",
    ]


def test_parse_remote_shortcut_without_url_is_rejected():
    try:
        parse_mcp_client_spec("bad=remote:")
    except ValueError as exc:
        assert "remote" in str(exc)
    else:
        raise AssertionError("esperado ValueError para remote sem URL")


def test_build_mcp_remote_command_honors_runner_override(monkeypatch):
    monkeypatch.setenv("QUIMERA_MCP_REMOTE_CMD", "bunx mcp-remote@0.1.0")

    command = build_mcp_remote_command("https://mcp.example.test/sse")

    assert command == [
        "bunx",
        "mcp-remote@0.1.0",
        "https://mcp.example.test/sse",
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


# Sequência de stderr que o ``mcp-remote`` realmente emite ao subir uma conexão
# remota bem-sucedida (com prefixos de timestamp/tag, ruído JSON-RPC e as linhas
# de progresso "Connected"/"Proxy"/"Local STDIO"). Reproduz o cenário das imagens
# reportadas por ALEX (github/jira).
MCP_REMOTE_SUCCESS_STDERR = [
    "[2026-07-10 14:27:30.001Z] [github] Using automatically selected callback port: 5598",
    "[2026-07-10 14:27:30.500Z] [github] Connecting to remote server: https://api.githubcopilot.com/mcp/",
    '[2026-07-10 14:27:30.900Z] [Local→Remote] {"jsonrpc":"2.0","method":"initialize"}',
    "[2026-07-10 14:27:31.100Z] [github] Local STDIO server running",
    "[2026-07-10 14:27:31.200Z] [github] Proxy established successfully between local STDIO and remote transport",
    "[2026-07-10 14:27:31.400Z] [github] Connected to remote server using StreamableHTTPClientTransport",
]


def _feed_stderr(transport, lines):
    for line in lines:
        transport._print_stderr_line(line)


def test_stderr_progress_lines_are_not_echoed_to_console(capsys):
    """Progresso do mcp-remote não deve duplicar o sucesso da camada Quimera."""
    transport = StdioMCPTransport(
        ["npx", "-y", "mcp-remote", "https://api.githubcopilot.com/mcp/"],
        name="github",
    )

    _feed_stderr(transport, MCP_REMOTE_SUCCESS_STDERR)

    err = capsys.readouterr().err
    assert err == ""
    assert "conectada" not in err
    assert "Local STDIO server running" not in err
    assert "Proxy established" not in err


def test_stderr_errors_are_echoed_once_per_message(capsys):
    """Erros continuam visíveis e sem duplicação por conexão."""
    transport = StdioMCPTransport(
        ["npx", "-y", "mcp-remote", "https://mcp.atlassian.com/v1/sse"],
        name="jira",
    )

    transport._print_stderr_line("[2026-07-10 14:30:00Z] [jira] Error: unauthorized (401)")
    transport._print_stderr_line("[2026-07-10 14:30:01Z] [jira] Error: unauthorized (401)")

    err = capsys.readouterr().err
    assert err.count("MCP stdio erro 'jira'") == 1
    assert "unauthorized (401)" in err


def test_auth_prompt_block_is_printed_once_per_url(capsys):
    """O bloco de autorização OAuth aparece uma única vez, mesmo se repetido."""
    transport = StdioMCPTransport(
        ["npx", "-y", "mcp-remote", "https://mcp.atlassian.com/v1/sse"],
        name="atlassian",
    )
    url = "https://mcp.atlassian.com/v1/authorize?client_id=abc&state=xyz"

    transport._print_stderr_line(f"[2026-07-10 14:31:00Z] [atlassian] {url}")
    transport._print_stderr_line(f"[2026-07-10 14:31:05Z] [atlassian] {url}")

    err = capsys.readouterr().err
    assert err.count("Autorização MCP necessária — conexão 'atlassian'") == 1
    assert err.count(url) == 1
    assert "Aguardando confirmação no navegador" in err


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

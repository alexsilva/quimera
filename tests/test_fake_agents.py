import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from quimera.plugins.base import OpenAIConnection, PluginRegistry, apply_connection_overrides, set_connection_override
from quimera.plugins.fake import register_fake_plugins
from quimera.devtools.fake_agents import FakeOpenAIHandler, _build_completion, _extract_quimera_current_turn
from quimera.runtime.approval import AutoApprovalHandler
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.mcp import MCPServer


def test_fake_plugins_are_registered_only_when_requested():
    """Verifica que os plugins fake só são registrados após chamada explícita a register_fake_plugins e que as capacidades de cada um estão corretas."""
    registry = PluginRegistry()

    assert registry.all_names() == []

    names = register_fake_plugins(registry)

    assert names == ("fake-cli", "fake-cli-handoff", "fake-openai", "fake-openai-mcp-cli")
    assert registry.get("fake-cli") is not None
    assert registry.get("fake-cli-handoff") is not None
    assert registry.get("fake-openai") is not None
    assert registry.get("fake-openai-mcp-cli") is not None
    assert registry.get("fake-cli").supports_tools is False
    assert registry.get("fake-cli-handoff").supports_tools is True
    assert registry.get("fake-openai").supports_tools is True
    assert registry.get("fake-openai-mcp-cli").supports_tools is True



def test_fake_openai_prefers_current_turn_from_rendered_prompt():
    """Verifica que fake openai prefers current turn from rendered prompt."""
    prompt = (
        "texto antigo com README e arquivos\n"
        '<current_turn title="Pedido atual de >>>">\n'
        "Execute pwd via shell usando MCP\n"
        "</current_turn>\n"
    )
    assert _extract_quimera_current_turn(prompt) == "Execute pwd via shell usando MCP"
    response = _build_completion({
        "model": "quimera-fake-tools",
        "messages": [{"role": "user", "content": prompt}],
        "tools": [
            {"type": "function", "function": {"name": "read_file", "parameters": {}}},
            {"type": "function", "function": {"name": "list_files", "parameters": {}}},
            {"type": "function", "function": {"name": "run_shell", "parameters": {}}},
        ],
    })
    tool_call = response["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "run_shell"

def test_fake_openai_completion_requests_tool_call():
    """Verifica que fake openai completion requests tool call."""
    payload = {
        "model": "quimera-fake-tools",
        "messages": [{"role": "user", "content": "Liste os arquivos do workspace"}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "list_files", "parameters": {"type": "object", "properties": {}}},
            }
        ],
    }

    response = _build_completion(payload)

    message = response["choices"][0]["message"]
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert message["tool_calls"][0]["function"]["name"] == "list_files"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"path": "."}


def test_fake_openai_http_models_and_chat():
    """Verifica que fake openai http models and chat."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAIHandler)
    server.model = "quimera-fake-tools"
    server.quiet = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=2) as response:
            models = json.loads(response.read().decode("utf-8"))
        assert models["data"][0]["id"] == "quimera-fake-tools"

        body = json.dumps({
            "model": "quimera-fake-tools",
            "messages": [{"role": "user", "content": "Leia o README"}],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            completion = json.loads(response.read().decode("utf-8"))
        tool_call = completion["choices"][0]["message"]["tool_calls"][0]
        assert tool_call["function"]["name"] == "read_file"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_openai_mcp_cli_calls_fake_openai_and_executes_tool_via_mcp(tmp_path):
    """Verifica que openai mcp cli calls fake openai and executes tool via mcp."""
    (tmp_path / "README.md").write_text("# Projeto fake\nConteúdo via MCP.\n", encoding="utf-8")

    http = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAIHandler)
    http.model = "quimera-fake-tools"
    http.quiet = True
    http_thread = threading.Thread(target=http.serve_forever, daemon=True)
    http_thread.start()
    base_url = f"http://127.0.0.1:{http.server_address[1]}/v1"

    socket_path = str(tmp_path / "quimera-mcp.sock")
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), AutoApprovalHandler(approve_all=True))
    mcp = MCPServer(executor, auth_token="test-token")
    mcp.start_background(socket_path)
    for _ in range(50):
        if os.path.exists(socket_path):
            break
        time.sleep(0.02)

    env = {
        **os.environ,
        "QUIMERA_FAKE_OPENAI_BASE_URL": base_url,
        "QUIMERA_FAKE_MCP_SOCKET": socket_path,
        "QUIMERA_FAKE_MCP_TOKEN": "test-token",
        "PYTHONPATH": os.getcwd(),
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "quimera.devtools.fake_agents", "openai-mcp-cli", "Leia o README usando ferramentas"],
            cwd=tmp_path,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "MCP conectado" in completed.stdout
        assert "MCP tool_call: read_file" in completed.stdout
        assert "MCP tool_result: OK" in completed.stdout
        assert "# Projeto fake" in completed.stdout
    finally:
        http.shutdown()
        http.server_close()
        http_thread.join(timeout=2)
        mcp.shutdown()


def test_mcp_handoff_cli_calls_call_agent_via_mcp(tmp_path):
    """Verifica que mcp handoff cli calls call agent via mcp."""
    socket_path = str(tmp_path / "quimera-mcp.sock")
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), AutoApprovalHandler(approve_all=True))
    executor.set_active_agents_provider(lambda: ["fake-openai"])

    def call_agent(agent_name, **kwargs):
        assert agent_name == "fake-openai"
        assert kwargs["handoff"]["task"] == "Execute pwd via shell"
        return "delegado para fake-openai"

    executor.set_call_agent_fn(call_agent)
    mcp = MCPServer(executor, auth_token="test-token")
    mcp.start_background(socket_path)
    for _ in range(50):
        if os.path.exists(socket_path):
            break
        time.sleep(0.02)

    env = {
        **os.environ,
        "QUIMERA_FAKE_MCP_SOCKET": socket_path,
        "QUIMERA_FAKE_MCP_TOKEN": "test-token",
        "PYTHONPATH": os.getcwd(),
    }
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "quimera.devtools.fake_agents",
                "mcp-handoff-cli",
                "--target-agent",
                "fake-openai",
                "Execute pwd via shell",
            ],
            cwd=tmp_path,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "MCP conectado" in completed.stdout
        assert "MCP tool_call: call_agent" in completed.stdout
        assert "MCP tool_result: OK" in completed.stdout
        assert "delegado para fake-openai" in completed.stdout
    finally:
        mcp.shutdown()


def test_mcp_handoff_cli_delegates_only_current_turn_via_call_agent(tmp_path):
    """Verifica que mcp handoff cli delegates only current turn via call agent."""
    socket_path = str(tmp_path / "quimera-mcp-current-turn.sock")
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), AutoApprovalHandler(approve_all=True))
    executor.set_active_agents_provider(lambda: ["fake-openai"])

    def call_agent(agent_name, **kwargs):
        assert agent_name == "fake-openai"
        assert kwargs["handoff"]["task"] == "Execute pwd via shell"
        assert "<header" not in kwargs["handoff"]["task"]
        assert "métricas" not in kwargs["handoff"]["task"]
        return "delegado com pedido limpo"

    executor.set_call_agent_fn(call_agent)
    mcp = MCPServer(executor, auth_token="test-token")
    mcp.start_background(socket_path)
    for _ in range(50):
        if os.path.exists(socket_path):
            break
        time.sleep(0.02)

    rendered_prompt = (
        '<header title="Identificação">contexto CLI</header>\n'
        '<current_turn>Execute pwd via shell</current_turn>\n'
        '<agent_metrics>métricas internas</agent_metrics>'
    )
    env = {
        **os.environ,
        "QUIMERA_FAKE_MCP_SOCKET": socket_path,
        "QUIMERA_FAKE_MCP_TOKEN": "test-token",
        "PYTHONPATH": os.getcwd(),
    }
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "quimera.devtools.fake_agents",
                "mcp-handoff-cli",
                "--target-agent",
                "fake-openai",
            ],
            cwd=tmp_path,
            env=env,
            input=rendered_prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert '"task": "Execute pwd via shell"' in completed.stdout
        assert "<header" not in completed.stdout
        assert "métricas internas" not in completed.stdout
        assert "delegado com pedido limpo" in completed.stdout
    finally:
        mcp.shutdown()


def test_fake_openai_ignores_persisted_connection_overrides_in_test_registry(tmp_path):
    """Verifica que fake openai ignores persisted connection overrides in test registry."""
    conn_file = tmp_path / "connections.json"
    conn_file.write_text(json.dumps({
        "fake-openai": {
            "type": "openai",
            "model": "external-model",
            "base_url": "https://external.example/v1",
            "api_key_env": "EXTERNAL_API_KEY",
            "provider": "openai_compat",
            "supports_native_tools": True,
        }
    }), encoding="utf-8")
    registry = PluginRegistry()
    names = register_fake_plugins(registry)

    from unittest.mock import patch
    with patch("quimera.plugins.base._get_connections_file", return_value=conn_file):
        apply_connection_overrides(registry=registry, exclude_names=set(names))

    connection = registry.get("fake-openai").effective_connection()
    assert connection.model == "quimera-fake-tools"
    assert connection.base_url == "http://127.0.0.1:8765/v1"
    assert connection.api_key_env == "QUIMERA_FAKE_API_KEY"


def test_fake_openai_allows_explicit_non_persistent_process_override(tmp_path):
    """Verifica que fake openai allows explicit non persistent process override."""
    conn_file = tmp_path / "connections.json"
    registry = PluginRegistry()
    register_fake_plugins(registry)
    override = OpenAIConnection(
        model="local-process-model",
        base_url="http://127.0.0.1:9999/v1",
        api_key_env="LOCAL_PROCESS_KEY",
        provider="openai_compat",
    )

    from unittest.mock import patch
    with patch("quimera.plugins.base._get_connections_file", return_value=conn_file):
        set_connection_override("fake-openai", override, persist=False, registry=registry)

    connection = registry.get("fake-openai").effective_connection()
    assert connection.model == "local-process-model"
    assert connection.base_url == "http://127.0.0.1:9999/v1"
    assert connection.api_key_env == "LOCAL_PROCESS_KEY"
    assert not conn_file.exists()


def test_fake_openai_mcp_cli_env_uses_fake_openai_process_override():
    """Verifica que fake openai mcp cli env uses fake openai process override."""
    registry = PluginRegistry()
    register_fake_plugins(registry)
    override = OpenAIConnection(
        model="local-process-model",
        base_url="http://127.0.0.1:43210/v1",
        api_key_env="LOCAL_PROCESS_KEY",
        provider="openai_compat",
    )
    set_connection_override("fake-openai", override, persist=False, registry=registry)

    with patch("quimera.plugins.get", registry.get):
        env = registry.get("fake-openai-mcp-cli").env_for_cli()

    assert env["QUIMERA_FAKE_OPENAI_BASE_URL"] == "http://127.0.0.1:43210/v1"
    assert env["QUIMERA_FAKE_OPENAI_MODEL"] == "local-process-model"

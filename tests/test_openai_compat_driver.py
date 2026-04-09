"""
Testes para o driver OpenAI-compatible (Ollama/Qwen e afins).
O cliente OpenAI é sempre mockado — não há chamada de rede real.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from quimera.runtime.drivers.openai_compat import (
    OpenAICompatDriver,
    _sanitize_assistant_text,
    _strip_thinking,
)
from quimera.runtime.drivers.tool_schemas import TOOL_SCHEMAS, resolve_tool_schemas
from quimera.runtime.models import ToolCall, ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(content=None):
    """Chunk de streaming para respostas de texto (sem ferramentas)."""
    delta = SimpleNamespace(content=content)
    choice = SimpleNamespace(delta=delta)
    return SimpleNamespace(choices=[choice])


def _make_non_streaming_response(content=None, tool_calls=None):
    """Resposta não-streaming (usada quando há ferramentas)."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def _make_tool_call(tc_id, name, arguments_json):
    """Tool call estruturada para resposta não-streaming."""
    func = SimpleNamespace(name=name, arguments=arguments_json)
    return SimpleNamespace(id=tc_id, function=func)


def _make_driver(model="qwen3-coder:30b", base_url="http://localhost:11434/v1"):
    """Cria um driver com o cliente OpenAI mockado."""
    with patch("quimera.runtime.drivers.openai_compat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        driver = OpenAICompatDriver(model=model, base_url=base_url)
    driver._client = mock_client
    return driver, mock_client


def _setup_stream(mock_client, chunks):
    """Configura streaming (somente para chamadas sem tools)."""
    mock_client.chat.completions.create.return_value = iter(chunks)


# ---------------------------------------------------------------------------
# Testes de tool_schemas
# ---------------------------------------------------------------------------

def test_all_schemas_have_required_fields():
    required_keys = {"type", "function"}
    function_keys = {"name", "description", "parameters"}
    for schema in TOOL_SCHEMAS:
        assert required_keys <= schema.keys(), f"Schema sem campos obrigatórios: {schema}"
        assert function_keys <= schema["function"].keys(), f"Function schema incompleto: {schema}"


def test_schema_names_match_registered_tools():
    expected = {"list_files", "read_file", "write_file", "grep_search", "run_shell",
                "list_tasks", "list_jobs", "get_job"}
    actual = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert actual == expected


def test_resolve_tool_schemas_hides_task_tools_without_db():
    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path=None)
    mock_executor.registry.names.return_value = [
        "list_files", "read_file", "write_file", "grep_search", "run_shell",
        "list_tasks", "list_jobs", "get_job",
    ]

    actual = {s["function"]["name"] for s in resolve_tool_schemas(mock_executor)}
    assert actual == {"list_files", "read_file", "write_file", "grep_search", "run_shell"}


def test_required_args_are_lists():
    for schema in TOOL_SCHEMAS:
        params = schema["function"]["parameters"]
        assert isinstance(params.get("required"), list), (
            f"'required' deve ser lista em: {schema['function']['name']}"
        )


# ---------------------------------------------------------------------------
# Testes de _strip_thinking
# ---------------------------------------------------------------------------

def test_strip_thinking_removes_block():
    text = "<think>raciocínio interno</think>Resposta final."
    assert _strip_thinking(text) == "Resposta final."


def test_strip_thinking_multiline():
    text = "<think>\nlinha 1\nlinha 2\n</think>\nResposta."
    assert _strip_thinking(text) == "Resposta."


def test_strip_thinking_no_block():
    text = "Resposta sem bloco think."
    assert _strip_thinking(text) == text


def test_strip_thinking_multiple_blocks():
    text = "<think>a</think>Texto<think>b</think>Final"
    assert _strip_thinking(text) == "TextoFinal"


def test_sanitize_assistant_text_removes_function_residue():
    text = "<think>x</think></function>\nResposta final\n</tool_call>"
    assert _sanitize_assistant_text(text) == "Resposta final"


# ---------------------------------------------------------------------------
# Testes de OpenAICompatDriver.__init__
# ---------------------------------------------------------------------------

def test_driver_init_missing_openai_raises():
    import quimera.runtime.drivers.openai_compat as mod
    with patch.object(mod, "OpenAI", None):
        with pytest.raises(ImportError, match="openai"):
            OpenAICompatDriver(model="m", base_url="http://localhost")


def test_driver_init_success():
    driver, _ = _make_driver()
    assert driver.model == "qwen3-coder:30b"


# ---------------------------------------------------------------------------
# Testes de _chat — resposta simples (sem tool calls)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Testes de _chat_streaming (sem ferramentas)
# ---------------------------------------------------------------------------

def test_chat_simple_response():
    driver, mock_client = _make_driver()
    chunks = [_make_chunk(content="Olá "), _make_chunk(content="mundo!"), _make_chunk(content=None)]
    _setup_stream(mock_client, chunks)

    text, tool_calls = driver._chat([{"role": "user", "content": "oi"}], tools=[])
    assert text == "Olá mundo!"
    assert tool_calls == []


def test_chat_empty_choices_ignored():
    driver, mock_client = _make_driver()
    empty_chunk = SimpleNamespace(choices=[])
    _setup_stream(mock_client, [empty_chunk, _make_chunk(content="ok")])

    text, tool_calls = driver._chat([], tools=[])
    assert text == "ok"
    assert tool_calls == []


def test_chat_no_tools_uses_streaming():
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="resposta")])

    driver._chat([{"role": "user", "content": "x"}], tools=[])

    call_kwargs = mock_client.chat.completions.create.call_args[1]
    assert call_kwargs["stream"] is True
    assert "tools" not in call_kwargs
    assert "tool_choice" not in call_kwargs


# ---------------------------------------------------------------------------
# Testes de _chat_with_tools (com ferramentas — modo não-streaming)
# ---------------------------------------------------------------------------

def test_chat_with_tools_uses_non_streaming():
    """Quando tools estão presentes, usa stream=False para evitar o bug do Ollama."""
    driver, mock_client = _make_driver()
    mock_client.chat.completions.create.return_value = _make_non_streaming_response(
        content="ok", tool_calls=None
    )
    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]

    driver._chat([{"role": "user", "content": "x"}], tools=resolve_tool_schemas(mock_executor))

    call_kwargs = mock_client.chat.completions.create.call_args[1]
    assert call_kwargs.get("stream") is False
    assert call_kwargs["tool_choice"] == "auto"
    assert call_kwargs["tools"] == TOOL_SCHEMAS


def test_chat_with_tools_returns_structured_tool_calls():
    driver, mock_client = _make_driver()
    tc = _make_tool_call("call_abc", "read_file", '{"path":"app.py"}')
    mock_client.chat.completions.create.return_value = _make_non_streaming_response(
        content="", tool_calls=[tc]
    )

    text, tool_calls = driver._chat([], tools=TOOL_SCHEMAS)
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_abc"
    assert tool_calls[0]["name"] == "read_file"
    assert tool_calls[0]["arguments"] == {"path": "app.py"}


def test_chat_with_tools_invalid_json_returns_empty_dict():
    driver, mock_client = _make_driver()
    tc = _make_tool_call("x", "run_shell", "NOT_JSON")
    mock_client.chat.completions.create.return_value = _make_non_streaming_response(
        content="", tool_calls=[tc]
    )

    _, tool_calls = driver._chat([], tools=TOOL_SCHEMAS)
    assert tool_calls[0]["arguments"] == {}


def test_chat_with_tools_no_tool_calls_in_response():
    driver, mock_client = _make_driver()
    mock_client.chat.completions.create.return_value = _make_non_streaming_response(
        content="</function>\nSó texto, sem ferramentas.", tool_calls=None
    )

    text, tool_calls = driver._chat([], tools=TOOL_SCHEMAS)
    assert text == "Só texto, sem ferramentas."
    assert tool_calls == []


# ---------------------------------------------------------------------------
# Testes de _execute_tool
# ---------------------------------------------------------------------------

def test_execute_tool_success():
    driver, _ = _make_driver()
    mock_executor = MagicMock()
    mock_executor.execute.return_value = ToolResult(ok=True, tool_name="read_file", content="conteúdo")

    tc = {"id": "c1", "name": "read_file", "arguments": {"path": "app.py"}}
    result = driver._execute_tool(tc, mock_executor)

    assert result.ok is True
    assert result.content == "conteúdo"
    called_with: ToolCall = mock_executor.execute.call_args[0][0]
    assert called_with.name == "read_file"
    assert called_with.arguments == {"path": "app.py"}
    assert called_with.call_id == "c1"


def test_execute_tool_exception_returns_error_result():
    driver, _ = _make_driver()
    mock_executor = MagicMock()
    mock_executor.execute.side_effect = RuntimeError("boom")

    tc = {"id": "c2", "name": "run_shell", "arguments": {"command": "ls"}}
    result = driver._execute_tool(tc, mock_executor)

    assert result.ok is False
    assert "boom" in result.error


# ---------------------------------------------------------------------------
# Testes de run() — loop completo
# ---------------------------------------------------------------------------

def test_run_simple_response_no_tools():
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="Resposta simples")])

    result = driver.run("prompt", tool_executor=None)
    assert result == "Resposta simples"


def test_run_strips_thinking_block():
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="<think>thinking</think>Resposta")])

    result = driver.run("prompt", tool_executor=None)
    assert result == "Resposta"


def test_run_strips_tool_residue_from_final_response():
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="</function>Resposta final</tool_call>")])

    result = driver.run("prompt", tool_executor=None)
    assert result == "Resposta final"


def test_run_tools_system_prompt_guides_tool_usage():
    driver, mock_client = _make_driver()
    mock_client.chat.completions.create.return_value = _make_non_streaming_response(
        content="ok", tool_calls=None
    )
    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(workspace_root="/tmp/workspace", db_path=None)
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]

    driver.run("prompt", tool_executor=mock_executor)

    messages = mock_client.chat.completions.create.call_args[1]["messages"]
    system_message = messages[0]
    assert system_message["role"] == "system"
    assert "prefira a ferramenta mais específica e barata" in system_message["content"]
    assert "Workspace raiz: /tmp/workspace." in system_message["content"]
    tool_names = {tool["function"]["name"] for tool in mock_client.chat.completions.create.call_args[1]["tools"]}
    assert tool_names == {"list_files", "read_file", "write_file", "grep_search", "run_shell"}


def test_run_returns_none_on_empty_response():
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content=None)])

    result = driver.run("prompt", tool_executor=None)
    assert result is None


def test_run_tool_loop_one_hop():
    """Modelo chama read_file (não-streaming), recebe resultado, responde com texto final (streaming)."""
    driver, mock_client = _make_driver()

    tc_id = "call_1"
    tc = _make_tool_call(tc_id, "read_file", '{"path":"x.py"}')
    # 1ª chamada: não-streaming com tool call
    resp_1 = _make_non_streaming_response(content="", tool_calls=[tc])
    # 2ª chamada: não-streaming sem tool calls (resposta final)
    resp_2 = _make_non_streaming_response(content="Arquivo lido com sucesso.", tool_calls=None)
    mock_client.chat.completions.create.side_effect = [resp_1, resp_2]

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.return_value = ToolResult(
        ok=True, tool_name="read_file", content="conteúdo do arquivo"
    )

    result = driver.run("leia o arquivo x.py", tool_executor=mock_executor)
    assert result == "Arquivo lido com sucesso."
    assert mock_executor.execute.call_count == 1
    assert mock_client.chat.completions.create.call_count == 2


def test_run_tool_loop_sends_tool_result_message():
    """Verifica que o resultado da tool é enviado como mensagem 'tool' na 2ª chamada."""
    driver, mock_client = _make_driver()

    tc_id = "call_xyz"
    tc = _make_tool_call(tc_id, "run_shell", '{"command":"ls"}')
    resp_1 = _make_non_streaming_response(content="", tool_calls=[tc])
    resp_2 = _make_non_streaming_response(content="Done.", tool_calls=None)
    mock_client.chat.completions.create.side_effect = [resp_1, resp_2]

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.return_value = ToolResult(ok=True, tool_name="run_shell", content="file.py")

    driver.run("liste arquivos", tool_executor=mock_executor)

    second_call_messages = mock_client.chat.completions.create.call_args_list[1][1]["messages"]
    tool_result_msg = next(m for m in second_call_messages if m.get("role") == "tool")
    assert tool_result_msg["tool_call_id"] == tc_id
    payload = json.loads(tool_result_msg["content"])
    assert payload["ok"] is True
    assert payload["content"] == "file.py"


def test_run_api_error_returns_none():
    driver, mock_client = _make_driver()
    mock_client.chat.completions.create.side_effect = RuntimeError("connection refused")

    result = driver.run("prompt", tool_executor=None)
    assert result is None


def test_run_max_hops_returns_last_text():
    """Quando o modelo não para de chamar tools, o loop encerra no MAX_TOOL_HOPS."""
    from quimera.runtime.drivers.openai_compat import MAX_TOOL_HOPS

    driver, mock_client = _make_driver()
    tc = _make_tool_call("c", "run_shell", '{"command":"x"}')

    def always_tool_response(*args, **kwargs):
        return _make_non_streaming_response(content="parcial", tool_calls=[tc])

    mock_client.chat.completions.create.side_effect = always_tool_response

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.return_value = ToolResult(ok=True, tool_name="run_shell", content="ok")

    result = driver.run("prompt", tool_executor=mock_executor)
    assert result is not None
    assert mock_client.chat.completions.create.call_count == MAX_TOOL_HOPS + 1


# ---------------------------------------------------------------------------
# Testes de AgentClient dispatch
# ---------------------------------------------------------------------------

def test_agent_client_dispatches_api_driver():
    """AgentClient.call() deve chamar _call_api() para plugins com driver != 'cli'."""
    from quimera.agents import AgentClient

    renderer = MagicMock()
    client = AgentClient(renderer)

    mock_plugin = MagicMock()
    mock_plugin.driver = "openai_compat"
    mock_plugin.model = "qwen3-coder:30b"
    mock_plugin.base_url = "http://localhost:11434/v1"
    mock_plugin.api_key_env = None

    with patch("quimera.plugins.get", return_value=mock_plugin):
        with patch.object(client, "_call_api", return_value="api response") as mock_api:
            result = client.call("qwen", "prompt")
            mock_api.assert_called_once()
            assert result == "api response"


def test_agent_client_cli_plugins_use_subprocess():
    """Plugins com driver='cli' continuam usando subprocess."""
    from quimera.agents import AgentClient

    renderer = MagicMock()
    client = AgentClient(renderer)

    mock_plugin = MagicMock()
    mock_plugin.driver = "cli"
    mock_plugin.cmd = ["mock-agent"]
    mock_plugin.prompt_as_arg = False

    with patch("quimera.plugins.get", return_value=mock_plugin):
        with patch.object(client, "run", return_value="cli output") as mock_run:
            result = client.call("mock", "prompt")
            mock_run.assert_called_once()
            assert result == "cli output"


def test_agent_client_mock_plugin_driver_uses_cli():
    """MagicMock sem driver explícito ainda usa o caminho CLI (isinstance check)."""
    from quimera.agents import AgentClient

    renderer = MagicMock()
    client = AgentClient(renderer)

    # Plugin sem atributo driver definido explicitamente (como os mocks dos testes legados)
    mock_plugin = MagicMock(spec=["name", "cmd", "prompt_as_arg"])
    mock_plugin.cmd = ["echo"]
    mock_plugin.prompt_as_arg = False

    with patch("quimera.plugins.get", return_value=mock_plugin):
        with patch.object(client, "run", return_value="ok") as mock_run:
            client.call("any", "prompt")
            mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# Testes de AgentPlugin com driver de API
# ---------------------------------------------------------------------------

def test_agent_plugin_api_defaults():
    from quimera.plugins.base import AgentPlugin

    plugin = AgentPlugin(
        name="test",
        prefix="/test",
        style=("red", "Test"),
        driver="openai_compat",
        model="llama3",
        base_url="http://localhost:11434/v1",
    )
    assert plugin.driver == "openai_compat"
    assert plugin.model == "llama3"
    assert plugin.cmd == []


def test_agent_plugin_cli_defaults():
    from quimera.plugins.base import AgentPlugin

    plugin = AgentPlugin(
        name="test",
        prefix="/test",
        style=("red", "Test"),
        cmd=["my-cli"],
    )
    assert plugin.driver == "cli"
    assert plugin.model is None
    assert plugin.base_url is None


# ---------------------------------------------------------------------------
# Testes de regressão: plugins existentes ainda funcionam
# ---------------------------------------------------------------------------

def test_existing_cli_plugins_still_register():
    import quimera.plugins.claude  # noqa: F401
    import quimera.plugins.mock  # noqa: F401
    import quimera.plugins.qwen  # noqa: F401
    import quimera.plugins as plugins

    claude = plugins.get("claude")
    assert claude is not None
    assert claude.driver == "cli"
    assert claude.cmd == ["claude", "--permission-mode=dontAsk", "-p"]

    mock = plugins.get("mock")
    assert mock is not None
    assert mock.driver == "cli"

    qwen = plugins.get("qwen")
    assert qwen is not None
    assert qwen.driver == "openai_compat"
    assert qwen.model == "qwen3-coder:30b"
    assert qwen.supports_tools is True
    assert qwen.supports_task_execution is True

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
    _MAX_TOOL_LOOP_MESSAGES,
    MAX_TOOL_HOPS_BY_RELIABILITY,
    OpenAICompatDriver,
    _build_tool_system_prompt,
    _prune_tool_loop_messages,
    _sanitize_assistant_text,
    _strip_thinking,
)
from quimera.runtime.drivers.repl import DriverRepl
from quimera.runtime.drivers.tool_schemas import TOOL_SCHEMAS, resolve_tool_schemas
from quimera.runtime.models import ToolCall, ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(content=None, diff=None):
    """Chunk de streaming para respostas de texto (sem ferramentas)."""
    delta = SimpleNamespace(content=content, diff=diff)
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
    expected = {"list_files", "read_file", "write_file", "apply_patch", "grep_search", "run_shell",
                "exec_command", "write_stdin", "close_command_session", "list_tasks", "list_jobs", "get_job", "remove_file"}
    actual = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert actual == expected


def test_resolve_tool_schemas_hides_task_tools_without_db():
    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path=None)
    mock_executor.policy = SimpleNamespace(blocked_tools=[])
    mock_executor.registry.names.return_value = [
        "list_files", "read_file", "write_file", "apply_patch", "grep_search", "run_shell",
        "exec_command", "write_stdin", "close_command_session",
        "list_tasks", "list_jobs", "get_job", "remove_file",
    ]

    actual = {s["function"]["name"] for s in resolve_tool_schemas(mock_executor)}
    assert actual == {
        "list_files",
        "read_file",
        "write_file",
        "apply_patch",
        "grep_search",
        "run_shell",
        "exec_command",
        "write_stdin",
        "close_command_session",
        "remove_file",
    }


def test_resolve_tool_schemas_hides_blocked_tools_from_active_mode():
    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db")
    mock_executor.policy = SimpleNamespace(blocked_tools=["run_shell", "exec_command", "apply_patch"])
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]

    actual = {s["function"]["name"] for s in resolve_tool_schemas(mock_executor)}
    assert "run_shell" not in actual
    assert "exec_command" not in actual
    assert "apply_patch" not in actual
    assert "read_file" in actual
    assert "list_files" in actual


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


def test_build_tool_system_prompt_includes_workspace_hint():
    prompt = _build_tool_system_prompt(["read_file", "apply_patch"], "/tmp/workspace")

    assert "read_file, apply_patch" in prompt
    assert "Workspace raiz: /tmp/workspace." in prompt
    assert "não invente envelopes JSON intermediários" in prompt


def test_build_tool_system_prompt_avoids_unavailable_tool_guidance():
    prompt = _build_tool_system_prompt(["read_file"], "/tmp/workspace")

    assert "read_file usa 'path', não 'file_path'" in prompt
    assert "run_shell" not in prompt
    assert "exec_command" not in prompt
    assert "começar exatamente com '*** Begin Patch'" not in prompt


def test_prune_tool_loop_messages_keeps_head_and_recent_tail():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ] + [
        {"role": "tool" if i % 2 else "assistant", "content": f"m{i}"}
        for i in range(20)
    ]

    pruned = _prune_tool_loop_messages(messages)

    assert len(pruned) == _MAX_TOOL_LOOP_MESSAGES
    assert pruned[:2] == messages[:2]
    assert pruned[2:] == messages[-(_MAX_TOOL_LOOP_MESSAGES - 2):]


def test_prune_tool_loop_messages_preserves_assistant_for_multi_tool_results():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]
    for i in range(6):
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"call_{i}_a"}, {"id": f"call_{i}_b"}],
        })
        messages.append({"role": "tool", "tool_call_id": f"call_{i}_a", "content": '{"ok": true}'})
        messages.append({"role": "tool", "tool_call_id": f"call_{i}_b", "content": '{"ok": true}'})

    pruned = _prune_tool_loop_messages(messages)

    assert pruned[:2] == messages[:2]
    assert len(pruned) <= len(messages)

    for index, msg in enumerate(pruned):
        if msg.get("role") != "tool":
            continue
        assert index > 0
        previous = pruned[index - 1]
        if previous.get("role") == "tool":
            previous = pruned[index - 2]
        assert previous.get("role") == "assistant"
        assert previous.get("tool_calls")


def test_prune_tool_loop_messages_caps_oversized_final_tool_segment():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"call_{i}"} for i in range(20)],
        },
    ]
    messages.extend(
        {"role": "tool", "tool_call_id": f"call_{i}", "content": '{"ok": true}'}
        for i in range(20)
    )

    pruned = _prune_tool_loop_messages(messages)

    assert len(pruned) == _MAX_TOOL_LOOP_MESSAGES
    assert pruned[:2] == messages[:2]
    assert pruned[2]["role"] == "assistant"

    retained_tool_ids = [msg["tool_call_id"] for msg in pruned[3:]]
    assert retained_tool_ids == [f"call_{i}" for i in range(7, 20)]
    assert [call["id"] for call in pruned[2]["tool_calls"]] == retained_tool_ids


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


def test_chat_streaming_supports_structured_diff_chunks():
    driver, mock_client = _make_driver()
    chunks = [
        _make_chunk(diff={"op": "replace", "text": "abc"}),
        _make_chunk(diff={"op": "add", "text": "def"}),
    ]
    _setup_stream(mock_client, chunks)
    received = []

    text, tool_calls = driver._chat(
        [{"role": "user", "content": "oi"}],
        tools=[],
        on_text_chunk=received.append,
    )

    assert text == "abcdef"
    assert tool_calls == []
    assert received == [
        {"text": "", "diff": [{"op": "replace", "text": "abc"}]},
        {"text": "", "diff": [{"op": "add", "text": "def"}]},
    ]


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
    assert "descubra o alvo antes de editar" in system_message["content"]
    assert "começar exatamente com '*** Begin Patch'" in system_message["content"]
    assert "não repita o mesmo payload inválido" in system_message["content"]
    assert '"action":"execute"' in system_message["content"]
    assert "read_file usa 'path', não 'file_path'" in system_message["content"]
    assert "use exatamente 'run_shell' para uma execução simples ou 'exec_command' para sessão interativa" in \
           system_message["content"]
    assert "nunca invente nomes como 'run', 'run_shell_command' ou 'execute_command'" in system_message["content"]
    assert "Workspace raiz: /tmp/workspace." in system_message["content"]
    tool_names = {tool["function"]["name"] for tool in mock_client.chat.completions.create.call_args[1]["tools"]}
    assert tool_names == {
        "list_files",
        "read_file",
        "write_file",
        "apply_patch",
        "grep_search",
        "run_shell",
        "exec_command",
        "write_stdin",
        "close_command_session",
        "remove_file",
    }


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


def test_run_tool_loop_uses_minimal_prompt_payload_and_valid_json():
    driver, mock_client = _make_driver()

    tc_id = "call_minimal"
    tc = _make_tool_call(tc_id, "run_shell", '{"command":"ls"}')
    resp_1 = _make_non_streaming_response(content="", tool_calls=[tc])
    resp_2 = _make_non_streaming_response(content="Done.", tool_calls=None)
    mock_client.chat.completions.create.side_effect = [resp_1, resp_2]

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.return_value = ToolResult(
        ok=False,
        tool_name="run_shell",
        content="x" * 6000,
        error="y" * 6000,
        exit_code=9,
        duration_ms=12,
        data={"cwd": "/tmp/workspace"},
    )

    driver.run("liste arquivos", tool_executor=mock_executor)

    second_call_messages = mock_client.chat.completions.create.call_args_list[1][1]["messages"]
    tool_result_msg = next(m for m in second_call_messages if m.get("role") == "tool")
    payload = json.loads(tool_result_msg["content"])

    assert set(payload) == {"ok", "content", "error", "truncated", "exit_code"}
    assert payload["ok"] is False
    assert payload["exit_code"] == 9
    assert payload["truncated"] is True
    assert "resultado com 6000 caracteres" in payload["content"]
    assert "resultado com 6000 caracteres" in payload["error"]


def test_run_tool_loop_prunes_messages_between_hops():
    driver, mock_client = _make_driver()

    def side_effect(*args, **kwargs):
        messages = kwargs["messages"]
        if len(mock_client.chat.completions.create.call_args_list) < 4:
            tc_id = f"call_{len(mock_client.chat.completions.create.call_args_list)}"
            return _make_non_streaming_response(
                content="",
                tool_calls=[_make_tool_call(tc_id, "run_shell", '{"command":"ls"}')],
            )
        return _make_non_streaming_response(content="Done.", tool_calls=None)

    mock_client.chat.completions.create.side_effect = side_effect

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.return_value = ToolResult(ok=True, tool_name="run_shell", content="file.py")

    driver.run("liste arquivos", tool_executor=mock_executor)

    observed_lengths = [
        call.kwargs["messages"]
        for call in mock_client.chat.completions.create.call_args_list
    ]
    assert len(observed_lengths[-1]) <= _MAX_TOOL_LOOP_MESSAGES
    assert observed_lengths[-1][0]["role"] == "system"
    assert observed_lengths[-1][1]["role"] == "user"


def test_run_api_error_returns_none():
    driver, mock_client = _make_driver()
    mock_client.chat.completions.create.side_effect = RuntimeError("connection refused")

    result = driver.run("prompt", tool_executor=None)
    assert result is None


def test_driver_repl_probe_backend_success():
    fake_plugin = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    fake_response = MagicMock()
    fake_response.__enter__.return_value.status = 200
    fake_response.__exit__.return_value = False

    with patch("quimera.runtime.drivers.repl.urllib_request.urlopen", return_value=fake_response), \
            patch("quimera.runtime.drivers.repl.OpenAICompatDriver"):
        repl = DriverRepl(
            "ollama-qwen",
            get_plugin=lambda name: fake_plugin if name == "ollama-qwen" else None,
            all_plugins=lambda: [fake_plugin],
        )
        repl.ensure_backend_available()


def test_driver_repl_probe_backend_unavailable_raises_clear_error():
    fake_plugin = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )

    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"), \
            patch(
                "quimera.runtime.drivers.repl.urllib_request.urlopen",
                side_effect=OSError("connection refused"),
            ):
        repl = DriverRepl(
            "ollama-qwen",
            get_plugin=lambda name: fake_plugin if name == "ollama-qwen" else None,
            all_plugins=lambda: [fake_plugin],
        )
        with pytest.raises(RuntimeError, match="indisponível"):
            repl.ensure_backend_available()


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


def test_run_low_reliability_uses_lower_max_hops():
    driver, mock_client = _make_driver()
    driver.tool_use_reliability = "low"
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
    assert mock_client.chat.completions.create.call_count == MAX_TOOL_HOPS_BY_RELIABILITY["low"] + 1


def test_run_low_reliability_aborts_on_repeated_invalid_tool():
    driver, mock_client = _make_driver()
    driver.tool_use_reliability = "low"
    tc = _make_tool_call("c", "bad_tool", '{"path":"x"}')
    mock_client.chat.completions.create.side_effect = [
        _make_non_streaming_response(content="", tool_calls=[tc]),
        _make_non_streaming_response(content="", tool_calls=[tc]),
    ]

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.side_effect = [
        ToolResult(ok=False, tool_name="bad_tool", error="Sem política para a ferramenta: bad_tool"),
        ToolResult(ok=False, tool_name="bad_tool", error="Sem política para a ferramenta: bad_tool"),
    ]

    result = driver.run("prompt", tool_executor=mock_executor)
    assert result == "Falha: loop de ferramenta inválida detectado."
    assert mock_client.chat.completions.create.call_count == 2


def test_run_reports_tool_abort_callback():
    driver, mock_client = _make_driver()
    driver.tool_use_reliability = "low"
    tc = _make_tool_call("c", "bad_tool", '{"path":"x"}')
    mock_client.chat.completions.create.side_effect = [
        _make_non_streaming_response(content="", tool_calls=[tc]),
        _make_non_streaming_response(content="", tool_calls=[tc]),
    ]

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.side_effect = [
        ToolResult(ok=False, tool_name="bad_tool", error="Sem política para a ferramenta: bad_tool"),
        ToolResult(ok=False, tool_name="bad_tool", error="Sem política para a ferramenta: bad_tool"),
    ]
    aborts = []

    driver.run("prompt", tool_executor=mock_executor, on_tool_abort=aborts.append)
    assert aborts == ["invalid_tool_loop"]


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
            result = client.call("ollama-qwen", "prompt")
            mock_api.assert_called_once()
            assert result == "api response"


def test_agent_client_passes_tool_use_reliability_to_api_driver():
    from quimera.agents import AgentClient

    renderer = MagicMock()
    client = AgentClient(renderer)
    plugin = SimpleNamespace(
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        tool_use_reliability="low",
        supports_tools=True,
    )

    with patch("quimera.plugins.get", return_value=plugin), \
            patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls:
        mock_driver = MagicMock()
        mock_driver.run.return_value = "ok"
        mock_driver_cls.return_value = mock_driver
        result = client.call("ollama-qwen", "prompt")

    assert result == "ok"
    assert mock_driver_cls.call_args.kwargs["tool_use_reliability"] == "low"


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
# Testes de cancelamento cooperativo no driver
# ---------------------------------------------------------------------------

def test_run_cancel_event_between_hops():
    """Driver retorna None quando cancel_event é acionado após o primeiro hop."""
    import threading
    driver, mock_client = _make_driver()
    tc = _make_tool_call("c", "run_shell", '{"command":"ls"}')
    cancel_event = threading.Event()
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        cancel_event.set()  # sinaliza cancelamento após primeira resposta
        return _make_non_streaming_response(content="parcial", tool_calls=[tc])

    mock_client.chat.completions.create.side_effect = side_effect

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/t.db", workspace_root="/tmp")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.return_value = ToolResult(ok=True, tool_name="run_shell", content="ok")

    result = driver.run("prompt", tool_executor=mock_executor, cancel_event=cancel_event)

    assert result is None
    # Apenas um hop: o cancelamento é checado no início do hop seguinte
    assert call_count == 1


def test_run_cancel_event_in_streaming():
    """Driver interrompe streaming e retorna texto parcial quando cancel_event é acionado."""
    import threading
    driver, mock_client = _make_driver()
    cancel_event = threading.Event()

    def make_chunks():
        yield _make_chunk(content="parte1 ")
        cancel_event.set()  # sinalizado antes do próximo chunk
        yield _make_chunk(content="parte2")

    _setup_stream(mock_client, make_chunks())

    result = driver.run("prompt", tool_executor=None, cancel_event=cancel_event)
    # "parte2" não deve ser incluído — cancelamento detectado no início da iteração seguinte
    assert result == "parte1"


def test_run_no_cancel_event_behaves_normally():
    """Sem cancel_event, o driver funciona igual ao comportamento anterior."""
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="resposta completa")])

    result = driver.run("prompt", tool_executor=None, cancel_event=None)
    assert result == "resposta completa"


def test_run_cancel_event_not_set_completes_normally():
    """cancel_event fornecido mas não acionado não interfere na execução."""
    import threading
    driver, mock_client = _make_driver()
    cancel_event = threading.Event()  # nunca acionado
    _setup_stream(mock_client, [_make_chunk(content="ok")])

    result = driver.run("prompt", tool_executor=None, cancel_event=cancel_event)
    assert result == "ok"


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

def test_existing_plugins_still_register():
    import quimera.plugins.claude  # noqa: F401
    import quimera.plugins.mock  # noqa: F401
    import quimera.plugins.ollama  # noqa: F401
    import quimera.plugins as plugins

    claude = plugins.get("claude")
    assert claude is not None
    assert claude.driver == "cli"
    assert claude.cmd == ["claude", "--permission-mode=bypassPermissions", "--output-format=stream-json", "--verbose",
                          "-p"]

    mock = plugins.get("mock")
    assert mock is not None
    assert mock.driver == "cli"

    granite = plugins.get("ollama-granite4")
    assert granite is not None
    assert granite.driver == "openai_compat"
    assert granite.model == "granite4.1:8b"
    assert granite.supports_tools is True
    assert granite.supports_task_execution is True

"""
Testes para o driver OpenAI-compatible (Ollama/Qwen e afins).
O cliente OpenAI é sempre mockado — não há chamada de rede real.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest

from quimera.evidence import EvidenceStore
from quimera.runtime.drivers.openai_compat import (
     _MAX_TOOL_LOOP_MESSAGES,
     _MAX_TOOL_RESULT_CHARS,
     DEFAULT_MAX_CONNECTIONS,
     MAX_TOOL_HOPS_BY_RELIABILITY,
     OpenAICompatDriver,
     _prune_tool_loop_messages,
     _sanitize_assistant_text,
     _strip_thinking,
 )
from quimera.runtime.drivers.repl import (
    DriverRepl,
    _header,
    _on_tool_call,
    _on_tool_result,
    _resolve_profile_connection,
    _resolve_profile_driver,
)
from quimera.runtime.drivers.tool_schemas import TOOL_SCHEMAS, resolve_tool_schemas
from quimera.runtime.errors import ToolPolicyViolationError
from quimera.runtime.models import ToolCall, ToolResult
from quimera.profiles.base import OpenAIConnection
from quimera.prompt_templates import PromptText


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


def _prompt(text="prompt"):
    """Prompt mínimo estruturado para testes do driver."""
    return _rendered(f'<current_turn title="Pedido atual">{text}</current_turn>')


def _rendered(text="", kind="chat"):
    return PromptText(text, kind)


# ---------------------------------------------------------------------------
# Testes de tool_schemas
# ---------------------------------------------------------------------------

def test_all_schemas_have_required_fields():
    """Verifica que Test all schemas have required fields."""
    required_keys = {"type", "function"}
    function_keys = {"name", "description", "parameters"}
    for schema in TOOL_SCHEMAS:
        assert required_keys <= schema.keys(), f"Schema sem campos obrigatórios: {schema}"
        assert function_keys <= schema["function"].keys(), f"Function schema incompleto: {schema}"


def test_schema_names_match_registered_tools():
    """Verifica que Test schema names match registered tools."""
    expected = {
        "list_files", "read_file", "write_file", "replace_text", "apply_patch", "grep_search",
        "inspect_symbols", "run_shell",
        "exec_command", "write_stdin", "poll_command_session", "close_command_session", "list_tasks", "list_jobs",
        "get_job", "memory_save", "memory_retrieve", "remove_file", "web_search", "web_fetch", "delegate",
        "todo_write", "todo_list", "list_agents", "ask_user", "update_shared_state",
        "browser_start", "browser_status", "browser_close", "browser_navigate",
        "browser_snapshot", "browser_click", "browser_type", "browser_press",
        "browser_mouse", "browser_wait", "browser_evaluate", "browser_screenshot",
        "browser_console", "browser_network",
        # Git tools
        "git_status", "git_log", "git_diff", "git_branch", "git_fetch",
        "git_add", "git_commit", "git_checkout", "git_push",
    }
    actual = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert actual == expected


def test_delegate_schema_mentions_existing_profiles():
    """Descrição do delegate deve orientar para perfis existentes, não exemplos obsoletos."""
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "delegate")
    text = json.dumps(schema["function"], ensure_ascii=False)

    assert "list_agents" in text


def test_delegate_schema_includes_role_and_access_list():
    """Schema do delegate expõe role/access_list na raiz e nos steps."""
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "delegate")
    properties = schema["function"]["parameters"]["properties"]
    step_properties = properties["steps"]["items"]["properties"]

    assert properties["role"]["enum"] == ["planner", "executor", "reviewer", "verifier", "synthesizer"]
    assert properties["access_list"]["items"]["type"] == "string"
    assert step_properties["role"]["enum"] == ["planner", "executor", "reviewer", "verifier", "synthesizer"]
    assert step_properties["access_list"]["items"]["type"] == "string"


def test_resolve_tool_schemas_hides_task_tools_without_db():
    """Verifica que Test resolve tool schemas hides task tools without db."""
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
    """Verifica que Test resolve tool schemas hides blocked tools from active mode."""
    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db")
    mock_executor.policy = SimpleNamespace(blocked_tools=["run_shell", "exec_command", "apply_patch"])
    mock_executor.is_delegate_available.return_value = True
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]

    actual = {s["function"]["name"] for s in resolve_tool_schemas(mock_executor)}
    assert "run_shell" not in actual
    assert "exec_command" not in actual
    assert "apply_patch" not in actual
    assert "read_file" in actual
    assert "list_files" in actual


def test_resolve_tool_schemas_hides_delegate_when_not_bound():
    """Verifica que Test resolve tool schemas hides call agent when not bound."""
    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db")
    mock_executor.policy = SimpleNamespace(blocked_tools=[])
    mock_executor.is_delegate_available.return_value = False
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]

    actual = {s["function"]["name"] for s in resolve_tool_schemas(mock_executor)}
    assert "delegate" not in actual
    assert "list_agents" not in actual


def test_required_args_are_lists():
    """Verifica que Test required args are lists."""
    for schema in TOOL_SCHEMAS:
        params = schema["function"]["parameters"]
        assert isinstance(params.get("required"), list), (
            f"'required' deve ser lista em: {schema['function']['name']}"
        )


# ---------------------------------------------------------------------------
# Testes de _strip_thinking
# ---------------------------------------------------------------------------

def test_strip_thinking_removes_block():
    """Verifica que Test strip thinking removes block."""
    text = "<think>raciocínio interno</think>Resposta final."
    assert _strip_thinking(text) == "Resposta final."


def test_strip_thinking_multiline():
    """Verifica que Test strip thinking multiline."""
    text = "<think>\nlinha 1\nlinha 2\n</think>\nResposta."
    assert _strip_thinking(text) == "Resposta."


def test_strip_thinking_no_block():
    """Verifica que Test strip thinking no block."""
    text = "Resposta sem bloco think."
    assert _strip_thinking(text) == text


def test_strip_thinking_multiple_blocks():
    """Verifica que Test strip thinking multiple blocks."""
    text = "<think>a</think>Texto<think>b</think>Final"
    assert _strip_thinking(text) == "TextoFinal"


def test_strip_thinking_persists_evidence_before_removal(tmp_path):
    """Verifica que Test strip thinking persists evidence before removal."""
    text = "<think>raciocínio interno detalhado</think>Resposta final."

    cleaned = _strip_thinking(
        text,
        agent_name="codex",
        session_id="sessao-1",
        base_dir=tmp_path,
    )

    store = EvidenceStore(tmp_path, "sessao-1")
    try:
        evidences = store.query("sessao-1")
    finally:
        store.close()

    assert cleaned == "Resposta final."
    assert len(evidences) == 1
    assert evidences[0].type == "think_summary"
    assert evidences[0].summary == "raciocínio interno detalhado"
    assert evidences[0].agent == "codex"
    assert evidences[0].session_id == "sessao-1"


def test_sanitize_assistant_text_preserves_function_like_text():
    """Verifica que Test sanitize assistant text preserves function like text."""
    text = "<think>x</think></function>\nResposta final\n</tool_call>"
    assert _sanitize_assistant_text(text) == "</function>\nResposta final\n</tool_call>"



def test_run_sends_quimera_current_turn_as_final_user_message_to_openai_api():
    """Verifica que Test run sends quimera current turn as final user message to openai api."""
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="ok")])
    prompt = (
        '<recent_conversation title="Conversa recente">\n'
        'USER: Leia o README\n'
        '</recent_conversation>\n'
        '<current_turn title="Pedido atual de >>>">\n'
        'Liste arquivos atuais\n'
        '</current_turn>'
    )

    result = driver.run(_rendered(prompt), tool_executor=None)

    assert result == "ok"
    messages = mock_client.chat.completions.create.call_args[1]["messages"]
    assert messages[-1] == {"role": "user", "content": "Liste arquivos atuais"}
    assert "Leia o README" in messages[-2]["content"]




def test_prune_tool_loop_messages_keeps_head_and_recent_tail():
    """Verifica que Test prune tool loop messages keeps head and recent tail."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ] + [
        {"role": "tool" if i % 2 else "assistant", "content": f"m{i}"}
        for i in range(_MAX_TOOL_LOOP_MESSAGES + 6)
    ]

    pruned = _prune_tool_loop_messages(messages)

    assert len(pruned) == _MAX_TOOL_LOOP_MESSAGES
    assert pruned[:2] == messages[:2]
    assert pruned[2:] == messages[-(_MAX_TOOL_LOOP_MESSAGES - 2):]


def test_prune_tool_loop_messages_preserves_assistant_for_multi_tool_results():
    """Verifica que Test prune tool loop messages preserves assistant for multi tool results."""
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
    """Verifica que Test prune tool loop messages caps oversized final tool segment."""
    total_calls = _MAX_TOOL_LOOP_MESSAGES
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"call_{i}"} for i in range(total_calls)],
        },
    ]
    messages.extend(
        {"role": "tool", "tool_call_id": f"call_{i}", "content": '{"ok": true}'}
        for i in range(total_calls)
    )

    pruned = _prune_tool_loop_messages(messages)

    assert len(pruned) == _MAX_TOOL_LOOP_MESSAGES
    assert pruned[:2] == messages[:2]
    assert pruned[2]["role"] == "assistant"

    retained_tool_ids = [msg["tool_call_id"] for msg in pruned[3:]]
    expected_kept_count = _MAX_TOOL_LOOP_MESSAGES - 3
    expected_start = total_calls - expected_kept_count
    assert retained_tool_ids == [f"call_{i}" for i in range(expected_start, total_calls)]
    assert [call["id"] for call in pruned[2]["tool_calls"]] == retained_tool_ids


# ---------------------------------------------------------------------------
# Testes de OpenAICompatDriver.__init__
# ---------------------------------------------------------------------------

def test_driver_init_missing_openai_raises():
    """Verifica que Test driver init missing openai raises."""
    import quimera.runtime.drivers.openai_compat as mod
    with patch.object(mod, "OpenAI", None):
        with pytest.raises(ImportError, match="openai"):
            OpenAICompatDriver(model="m", base_url="http://localhost")


def test_driver_init_success():
    """Verifica que Test driver init success."""
    driver, _ = _make_driver()
    assert driver.model == "qwen3-coder:30b"


# ---------------------------------------------------------------------------
# Testes de _chat — resposta simples (sem tool calls)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Testes de _chat_streaming (sem ferramentas)
# ---------------------------------------------------------------------------

def test_chat_simple_response():
    """Verifica que Test chat simple response."""
    driver, mock_client = _make_driver()
    chunks = [_make_chunk(content="Olá "), _make_chunk(content="mundo!"), _make_chunk(content=None)]
    _setup_stream(mock_client, chunks)

    text, tool_calls = driver._chat([{"role": "user", "content": "oi"}], tools=[])
    assert text == "Olá mundo!"
    assert tool_calls == []


def test_chat_empty_choices_ignored():
    """Verifica que Test chat empty choices ignored."""
    driver, mock_client = _make_driver()
    empty_chunk = SimpleNamespace(choices=[])
    _setup_stream(mock_client, [empty_chunk, _make_chunk(content="ok")])

    text, tool_calls = driver._chat([], tools=[])
    assert text == "ok"
    assert tool_calls == []


def test_chat_no_tools_uses_streaming():
    """Verifica que Test chat no tools uses streaming."""
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="resposta")])

    driver._chat([{"role": "user", "content": "x"}], tools=[])

    call_kwargs = mock_client.chat.completions.create.call_args[1]
    assert call_kwargs["stream"] is True
    assert "tools" not in call_kwargs
    assert "tool_choice" not in call_kwargs


def test_chat_streaming_supports_structured_diff_chunks():
    """Verifica que Test chat streaming supports structured diff chunks."""
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
    """Verifica que Test chat with tools returns structured tool calls."""
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
    """Verifica que Test chat with tools invalid json returns empty dict."""
    driver, mock_client = _make_driver()
    tc = _make_tool_call("x", "run_shell", "NOT_JSON")
    mock_client.chat.completions.create.return_value = _make_non_streaming_response(
        content="", tool_calls=[tc]
    )

    _, tool_calls = driver._chat([], tools=TOOL_SCHEMAS)
    assert tool_calls[0]["arguments"] == {}


def test_chat_with_tools_no_tool_calls_in_response():
    """Verifica que Test chat with tools no tool calls in response."""
    driver, mock_client = _make_driver()
    mock_client.chat.completions.create.return_value = _make_non_streaming_response(
        content="</function>\nSó texto, sem ferramentas.", tool_calls=None
    )

    text, tool_calls = driver._chat([], tools=TOOL_SCHEMAS)
    assert text == "</function>\nSó texto, sem ferramentas."
    assert tool_calls == []


def test_chat_with_tools_ignores_textual_function_like_tool_call():
    """Verifica que Test chat with tools ignores textual function like tool call."""
    driver, mock_client = _make_driver()
    textual = '<function=read_file><parameter=path>secret.txt</function>'
    mock_client.chat.completions.create.return_value = _make_non_streaming_response(
        content=textual, tool_calls=None
    )

    text, tool_calls = driver._chat([], tools=TOOL_SCHEMAS)

    assert text == textual
    assert tool_calls == []


# ---------------------------------------------------------------------------
# Testes de _execute_tool
# ---------------------------------------------------------------------------

def test_execute_tool_success():
    """Verifica que Test execute tool success."""
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


def test_execute_tool_forwards_progress_callback():
    """Tool calls nativas devem propagar o callback de progresso para o executor."""
    driver, _ = _make_driver()
    mock_executor = MagicMock()
    mock_executor.execute.return_value = ToolResult(ok=True, tool_name="read_file", content="ok")
    progress_callback = MagicMock()

    tc = {"id": "c-progress", "name": "read_file", "arguments": {"path": "x.py"}}
    result = driver._execute_tool(tc, mock_executor, progress_callback=progress_callback)

    assert result.ok is True
    mock_executor.execute.assert_called_once()
    assert mock_executor.execute.call_args.kwargs["progress_callback"] is progress_callback


def test_execute_tool_exception_returns_error_result():
    """Verifica que Test execute tool exception returns error result."""
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
    """Verifica que Test run simple response no tools."""
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="Resposta simples")])

    result = driver.run(_prompt(), tool_executor=None)
    assert result == "Resposta simples"


def test_run_strips_thinking_block():
    """Verifica que Test run strips thinking block."""
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="<think>thinking</think>Resposta")])

    result = driver.run(_prompt(), tool_executor=None)
    assert result == "Resposta"


def test_run_preserves_function_like_text_in_final_response():
    """Verifica que Test run preserves function like text in final response."""
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="</function>Resposta final</tool_call>")])

    result = driver.run(_prompt(), tool_executor=None)
    assert result == "</function>Resposta final</tool_call>"


def test_run_tools_system_prompt_guides_tool_usage():
    """Driver injeta prompt curto de uso de ferramentas e orçamento separado."""
    driver, mock_client = _make_driver()
    mock_client.chat.completions.create.return_value = _make_non_streaming_response(
        content="ok", tool_calls=None
    )
    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(workspace_root="/tmp/workspace", db_path=None)
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]

    driver.run(_prompt(), tool_executor=mock_executor)

    messages = mock_client.chat.completions.create.call_args[1]["messages"]
    system_message = messages[0]
    budget_message = messages[1]
    assert system_message["role"] == "system"
    assert budget_message["role"] == "system"
    assert "não repita o mesmo payload inválido" in system_message["content"]
    assert "Use as ferramentas disponíveis" in system_message["content"]
    assert "Workspace raiz: /tmp/workspace." not in system_message["content"]
    assert f"max_tool_hops={MAX_TOOL_HOPS_BY_RELIABILITY['medium']}" in budget_message["content"]
    assert f"remaining_tool_hops={MAX_TOOL_HOPS_BY_RELIABILITY['medium']}" in budget_message["content"]
    tool_names = {tool["function"]["name"] for tool in mock_client.chat.completions.create.call_args[1]["tools"]}
    assert tool_names == {
        "list_files",
        "read_file",
        "write_file",
        "replace_text",
        "apply_patch",
        "grep_search",
        "inspect_symbols",
        "run_shell",
        "exec_command",
        "write_stdin",
        "poll_command_session",
        "close_command_session",
        "memory_save",
        "memory_retrieve",
        "remove_file",
        "web_search",
        "web_fetch",
        "delegate",
        "list_agents",
        "todo_write",
        "todo_list",
        "ask_user",
        "update_shared_state",
        "browser_start",
        "browser_status",
        "browser_close",
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_mouse",
        "browser_wait",
        "browser_evaluate",
        "browser_screenshot",
        "browser_console",
        "browser_network",
        # Git tools
        "git_status",
        "git_log",
        "git_diff",
        "git_branch",
        "git_fetch",
        "git_add",
        "git_commit",
        "git_checkout",
        "git_push",
    }


def test_run_returns_none_on_empty_response():
    """Verifica que Test run returns none on empty response."""
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content=None)])

    result = driver.run(_prompt(), tool_executor=None)
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

    result = driver.run(_prompt("leia o arquivo x.py"), tool_executor=mock_executor)
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

    driver.run(_prompt("liste arquivos"), tool_executor=mock_executor)

    second_call_messages = mock_client.chat.completions.create.call_args_list[1][1]["messages"]
    tool_result_msg = next(m for m in second_call_messages if m.get("role") == "tool")
    assert tool_result_msg["tool_call_id"] == tc_id
    payload = json.loads(tool_result_msg["content"])
    assert payload["ok"] is True
    assert payload["content"] == "file.py"


def test_run_tool_loop_updates_remaining_budget_each_hop():
    """Verifica que Test run tool loop updates remaining budget each hop."""
    driver, mock_client = _make_driver()

    tc_id = "call_budget"
    tc = _make_tool_call(tc_id, "run_shell", '{"command":"ls"}')
    responses = iter(
        [
            _make_non_streaming_response(content="", tool_calls=[tc]),
            _make_non_streaming_response(content="Done.", tool_calls=None),
        ]
    )
    observed_budget_prompts = []

    def side_effect(*args, **kwargs):
        messages = kwargs["messages"]
        observed_budget_prompts.append(messages[1]["content"])
        return next(responses)

    mock_client.chat.completions.create.side_effect = side_effect

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.return_value = ToolResult(ok=True, tool_name="run_shell", content="file.py")

    driver.run(_prompt("liste arquivos"), tool_executor=mock_executor)

    max_hops = MAX_TOOL_HOPS_BY_RELIABILITY["medium"]

    assert f"max_tool_hops={max_hops}" in observed_budget_prompts[0]
    assert f"remaining_tool_hops={max_hops}" in observed_budget_prompts[0]
    assert f"max_tool_hops={max_hops}" in observed_budget_prompts[1]
    assert f"remaining_tool_hops={max_hops - 1}" in observed_budget_prompts[1]


def test_run_tool_loop_uses_minimal_prompt_payload_and_valid_json():
    """Verifica que Test run tool loop uses minimal prompt payload and valid json."""
    driver, mock_client = _make_driver()
    oversized_len = _MAX_TOOL_RESULT_CHARS + 1000

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
        content="x" * oversized_len,
        error="y" * oversized_len,
        exit_code=9,
        duration_ms=12,
        data={"cwd": "/tmp/workspace"},
    )

    driver.run(_prompt("liste arquivos"), tool_executor=mock_executor)

    second_call_messages = mock_client.chat.completions.create.call_args_list[1][1]["messages"]
    tool_result_msg = next(m for m in second_call_messages if m.get("role") == "tool")
    payload = json.loads(tool_result_msg["content"])

    assert set(payload) == {"ok", "content", "error", "error_type", "hint", "truncated", "exit_code"}
    assert payload["ok"] is False
    assert payload["error_type"] == "generic"
    assert payload["hint"] is None
    assert payload["exit_code"] == 9
    assert payload["truncated"] is True
    assert f"resultado com {oversized_len} caracteres" in payload["content"]
    assert f"resultado com {oversized_len} caracteres" in payload["error"]


def test_run_tool_loop_prunes_messages_between_hops():
    """Verifica que Test run tool loop prunes messages between hops."""
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

    driver.run(_prompt("liste arquivos"), tool_executor=mock_executor)

    observed_lengths = [
        call.kwargs["messages"]
        for call in mock_client.chat.completions.create.call_args_list
    ]
    assert len(observed_lengths[-1]) <= _MAX_TOOL_LOOP_MESSAGES
    assert observed_lengths[-1][0]["role"] == "system"
    assert observed_lengths[-1][1]["role"] == "system"
    assert observed_lengths[-1][2]["role"] == "user"


def test_run_api_error_returns_none():
    """Verifica que Test run api error returns none."""
    driver, mock_client = _make_driver()
    mock_client.chat.completions.create.side_effect = RuntimeError("connection refused")

    result = driver.run(_prompt(), tool_executor=None)
    assert result is None


def test_driver_repl_probe_backend_success():
    """Verifica que Test driver repl probe backend success."""
    fake_profile = SimpleNamespace(
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
            get_profile=lambda name: fake_profile if name == "ollama-qwen" else None,
            all_profiles=lambda: [fake_profile],
        )
        repl.ensure_backend_available()


def test_driver_repl_probe_backend_unavailable_raises_clear_error():
    """Verifica que Test driver repl probe backend unavailable raises clear error."""
    fake_profile = SimpleNamespace(
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
            get_profile=lambda name: fake_profile if name == "ollama-qwen" else None,
            all_profiles=lambda: [fake_profile],
        )
        with pytest.raises(RuntimeError, match="indisponível"):
            repl.ensure_backend_available()


def test_driver_repl_format_user_prompt_formats_user_name():
    """Verifica que Test driver repl format user prompt formats user name."""
    assert DriverRepl._format_user_prompt("Alex", "execute") == "Alex: "
    assert DriverRepl._format_user_prompt("Alex>", "execute") == "Alex: "
    assert DriverRepl._format_user_prompt(">>>", "execute") == ">>> "
    assert DriverRepl._format_user_prompt("Alex", "review") == "Alex [review]: "


def test_driver_repl_run_uses_prompt_from_config_name():
    """Verifica que Test driver repl run uses prompt from config name."""
    fake_profile = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"), \
            patch.object(DriverRepl, "_load_user_name_from_config", return_value="Alex"):
        repl = DriverRepl(
            "ollama-qwen",
            get_profile=lambda name: fake_profile if name == "ollama-qwen" else None,
            all_profiles=lambda: [fake_profile],
        )

    with patch.object(repl, "ensure_backend_available"), \
            patch("builtins.input", side_effect=EOFError) as mock_input, \
            patch("builtins.print"):
        repl.run()

    mock_input.assert_called_once_with("Alex: ")


def test_driver_repl_with_input_gate_uses_gate_for_prompt_and_approval_handler():
    """Verifica que Test driver repl with input gate uses gate for prompt and approval handler."""
    fake_profile = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    gate = MagicMock(side_effect=EOFError)

    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"), \
            patch("quimera.runtime.drivers.repl.ApprovalManager") as mock_approval_handler, \
            patch.object(DriverRepl, "_load_user_name_from_config", return_value="Alex"):
        repl = DriverRepl(
            "ollama-qwen",
            get_profile=lambda name: fake_profile if name == "ollama-qwen" else None,
            all_profiles=lambda: [fake_profile],
            input_gate=gate,
        )

    first_call = mock_approval_handler.call_args_list[0]
    assert first_call[1].get("input_gate") is gate

    with patch.object(repl, "ensure_backend_available"), \
            patch("builtins.input", side_effect=AssertionError("input() não deveria ser chamado")), \
            patch("builtins.print"):
        repl.run()

    gate.assert_called_once_with("Alex: ")


def test_driver_repl_with_input_gate_executes_regular_message_flow():
    """Verifica que Test driver repl with input gate executes regular message flow."""
    fake_profile = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    gate = MagicMock(side_effect=["mensagem teste", "exit"])

    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"), \
            patch.object(DriverRepl, "_load_user_name_from_config", return_value="Alex"):
        repl = DriverRepl(
            "ollama-qwen",
            get_profile=lambda name: fake_profile if name == "ollama-qwen" else None,
            all_profiles=lambda: [fake_profile],
            input_gate=gate,
        )

    with patch.object(repl, "ensure_backend_available"), \
            patch.object(repl, "_connection_has_changed", return_value=False), \
            patch.object(repl.driver, "run", return_value="ok") as mock_run, \
            patch("builtins.print"):
        repl.run()

    mock_run.assert_called_once_with(
        "mensagem teste",
        tool_executor=repl.tool_executor,
        on_tool_call=ANY,
        on_tool_result=ANY,
    )
    assert gate.call_count == 2


def test_repl_helpers_print_and_truncate_fields():
    """Verifica que Test repl helpers print and truncate fields."""
    with patch("builtins.print") as mock_print:
        _header("Sessao")
        _on_tool_call("run_shell", {"long": "x" * 305, "short": "ok"})
        _on_tool_result(
            ToolResult(
                ok=True,
                tool_name="read_file",
                content="\n".join([f"line-{i}" for i in range(12)]),
            )
        )
        _on_tool_result(
            ToolResult(
                ok=False,
                tool_name="run_shell",
                error="e" * 420,
            )
        )

    printed = "\n".join(str(args[0]) for args, _ in mock_print.call_args_list if args)
    assert "TOOL CALL: run_shell" in printed
    assert "TOOL RESULT: read_file [✓ OK]" in printed
    assert "TOOL RESULT: run_shell [✗ ERRO]" in printed
    assert " …" in printed


def test_resolve_profile_connection_and_driver_with_fallbacks():
    """Verifica que Test resolve profile connection and driver with fallbacks."""
    profile_with_resolver = SimpleNamespace(
        effective_connection=lambda: OpenAIConnection(
            model="qwen3",
            base_url="http://localhost:11434/v1",
            api_key_env="OPENAI_API_KEY",
            provider="openai_compat",
            supports_native_tools=True,
        )
    )
    assert isinstance(_resolve_profile_connection(profile_with_resolver), OpenAIConnection)

    profile_non_cli = SimpleNamespace(
        driver="openai_compat",
        model="qwen3",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        supports_tools=False,
    )
    conn = _resolve_profile_connection(profile_non_cli)
    assert isinstance(conn, OpenAIConnection)
    assert conn.supports_native_tools is False

    profile_cli = SimpleNamespace(driver="cli")
    assert _resolve_profile_connection(profile_cli) is None

    profile_driver_resolver = SimpleNamespace(effective_driver=lambda: "openai_compat")
    assert _resolve_profile_driver(profile_driver_resolver) == "openai_compat"


def test_driver_repl_init_fails_when_profile_not_found_with_compat_list():
    """Verifica que Test driver repl init fails when profile not found with compat list."""
    compat_profile = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    with pytest.raises(ValueError, match="Profiles openai_compat disponíveis: ollama-qwen"):
        DriverRepl(
            "missing",
            get_profile=lambda _: None,
            all_profiles=lambda: [compat_profile],
        )


def test_driver_repl_init_rejects_cli_profiles():
    """Verifica que Test driver repl init rejects cli profiles."""
    cli_profile = SimpleNamespace(name="claude", driver="cli", model=None, base_url=None, api_key_env=None)
    with pytest.raises(ValueError, match="driver='cli'"):
        DriverRepl(
            "claude",
            get_profile=lambda _: cli_profile,
            all_profiles=lambda: [cli_profile],
        )


def test_driver_repl_format_user_prompt_handles_empty_name_and_mode_with_chevrons():
    """Verifica que Test driver repl format user prompt handles empty name and mode with chevrons."""
    assert DriverRepl._format_user_prompt("", "execute") == ">>> "
    assert DriverRepl._format_user_prompt(">>>", "review") == ">>> [review]: "


def test_driver_repl_load_user_name_from_config_falls_back_to_default():
    """Verifica que Test driver repl load user name from config falls back to default."""
    from quimera.config import DEFAULT_USER_NAME

    with patch("quimera.runtime.drivers.repl.find_base_writable", side_effect=RuntimeError("boom")):
        assert DriverRepl._load_user_name_from_config() == DEFAULT_USER_NAME


def test_driver_repl_connection_signature_tracks_model_url_and_api_key_env():
    """Verifica que Test driver repl connection signature tracks model url and api key env."""
    profile = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env="MY_TEST_API_KEY",
    )
    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"), \
            patch.dict("os.environ", {"MY_TEST_API_KEY": "k1"}, clear=False):
        repl = DriverRepl(
            "ollama-qwen",
            get_profile=lambda _: profile,
            all_profiles=lambda: [profile],
        )
        assert repl._connection_has_changed() is True
        assert repl._connection_has_changed() is False
        with patch.dict("os.environ", {"MY_TEST_API_KEY": "k2"}, clear=False):
            assert repl._connection_has_changed() is True


def test_driver_repl_get_current_connection_rejects_when_profile_driver_changes():
    """Verifica que Test driver repl get current connection rejects when profile driver changes."""
    profile = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"):
        repl = DriverRepl(
            "ollama-qwen",
            get_profile=lambda _: profile,
            all_profiles=lambda: [profile],
        )
    profile.driver = "cli"
    with pytest.raises(ValueError, match="driver='cli'"):
        repl._get_current_connection()


def test_driver_repl_backend_probe_handles_status_and_http_errors():
    """Verifica que Test driver repl backend probe handles status and http errors."""
    profile = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )

    bad_response = MagicMock()
    bad_response.__enter__.return_value.status = 503
    bad_response.__exit__.return_value = False

    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"), \
            patch("quimera.runtime.drivers.repl.urllib_request.urlopen", return_value=bad_response):
        repl = DriverRepl(
            "ollama-qwen",
            get_profile=lambda _: profile,
            all_profiles=lambda: [profile],
        )
        with pytest.raises(RuntimeError, match="status HTTP 503"):
            repl.ensure_backend_available()

    from urllib.error import HTTPError

    http_404 = HTTPError(
        url="http://localhost:11434/v1/models",
        code=404,
        msg="not found",
        hdrs=None,
        fp=None,
    )
    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"), \
            patch("quimera.runtime.drivers.repl.urllib_request.urlopen", side_effect=http_404):
        repl = DriverRepl(
            "ollama-qwen",
            get_profile=lambda _: profile,
            all_profiles=lambda: [profile],
        )
        repl.ensure_backend_available()

    http_500 = HTTPError(
        url="http://localhost:11434/v1/models",
        code=500,
        msg="server error",
        hdrs=None,
        fp=None,
    )
    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"), \
            patch("quimera.runtime.drivers.repl.urllib_request.urlopen", side_effect=http_500):
        repl = DriverRepl(
            "ollama-qwen",
            get_profile=lambda _: profile,
            all_profiles=lambda: [profile],
        )
        with pytest.raises(RuntimeError, match="status HTTP 500"):
            repl.ensure_backend_available()


def test_driver_repl_probe_uses_executor_toggle():
    """Verifica que Test driver repl probe uses executor toggle."""
    profile = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"):
        repl = DriverRepl(
            "ollama-qwen",
            get_profile=lambda _: profile,
            all_profiles=lambda: [profile],
        )

    with patch.object(repl, "ensure_backend_available"), \
            patch.object(repl.driver, "run", return_value="ok") as mock_run:
        assert repl.probe("prompt", use_tools=False) == "ok"
    assert mock_run.call_args.kwargs["tool_executor"] is None

    with patch.object(repl, "ensure_backend_available"), \
            patch.object(repl.driver, "run", return_value="ok") as mock_run:
        assert repl.probe("prompt", use_tools=True) == "ok"
    assert mock_run.call_args.kwargs["tool_executor"] is repl.tool_executor


def test_driver_repl_run_one_shot_and_interactive_commands():
    """Verifica que Test driver repl run one shot and interactive commands."""
    profile = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    gate = MagicMock(side_effect=[
        "   ",
        "/sem-tools",
        "msg-sem",
        "/tools",
        "/info",
        "/reload",
        "msg-normal",
        "exit",
    ])
    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"), \
            patch.object(DriverRepl, "_load_user_name_from_config", return_value="Alex"):
        repl = DriverRepl(
            "ollama-qwen",
            get_profile=lambda _: profile,
            all_profiles=lambda: [profile],
            input_gate=gate,
        )

    with patch.object(repl, "ensure_backend_available"), \
            patch.object(repl, "probe", return_value=None) as mock_probe, \
            patch("builtins.print"):
        repl.run(one_shot_prompt="hello")
    mock_probe.assert_called_once_with("hello")

    with patch.object(repl, "ensure_backend_available"), \
            patch.object(repl, "_update_driver") as mock_update_driver, \
            patch.object(repl, "_connection_has_changed", side_effect=[True, False]), \
            patch.object(repl.driver, "run", side_effect=[None, "ok"]) as mock_run, \
            patch("builtins.print") as mock_print:
        repl.run()

    assert mock_run.call_args_list[0].kwargs["tool_executor"] is None
    assert mock_run.call_args_list[1].kwargs["tool_executor"] is repl.tool_executor
    assert mock_update_driver.call_count == 2
    printed = "\n".join(str(args[0]) for args, _ in mock_print.call_args_list if args)
    assert "[sem resposta]" in printed


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

    from quimera.runtime.tool_hops import MAX_TOOL_HOPS_BY_RELIABILITY
    expected_hops = MAX_TOOL_HOPS_BY_RELIABILITY["medium"]
    driver.tool_use_reliability = "medium"
    result = driver.run(_prompt(), tool_executor=mock_executor)
    assert result is not None
    assert mock_client.chat.completions.create.call_count == expected_hops + 1


def test_run_low_reliability_uses_lower_max_hops():
    """Verifica que Test run low reliability uses lower max hops."""
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

    result = driver.run(_prompt(), tool_executor=mock_executor)
    assert result is not None
    assert mock_client.chat.completions.create.call_count == MAX_TOOL_HOPS_BY_RELIABILITY["low"] + 1


def test_run_aborts_on_repeated_policy_error_for_all_reliabilities():
    """Verifica que Test run aborts on repeated policy error for all reliabilities."""
    from quimera.runtime.tool_hops import get_invalid_tool_loop_threshold

    tc = _make_tool_call("c", "bad_tool", '{"path":"x"}')
    for reliability in ("low", "medium", "high"):
        driver, mock_client = _make_driver()
        driver.tool_use_reliability = reliability
        threshold = get_invalid_tool_loop_threshold(reliability)
        mock_client.chat.completions.create.side_effect = [
            _make_non_streaming_response(content="", tool_calls=[tc])
            for _ in range(threshold)
        ]

        mock_executor = MagicMock()
        mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
        mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
        mock_executor.execute.side_effect = [
            ToolResult(
                ok=False,
                tool_name="bad_tool",
                error=ToolPolicyViolationError(
                    "Comando fora da allowlist: bash",
                    hint="Use apenas comandos permitidos.",
                ),
            )
            for _ in range(threshold)
        ]

        result = driver.run(_prompt(), tool_executor=mock_executor)
        assert result == "Falha: loop de ferramenta inválida detectado."
        assert mock_client.chat.completions.create.call_count == threshold


def test_run_does_not_abort_on_different_policy_error_signatures():
    """Verifica que Test run does not abort on different policy error signatures."""
    driver, mock_client = _make_driver()
    tc = _make_tool_call("c", "run_shell", '{"command":"x"}')
    mock_client.chat.completions.create.side_effect = [
        _make_non_streaming_response(content="", tool_calls=[tc]),
        _make_non_streaming_response(content="", tool_calls=[tc]),
        _make_non_streaming_response(content="resposta final", tool_calls=[]),
    ]

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.side_effect = [
        ToolResult(
            ok=False,
            tool_name="run_shell",
            error=ToolPolicyViolationError("Comando bloqueado: operador de encadeamento proibido: '&&'"),
        ),
        ToolResult(
            ok=False,
            tool_name="run_shell",
            error=ToolPolicyViolationError("Comando bloqueado: operador de encadeamento proibido: ';'"),
        ),
    ]

    result = driver.run(_prompt(), tool_executor=mock_executor)
    assert result == "resposta final"
    assert mock_client.chat.completions.create.call_count == 3


def test_run_allows_same_policy_signature_before_threshold():
    """Verifica que Test run allows same policy signature before threshold."""
    from quimera.runtime.tool_hops import get_invalid_tool_loop_threshold

    driver, mock_client = _make_driver()
    driver.tool_use_reliability = "medium"
    threshold = get_invalid_tool_loop_threshold("medium")
    tc = _make_tool_call("c", "run_shell", '{"command":"x"}')
    mock_client.chat.completions.create.side_effect = [
        *[
            _make_non_streaming_response(content="", tool_calls=[tc])
            for _ in range(threshold - 1)
        ],
        _make_non_streaming_response(content="resposta final", tool_calls=[]),
    ]

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.side_effect = [
        ToolResult(
            ok=False,
            tool_name="run_shell",
            error=ToolPolicyViolationError("Comando fora da allowlist: curl"),
        )
        for _ in range(threshold - 1)
    ]

    result = driver.run(_prompt(), tool_executor=mock_executor)
    assert result == "resposta final"
    assert mock_client.chat.completions.create.call_count == threshold


def test_run_reports_tool_abort_callback():
    """Verifica que Test run reports tool abort callback."""
    from quimera.runtime.tool_hops import get_invalid_tool_loop_threshold

    driver, mock_client = _make_driver()
    driver.tool_use_reliability = "high"
    threshold = get_invalid_tool_loop_threshold("high")
    tc = _make_tool_call("c", "bad_tool", '{"path":"x"}')
    mock_client.chat.completions.create.side_effect = [
        _make_non_streaming_response(content="", tool_calls=[tc])
        for _ in range(threshold)
    ]

    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db", workspace_root="/tmp/workspace")
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]
    mock_executor.execute.side_effect = [
        ToolResult(
            ok=False,
            tool_name="bad_tool",
            error=ToolPolicyViolationError("Comando bloqueado: operador de encadeamento proibido: ';'"),
        )
        for _ in range(threshold)
    ]
    aborts = []

    driver.run(_prompt(), tool_executor=mock_executor, on_tool_abort=aborts.append)
    assert aborts == ["invalid_tool_loop"]


# ---------------------------------------------------------------------------
# Testes de AgentClient dispatch
# ---------------------------------------------------------------------------

def test_agent_client_dispatches_api_driver():
    """AgentClient.call() deve chamar _call_api() para profiles com driver != 'cli'."""
    from quimera.agents import AgentClient

    renderer = MagicMock()
    client = AgentClient(renderer)

    mock_profile = MagicMock()
    mock_profile.driver = "openai_compat"
    mock_profile.model = "qwen3-coder:30b"
    mock_profile.base_url = "http://localhost:11434/v1"
    mock_profile.api_key_env = None

    with patch("quimera.profiles.get", return_value=mock_profile):
        with patch.object(client, "_call_api", return_value="api response") as mock_api:
            result = client.call("ollama-qwen", "prompt")
            mock_api.assert_called_once()
            assert result == "api response"


def test_agent_client_passes_tool_use_reliability_to_api_driver():
    """Verifica que Test agent client passes tool use reliability to api driver."""
    from quimera.agents import AgentClient

    renderer = MagicMock()
    client = AgentClient(renderer)
    profile = SimpleNamespace(
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        tool_use_reliability="low",
        supports_tools=True,
    )

    with patch("quimera.profiles.get", return_value=profile), \
            patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls:
        mock_driver = MagicMock()
        mock_driver.run.return_value = "ok"
        mock_driver_cls.return_value = mock_driver
        result = client.call("ollama-qwen", "prompt")

    assert result == "ok"
    assert mock_driver_cls.call_args.kwargs["tool_use_reliability"] == "low"


def test_agent_client_cli_profiles_use_subprocess():
    """Profiles com driver='cli' continuam usando subprocess."""
    from quimera.agents import AgentClient

    renderer = MagicMock()
    client = AgentClient(renderer)

    mock_profile = MagicMock()
    mock_profile.driver = "cli"
    mock_profile.cmd = ["mock-agent"]
    mock_profile.prompt_as_arg = False

    with patch("quimera.profiles.get", return_value=mock_profile):
        with patch.object(client, "run", return_value="cli output") as mock_run:
            result = client.call("mock", "prompt")
            mock_run.assert_called_once()
            assert result == "cli output"


def test_agent_client_mock_profile_driver_uses_cli():
    """MagicMock sem driver explícito ainda usa o caminho CLI (isinstance check)."""
    from quimera.agents import AgentClient

    renderer = MagicMock()
    client = AgentClient(renderer)

    # Profile sem atributo driver definido explicitamente (como os mocks dos testes legados)
    mock_profile = MagicMock(spec=["name", "cmd", "prompt_as_arg"])
    mock_profile.cmd = ["echo"]
    mock_profile.prompt_as_arg = False

    with patch("quimera.profiles.get", return_value=mock_profile):
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

    result = driver.run(_prompt(), tool_executor=mock_executor, cancel_event=cancel_event)

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

    result = driver.run(_prompt(), tool_executor=None, cancel_event=cancel_event)
    # "parte2" não deve ser incluído — cancelamento detectado no início da iteração seguinte
    assert result == "parte1"


def test_run_no_cancel_event_behaves_normally():
    """Sem cancel_event, o driver funciona igual ao comportamento anterior."""
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="resposta completa")])

    result = driver.run(_prompt(), tool_executor=None, cancel_event=None)
    assert result == "resposta completa"


def test_run_cancel_event_not_set_completes_normally():
    """cancel_event fornecido mas não acionado não interfere na execução."""
    import threading
    driver, mock_client = _make_driver()
    cancel_event = threading.Event()  # nunca acionado
    _setup_stream(mock_client, [_make_chunk(content="ok")])

    result = driver.run(_prompt(), tool_executor=None, cancel_event=cancel_event)
    assert result == "ok"


# ---------------------------------------------------------------------------
# Testes de ExecutionProfile com driver de API
# ---------------------------------------------------------------------------

def test_agent_profile_api_defaults():
    """Verifica que Test agent profile api defaults."""
    from quimera.profiles.base import ExecutionProfile

    profile = ExecutionProfile(
        name="test",
        prefix="/test",
        style=("red", "Test"),
        driver="openai_compat",
        model="llama3",
        base_url="http://localhost:11434/v1",
    )
    assert profile.driver == "openai_compat"
    assert profile.model == "llama3"
    assert profile.cmd == []


def test_agent_profile_cli_defaults():
    """Verifica que Test agent profile cli defaults."""
    from quimera.profiles.base import ExecutionProfile

    profile = ExecutionProfile(
        name="test",
        prefix="/test",
        style=("red", "Test"),
        cmd=["my-cli"],
    )
    assert profile.driver == "cli"
    assert profile.model is None
    assert profile.base_url is None


# ---------------------------------------------------------------------------
# Testes de regressão: perfis existentes ainda funcionam
# ---------------------------------------------------------------------------

def test_existing_profiles_still_register():
    """Verifica que os perfis embutidos continuam registrados."""
    import quimera.profiles.claude  # noqa: F401
    import quimera.profiles.mock  # noqa: F401
    import quimera.profiles as profiles

    claude = profiles.get("claude")
    assert claude is not None
    assert claude.driver == "cli"
    assert claude.cmd == [
        "claude",
        "--permission-mode=bypassPermissions",
        "--output-format=stream-json",
        "--verbose",
        "--print",
        "--input-format=stream-json",
    ]

    mock = profiles.get("mock")
    assert mock is not None
    assert mock.driver == "cli"


# ---------------------------------------------------------------------------
# Testes de semáforo de rate-limit (fix #14)
# ---------------------------------------------------------------------------

def test_default_max_connections_is_positive():
    """DEFAULT_MAX_CONNECTIONS deve ser um inteiro positivo."""
    from quimera.runtime.drivers.openai_compat import DEFAULT_MAX_CONNECTIONS
    assert isinstance(DEFAULT_MAX_CONNECTIONS, int)
    assert DEFAULT_MAX_CONNECTIONS > 0


def test_driver_has_instance_semaphore():
    """Cada instância de OpenAICompatDriver deve ter seu próprio semáforo."""
    import threading
    driver, _ = _make_driver()
    assert hasattr(driver, "_semaphore")
    assert isinstance(driver._semaphore, threading.Semaphore)


def test_semaphore_initial_value_equals_max_connections():
    """Valor inicial do semáforo deve corresponder a max_connections passado."""
    from quimera.runtime.drivers.openai_compat import DEFAULT_MAX_CONNECTIONS
    driver, _ = _make_driver()
    assert driver._semaphore._value == DEFAULT_MAX_CONNECTIONS


def test_run_acquires_and_releases_semaphore():
    """run() deve adquirir e liberar o semáforo ao redor da chamada API."""
    from quimera.runtime.drivers.openai_compat import OpenAICompatDriver, DEFAULT_MAX_CONNECTIONS
    driver, mock_client = _make_driver()

    # Reduz o semáforo para 1 para testar contenção
    driver._semaphore = threading.Semaphore(1)

    _setup_stream(mock_client, [_make_chunk(content="ok")])

    # Semáforo deve ser adquirido durante a execução e liberado após
    assert driver._semaphore._value == 1
    result = driver.run(_prompt(), tool_executor=None)
    assert result == "ok"
    # Após run(), o semáforo deve estar liberado de volta
    assert driver._semaphore._value == 1


def test_concurrent_runs_block_at_max_connections():
    """Chamadas concorrentes além de max_connections devem esperar."""
    import threading
    import time
    from quimera.runtime.drivers.openai_compat import OpenAICompatDriver

    driver, mock_client = _make_driver(model="model-x", base_url="http://localhost:1/v1")

    # Permite apenas 1 conexão simultânea
    driver._semaphore = threading.Semaphore(1)

    # Controla quando a primeira chamada pode terminar
    first_can_finish = threading.Event()

    def slow_create(*args, **kwargs):
        first_can_finish.wait(timeout=5)
        # Retorna resposta simples fora do semaphore (já liberado)
        return _make_non_streaming_response(content="ok", tool_calls=None)

    mock_client.chat.completions.create.side_effect = slow_create
    _setup_stream(mock_client, [_make_chunk(content="ok")])

    completed = {"count": 0}
    errors = {"list": []}

    def run_in_thread(name):
        try:
            driver.run(_prompt(f"prompt-{name}"), tool_executor=None)
            completed["count"] += 1
        except Exception as e:
            errors["list"].append(e)

    # Inicia a primeira chamada (vai bloquear em slow_create até first_can_finish)
    t1 = threading.Thread(target=lambda: run_in_thread("A"))
    t1.start()

    # Inicia a segunda thread imediatamente — vai bloquear no semáforo
    t2_start = time.monotonic()
    t2 = threading.Thread(target=lambda: run_in_thread("B"))
    t2.start()

    # Espera um curto período — t2 ainda deve estar bloqueada no semáforo
    time.sleep(0.2)

    # Ainda nenhuma completou (ambas bloqueadas)
    assert completed["count"] == 0, "Nenhuma chamada deveria ter completado ainda"

    # Libera a primeira chamada
    first_can_finish.set()

    # Espera ambas completarem
    t1.join(timeout=5)
    t2.join(timeout=5)

    # Verifica que ambas completaram sem erro
    assert errors["list"] == [], f"Erros: {errors['list']}"
    assert completed["count"] == 2

    # A segunda thread levou pelo menos 0.2s (esperou no semáforo)
    elapsed = time.monotonic() - t2_start
    assert elapsed >= 0.15, f"Segunda thread não deveria ter sido bloqueada no semáforo ({elapsed:.2f}s)"


def test_semaphore_is_per_instance_not_class():
    """Cada instância deve ter seu próprio semáforo independente."""
    driver1, _ = _make_driver(model="model-a", base_url="http://localhost:1/v1")
    driver2, _ = _make_driver(model="model-b", base_url="http://localhost:2/v1")

    assert driver1._semaphore is not driver2._semaphore

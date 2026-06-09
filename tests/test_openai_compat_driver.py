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
     _build_openai_messages_from_prompt,
     _build_tool_budget_prompt,
     _build_tool_system_prompt,
     _prune_tool_loop_messages,
     _sanitize_assistant_text,
     _strip_thinking,
 )
from quimera.runtime.drivers.repl import (
    DriverRepl,
    _header,
    _on_tool_call,
    _on_tool_result,
    _resolve_plugin_connection,
    _resolve_plugin_driver,
)
from quimera.runtime.drivers.tool_schemas import TOOL_SCHEMAS, resolve_tool_schemas
from quimera.runtime.errors import ToolPolicyViolationError
from quimera.runtime.models import ToolCall, ToolResult
from quimera.plugins.base import OpenAIConnection


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
    expected = {
        "list_files", "read_file", "write_file", "apply_patch", "grep_search", "run_shell",
        "exec_command", "write_stdin", "close_command_session", "list_tasks", "list_jobs",
        "get_job", "remove_file", "web_search", "web_fetch", "call_agent",
        "todo_write", "todo_list", "list_agents",
    }
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
    mock_executor.is_call_agent_available.return_value = True
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]

    actual = {s["function"]["name"] for s in resolve_tool_schemas(mock_executor)}
    assert "run_shell" not in actual
    assert "exec_command" not in actual
    assert "apply_patch" not in actual
    assert "read_file" in actual
    assert "list_files" in actual


def test_resolve_tool_schemas_hides_call_agent_when_not_bound():
    mock_executor = MagicMock()
    mock_executor.config = SimpleNamespace(db_path="/tmp/tasks.db")
    mock_executor.policy = SimpleNamespace(blocked_tools=[])
    mock_executor.is_call_agent_available.return_value = False
    mock_executor.registry.names.return_value = [s["function"]["name"] for s in TOOL_SCHEMAS]

    actual = {s["function"]["name"] for s in resolve_tool_schemas(mock_executor)}
    assert "call_agent" not in actual


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


def test_strip_thinking_persists_evidence_before_removal(tmp_path):
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
    text = "<think>x</think></function>\nResposta final\n</tool_call>"
    assert _sanitize_assistant_text(text) == "</function>\nResposta final\n</tool_call>"


def test_build_openai_messages_from_prompt_uses_current_turn_as_active_user_message():
    prompt = (
        '<rules title="Suas regras">contexto</rules>\n'
        '<recent_conversation title="Conversa recente">\n'
        'USER: Leia o README\nASSISTANT: já li\n'
        '</recent_conversation>\n'
        '<current_turn title="Pedido atual de >>>">\n'
        'Execute pwd via shell usando MCP\n'
        '</current_turn>'
    )

    messages = _build_openai_messages_from_prompt(prompt)

    assert messages[-1] == {"role": "user", "content": "Execute pwd via shell usando MCP"}
    assert all(message["role"] == "system" for message in messages[:-1])
    assert "Leia o README" in messages[-2]["content"]
    assert "Execute pwd" not in messages[0]["content"]



def test_build_openai_messages_keeps_current_turn_last_with_embedded_xml():
    prompt = (
        '<recent_conversation title="Conversa recente">\n'
        'Histórico anterior\n'
        '</recent_conversation>\n'
        '<current_turn title="Pedido atual">\n'
        'Analise este HTML/XML:\n'
        '```html\n'
        '<section>\n'
        '<recent_conversation>não é histórico</recent_conversation>\n'
        '</section>\n'
        '```\n'
        '</current_turn>\n'
    )

    messages = _build_openai_messages_from_prompt(prompt)

    assert messages[-1]["role"] == "user"
    assert "Analise este HTML/XML" in messages[-1]["content"]
    assert "<recent_conversation>não é histórico</recent_conversation>" in messages[-1]["content"]
    assert messages[-1]["content"].count("não é histórico") == 1


def test_build_openai_messages_keeps_current_turn_last_when_metrics_follow():
    prompt = (
        '<header title="Identificação">contexto</header>\n'
        '<current_turn>pedido atual</current_turn>\n'
        '<agent_metrics>métricas</agent_metrics>'
    )

    messages = _build_openai_messages_from_prompt(prompt)

    assert messages[-1] == {"role": "user", "content": "pedido atual"}
    assert all(message["role"] == "system" for message in messages[:-1])
    assert "métricas" in messages[-2]["content"]


def test_run_sends_quimera_current_turn_as_final_user_message_to_openai_api():
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

    result = driver.run(prompt, tool_executor=None)

    assert result == "ok"
    messages = mock_client.chat.completions.create.call_args[1]["messages"]
    assert messages[-1] == {"role": "user", "content": "Liste arquivos atuais"}
    assert "Leia o README" in messages[-2]["content"]



def test_build_openai_messages_uses_short_operational_context_title_for_free_text():
    messages = _build_openai_messages_from_prompt('texto solto\n<header title="H">\nctx\n</header>')

    assert messages[0]["content"] == "Contexto operacional\n\ntexto solto"


def test_build_openai_messages_uses_plain_titles_without_instructional_text():
    prompt = (
        '<header title="Identificação">\nVocê é OPENAI.\n</header>\n'
        '<recent_conversation title="Conversa recente">\nUSER: ação antiga\n</recent_conversation>\n'
        '<current_turn title="Pedido atual de >>>">\nAção atual\n</current_turn>'
    )

    messages = _build_openai_messages_from_prompt(prompt)

    assert messages[0]["content"] == '<header title="Identificação">\n\nVocê é OPENAI.'
    assert messages[1]["content"] == "Conversa recente\n\nUSER: ação antiga"
    assert "Não trate este bloco" not in messages[0]["content"]
    assert "Use para evitar duplicação" not in messages[1]["content"]

def test_build_openai_messages_maps_task_reviewer_rules_to_system_and_material_to_user():
    prompt = (
        '<header title="Task Reviewer">\nVocê é OPENAI.\n</header>\n'
        '<task_review_rules title="Critério de review">\n'
        '- Responda com ACEITE ou RETENTATIVA.\n'
        '</task_review_rules>\n'
        '<task_review title="Material para validação">\n'
        'TASK:\nValidar execução\n'
        '</task_review>'
    )

    messages = _build_openai_messages_from_prompt(prompt)

    assert messages[-1] == {"role": "user", "content": "TASK:\nValidar execução"}
    assert all(message["role"] == "system" for message in messages[:-1])
    assert "Critério de review" in messages[1]["content"]
    assert "ACEITE ou RETENTATIVA" in messages[1]["content"]

def test_build_tool_system_prompt_includes_workspace_hint():
    prompt = _build_tool_system_prompt(["read_file", "apply_patch"], "/tmp/workspace")

    assert "read_file, apply_patch" in prompt
    assert "Workspace raiz: /tmp/workspace." in prompt
    assert "não invente envelopes JSON para chamadas de ferramenta" in prompt


def test_build_tool_system_prompt_avoids_unavailable_tool_guidance():
    prompt = _build_tool_system_prompt(["read_file"], "/tmp/workspace")

    assert "read_file usa 'path', não 'file_path'" in prompt
    assert "run_shell" not in prompt
    assert "exec_command" not in prompt
    assert "começar exatamente com '*** Begin Patch'" not in prompt


def test_build_tool_system_prompt_prefers_call_agent_for_delegation():
    prompt = _build_tool_system_prompt(["read_file", "call_agent"], "/tmp/workspace")

    assert "Para delegação entre agentes, use a tool `call_agent`" in prompt
    assert "use `fallback_agents` para failover sequencial" in prompt
    assert "e `handoffs` para múltiplos passos no mesmo envio" in prompt
    assert "Se precisar delegar e `call_agent` não estiver disponível" not in prompt


def test_build_tool_system_prompt_reports_limitation_without_call_agent():
    prompt = _build_tool_system_prompt(["read_file"], "/tmp/workspace")

    assert "Se precisar delegar e `call_agent` não estiver disponível" in prompt


def test_build_tool_system_prompt_includes_shell_policy_rules():
    prompt = _build_tool_system_prompt(
        ["run_shell", "exec_command"],
        "/tmp/workspace",
        shell_allowlist=["ls", "cat", "pytest"],
    )

    assert "sem operadores de encadeamento como &&, ;, ||, ` ou $()" in prompt
    assert "comandos permitidos na allowlist: cat, ls, pytest;" in prompt


def test_build_tool_budget_prompt_includes_max_and_remaining():
    prompt = _build_tool_budget_prompt(max_tool_hops=24, remaining_tool_hops=17)

    assert "max_tool_hops=24" in prompt
    assert "remaining_tool_hops=17" in prompt


def test_prune_tool_loop_messages_keeps_head_and_recent_tail():
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
    assert text == "</function>\nSó texto, sem ferramentas."
    assert tool_calls == []


def test_chat_with_tools_ignores_textual_function_like_tool_call():
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


def test_run_preserves_function_like_text_in_final_response():
    driver, mock_client = _make_driver()
    _setup_stream(mock_client, [_make_chunk(content="</function>Resposta final</tool_call>")])

    result = driver.run("prompt", tool_executor=None)
    assert result == "</function>Resposta final</tool_call>"


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
    budget_message = messages[1]
    assert system_message["role"] == "system"
    assert budget_message["role"] == "system"
    assert "descubra o alvo antes de editar" in system_message["content"]
    assert "começar exatamente com '*** Begin Patch'" in system_message["content"]
    assert "não repita o mesmo payload inválido" in system_message["content"]
    assert "não invente envelopes JSON para chamadas de ferramenta" in system_message["content"]
    assert "Para delegação entre agentes, use a tool `call_agent`" in system_message["content"]
    assert "use `fallback_agents` para failover sequencial" in system_message["content"]
    assert "read_file usa 'path', não 'file_path'" in system_message["content"]
    assert "use exatamente 'run_shell' para uma execução simples ou 'exec_command' para sessão interativa" in \
           system_message["content"]
    assert "nunca invente nomes como 'run', 'run_shell_command' ou 'execute_command'" in system_message["content"]
    assert "Workspace raiz: /tmp/workspace." in system_message["content"]
    assert f"max_tool_hops={MAX_TOOL_HOPS_BY_RELIABILITY['medium']}" in budget_message["content"]
    assert f"remaining_tool_hops={MAX_TOOL_HOPS_BY_RELIABILITY['medium']}" in budget_message["content"]
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
        "web_search",
        "web_fetch",
        "call_agent",
        "todo_write",
        "todo_list",
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


def test_run_tool_loop_updates_remaining_budget_each_hop():
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

    driver.run("liste arquivos", tool_executor=mock_executor)

    max_hops = MAX_TOOL_HOPS_BY_RELIABILITY["medium"]

    assert f"max_tool_hops={max_hops}" in observed_budget_prompts[0]
    assert f"remaining_tool_hops={max_hops}" in observed_budget_prompts[0]
    assert f"max_tool_hops={max_hops}" in observed_budget_prompts[1]
    assert f"remaining_tool_hops={max_hops - 1}" in observed_budget_prompts[1]


def test_run_tool_loop_uses_minimal_prompt_payload_and_valid_json():
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

    driver.run("liste arquivos", tool_executor=mock_executor)

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
    assert observed_lengths[-1][1]["role"] == "system"
    assert observed_lengths[-1][2]["role"] == "user"


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


def test_driver_repl_build_input_prompt_formats_user_name():
    assert DriverRepl._build_input_prompt("Alex", "execute") == "Alex: "
    assert DriverRepl._build_input_prompt("Alex>", "execute") == "Alex: "
    assert DriverRepl._build_input_prompt(">>>", "execute") == ">>> "
    assert DriverRepl._build_input_prompt("Alex", "review") == "Alex [review]: "


def test_driver_repl_run_uses_prompt_from_config_name():
    fake_plugin = SimpleNamespace(
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
            get_plugin=lambda name: fake_plugin if name == "ollama-qwen" else None,
            all_plugins=lambda: [fake_plugin],
        )

    with patch.object(repl, "ensure_backend_available"), \
            patch("builtins.input", side_effect=EOFError) as mock_input, \
            patch("builtins.print"):
        repl.run()

    mock_input.assert_called_once_with("Alex: ")


def test_driver_repl_with_input_gate_uses_gate_for_prompt_and_approval_handler():
    fake_plugin = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    gate = MagicMock(side_effect=EOFError)

    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"), \
            patch("quimera.runtime.drivers.repl.ConsoleApprovalHandler") as mock_approval_handler, \
            patch.object(DriverRepl, "_load_user_name_from_config", return_value="Alex"):
        repl = DriverRepl(
            "ollama-qwen",
            get_plugin=lambda name: fake_plugin if name == "ollama-qwen" else None,
            all_plugins=lambda: [fake_plugin],
            input_gate=gate,
        )

    mock_approval_handler.assert_called_once_with(input_gate=gate)

    with patch.object(repl, "ensure_backend_available"), \
            patch("builtins.input", side_effect=AssertionError("input() não deveria ser chamado")), \
            patch("builtins.print"):
        repl.run()

    gate.assert_called_once_with("Alex: ")


def test_driver_repl_with_input_gate_executes_regular_message_flow():
    fake_plugin = SimpleNamespace(
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
            get_plugin=lambda name: fake_plugin if name == "ollama-qwen" else None,
            all_plugins=lambda: [fake_plugin],
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


def test_resolve_plugin_connection_and_driver_with_fallbacks():
    plugin_with_resolver = SimpleNamespace(
        effective_connection=lambda: OpenAIConnection(
            model="qwen3",
            base_url="http://localhost:11434/v1",
            api_key_env="OPENAI_API_KEY",
            provider="openai_compat",
            supports_native_tools=True,
        )
    )
    assert isinstance(_resolve_plugin_connection(plugin_with_resolver), OpenAIConnection)

    plugin_non_cli = SimpleNamespace(
        driver="openai_compat",
        model="qwen3",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        supports_tools=False,
    )
    conn = _resolve_plugin_connection(plugin_non_cli)
    assert isinstance(conn, OpenAIConnection)
    assert conn.supports_native_tools is False

    plugin_cli = SimpleNamespace(driver="cli")
    assert _resolve_plugin_connection(plugin_cli) is None

    plugin_driver_resolver = SimpleNamespace(effective_driver=lambda: "openai_compat")
    assert _resolve_plugin_driver(plugin_driver_resolver) == "openai_compat"


def test_driver_repl_init_fails_when_plugin_not_found_with_compat_list():
    compat_plugin = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    with pytest.raises(ValueError, match="Plugins openai_compat disponíveis: ollama-qwen"):
        DriverRepl(
            "missing",
            get_plugin=lambda _: None,
            all_plugins=lambda: [compat_plugin],
        )


def test_driver_repl_init_rejects_cli_plugins():
    cli_plugin = SimpleNamespace(name="claude", driver="cli", model=None, base_url=None, api_key_env=None)
    with pytest.raises(ValueError, match="driver='cli'"):
        DriverRepl(
            "claude",
            get_plugin=lambda _: cli_plugin,
            all_plugins=lambda: [cli_plugin],
        )


def test_driver_repl_build_input_prompt_handles_empty_name_and_mode_with_chevrons():
    assert DriverRepl._build_input_prompt("", "execute") == ">>> "
    assert DriverRepl._build_input_prompt(">>>", "review") == ">>> [review]: "


def test_driver_repl_load_user_name_from_config_falls_back_to_default():
    from quimera.config import DEFAULT_USER_NAME

    with patch("quimera.runtime.drivers.repl.find_base_writable", side_effect=RuntimeError("boom")):
        assert DriverRepl._load_user_name_from_config() == DEFAULT_USER_NAME


def test_driver_repl_connection_signature_tracks_model_url_and_api_key_env():
    plugin = SimpleNamespace(
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
            get_plugin=lambda _: plugin,
            all_plugins=lambda: [plugin],
        )
        assert repl._connection_has_changed() is True
        assert repl._connection_has_changed() is False
        with patch.dict("os.environ", {"MY_TEST_API_KEY": "k2"}, clear=False):
            assert repl._connection_has_changed() is True


def test_driver_repl_get_current_connection_rejects_when_plugin_driver_changes():
    plugin = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"):
        repl = DriverRepl(
            "ollama-qwen",
            get_plugin=lambda _: plugin,
            all_plugins=lambda: [plugin],
        )
    plugin.driver = "cli"
    with pytest.raises(ValueError, match="driver='cli'"):
        repl._get_current_connection()


def test_driver_repl_backend_probe_handles_status_and_http_errors():
    plugin = SimpleNamespace(
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
            get_plugin=lambda _: plugin,
            all_plugins=lambda: [plugin],
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
            get_plugin=lambda _: plugin,
            all_plugins=lambda: [plugin],
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
            get_plugin=lambda _: plugin,
            all_plugins=lambda: [plugin],
        )
        with pytest.raises(RuntimeError, match="status HTTP 500"):
            repl.ensure_backend_available()


def test_driver_repl_probe_uses_executor_toggle():
    plugin = SimpleNamespace(
        name="ollama-qwen",
        driver="openai_compat",
        model="qwen3-coder:30b",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
    )
    with patch("quimera.runtime.drivers.repl.OpenAICompatDriver"):
        repl = DriverRepl(
            "ollama-qwen",
            get_plugin=lambda _: plugin,
            all_plugins=lambda: [plugin],
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
    plugin = SimpleNamespace(
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
            get_plugin=lambda _: plugin,
            all_plugins=lambda: [plugin],
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
    result = driver.run("prompt", tool_executor=mock_executor)
    assert result is not None
    assert mock_client.chat.completions.create.call_count == expected_hops + 1


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


def test_run_aborts_on_repeated_policy_error_for_all_reliabilities():
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

        result = driver.run("prompt", tool_executor=mock_executor)
        assert result == "Falha: loop de ferramenta inválida detectado."
        assert mock_client.chat.completions.create.call_count == threshold


def test_run_does_not_abort_on_different_policy_error_signatures():
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

    result = driver.run("prompt", tool_executor=mock_executor)
    assert result == "resposta final"
    assert mock_client.chat.completions.create.call_count == 3


def test_run_allows_same_policy_signature_before_threshold():
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

    result = driver.run("prompt", tool_executor=mock_executor)
    assert result == "resposta final"
    assert mock_client.chat.completions.create.call_count == threshold


def test_run_reports_tool_abort_callback():
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
    assert claude.cmd == ["claude", "--permission-mode=dontAsk", "--output-format=stream-json", "--verbose",
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
    result = driver.run("prompt", tool_executor=None)
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
            driver.run(f"prompt-{name}", tool_executor=None)
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

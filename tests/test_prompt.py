from unittest.mock import MagicMock
import re

from quimera.context import ContextManager
from quimera.prompt import PromptBuilder
from quimera.prompt_templates import PromptTemplate


def _extract_block(prompt: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*>\n?(.*?)\n?</{tag}>", prompt, re.DOTALL)
    assert match, f"Bloco <{tag}> não encontrado no prompt"
    return match.group(1)


def _make_context_manager(content: str) -> MagicMock:
    mock_context_manager = MagicMock(spec=ContextManager)
    mock_context_manager.load.return_value = content
    return mock_context_manager


def _build_prompt_template_fixture() -> str:
    blocks = [
        "base rules inline|{agent}|{user_name}"
        "<!-- IF:tools -->|{tools}<!-- ENDIF:tools -->"
        "<!-- NOT_IF:tools -->|no-tools<!-- ENDNOT_IF:tools -->",
    ]
    return "\n\n".join(blocks)


def test_final_prompt_contract_has_sections_once_in_order_and_without_duplication():
    session_state = {
        "session_id": "test-session",
        "current_job_id": 123,
        "workspace_root": "/tmp/test",
        "current_dir": ".",
    }

    builder = PromptBuilder(
        context_manager=_make_context_manager("Persistent context content"),
        session_state=session_state,
        user_name="ALEX",
        active_agents=["claude", "codex", "gemini"],
    )

    history = [
        {"role": "human", "content": "Contexto inicial"},
        {"role": "claude", "content": "Resposta anterior"},
        {"role": "gemini", "content": "Outro ponto relevante"},
        {"role": "human", "content": "Revise os testes finais"},
    ]

    prompt = builder.build(
        agent="codex",
        history=history,
        handoff="Revise apenas os testes do prompt.",
        shared_state={
            "working_dir": "/tmp/test",
            "workspace_root": "/tmp/test",
            "ignored_internal_note": "não deve aparecer",
        },
    )

    ordered_sections = [
        '<header title="Identificação">',
        '<session_state title="Estado da sessão">',
        '<rules title="Suas regras">',
        '<tools title="Ferramentas disponíveis">',
        '<persistent_context title="Contexto persistente do workspace">',
        '<current_turn title="Pedido atual de ALEX">',
        '<recent_agent_messages title="Mensagens recentes de outros agentes">',
        '<shared_state title="Estado compartilhado">',
        '<handoff title="Mensagem direta do outro agente">',
        '<recent_conversation title="Conversa recente">',
    ]

    last_position = -1
    for section in ordered_sections:
        assert prompt.count(section) == 1
        position = prompt.index(section)
        assert position > last_position
        last_position = position

    assert "Persistent context content" in prompt
    assert "Revise os testes finais" in _extract_block(prompt, "current_turn")
    facts_block = _extract_block(prompt, "recent_agent_messages")
    assert "[CLAUDE] Resposta anterior" in facts_block
    assert "[GEMINI] Outro ponto relevante" in facts_block

    shared_state_block = _extract_block(prompt, "shared_state")
    assert '"working_dir": "/tmp/test"' in shared_state_block
    assert '"workspace_root": "/tmp/test"' in shared_state_block
    assert "ignored_internal_note" not in shared_state_block

    handoff_block = _extract_block(prompt, "handoff")
    assert "Revise apenas os testes do prompt." in handoff_block

    conversation_block = _extract_block(prompt, "recent_conversation")
    assert "[ALEX]: Contexto inicial" in conversation_block
    assert "[ALEX]: Revise os testes finais" not in conversation_block
    assert "[CLAUDE]: Resposta anterior" not in conversation_block
    assert "[GEMINI]: Outro ponto relevante" not in conversation_block
    assert "\n\n\n" not in prompt


def test_prompt_template_loads_file_lazily(tmp_path):
    template_path = tmp_path / "prompt.md"
    template = PromptTemplate(template_path)

    template_path.write_text(_build_prompt_template_fixture(), encoding="utf-8")

    assert template.render(agent="CODEX", user_name="ALEX") == "base rules inline|CODEX|ALEX|no-tools"
    assert template.render(agent="CODEX", user_name="ALEX", tools="TOOLS") == "base rules inline|CODEX|ALEX|TOOLS"


def test_prompt_no_tools():
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [{"role": "human", "content": "test"}]

    prompt = builder.build(agent="claude", history=history, skip_tool_prompt=True)

    assert '<tools title="Ferramentas disponíveis">' not in prompt
    assert "</tools>" not in prompt
    assert '<current_turn title="Pedido atual de VOCÊ">' in prompt
    assert '<recent_conversation title="Conversa recente">' in prompt
    assert '<response_prefix title="Prefixo de resposta">' not in prompt
    assert "\n\n\n" not in prompt


def test_prompt_primary_false_omits_only_session_state():
    session_state = {"session_id": "test"}
    builder = PromptBuilder(context_manager=_make_context_manager("Contexto"), session_state=session_state)
    history = [{"role": "human", "content": "test"}]

    prompt_primary = builder.build(agent="claude", history=history)
    assert '<session_state' in prompt_primary

    prompt_secondary = builder.build(agent="claude", history=history, primary=False)
    assert '<session_state title="Estado da sessão">' not in prompt_secondary
    assert '<persistent_context title="Contexto persistente do workspace">' in prompt_secondary
    assert '<current_turn title="Pedido atual de VOCÊ">' in prompt_secondary


def test_prompt_shared_state():
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [{"role": "human", "content": "test"}]
    shared_state = {
        "working_dir": "/home/user",
        "workspace_root": "/home/user/project",
    }

    prompt = builder.build(agent="claude", history=history, shared_state=shared_state)

    assert '<shared_state title="Estado compartilhado">' in prompt
    assert '"working_dir": "/home/user"' in prompt
    assert '"workspace_root": "/home/user/project"' in prompt


def test_prompt_keeps_empty_optional_blocks_in_output():
    builder = PromptBuilder(context_manager=_make_context_manager(""))

    prompt = builder.build(agent="claude", history=[])

    assert '<current_turn title="Pedido atual de VOCÊ">' in prompt
    assert "[sem pedido atual]" in prompt
    assert '<recent_agent_messages title="Mensagens recentes de outros agentes">' not in prompt
    assert '<shared_state title="Estado compartilhado">' not in prompt
    assert '<completed_tasks title="Tarefas concluídas">' not in prompt
    assert '<handoff title="Mensagem direta do outro agente">' not in prompt
    assert '<agent_metrics title="Suas métricas (apenas referência)">' not in prompt


def test_prompt_completed_tasks():
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [{"role": "human", "content": "test"}]
    shared_state = {
        "task_overview": {"job_id": 1},
        "completed_task_results": "Task 1: Success\nTask 2: Success",
    }

    prompt = builder.build(agent="claude", history=history, shared_state=shared_state)

    assert '<completed_tasks title="Tarefas concluídas">' in prompt
    assert 'Task 1: Success' in prompt


def test_prompt_keeps_infra_shared_state_visible_even_with_goal_canonical():
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [{"role": "human", "content": "test"}]

    prompt = builder.build(
        agent="claude",
        history=history,
        shared_state={
            "goal_canonical": "Objetivo legado",
            "task_overview": {"job_id": 7},
        },
    )

    assert '<shared_state title="Estado compartilhado">' in prompt
    assert '"task_overview": {' in prompt
    assert '"job_id": 7' in prompt
    assert '<goal_lock title="Objetivo fixo (imutável)">' not in prompt


def test_safe_format_replaces_missing_keys_with_empty_string(tmp_path):
    """_SafeDict.__missing__ deve retornar '' para chaves não fornecidas."""
    template_path = tmp_path / "prompt.md"
    template_path.write_text("hello {name} and {missing_key}", encoding="utf-8")
    template = PromptTemplate(template_path)

    result = template.render(name="world")

    assert "world" in result
    assert "{missing_key}" not in result
    assert result == "hello world and"


def test_collect_recent_facts_skips_empty_content():
    """Mensagens com content vazio não devem aparecer no bloco de fatos recentes."""
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [
        {"role": "claude", "content": ""},
        {"role": "claude", "content": "  "},
        {"role": "human", "content": "pergunta"},
        {"role": "codex", "content": "resposta válida"},
    ]

    prompt = builder.build(agent="outro", history=history)

    facts_block = _extract_block(prompt, "recent_agent_messages")
    assert "resposta válida" in facts_block
    assert "[CLAUDE]" not in facts_block


def test_collect_recent_facts_respects_max_items():
    """Bloco de fatos recentes deve ser limitado a max_items (padrão 4)."""
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [
        {"role": "agent1", "content": f"mensagem {i}"}
        for i in range(10)
    ] + [{"role": "human", "content": "última pergunta"}]

    prompt = builder.build(agent="claude", history=history)

    facts_block = _extract_block(prompt, "recent_agent_messages")
    count = facts_block.count("[AGENT1]")
    assert count <= 4


def test_build_conversation_block_skips_empty_content():
    """Mensagens com content vazio não devem aparecer na conversa recente."""
    builder = PromptBuilder(context_manager=_make_context_manager(""), user_name="ALEX")
    history = [
        {"role": "human", "content": ""},
        {"role": "human", "content": "  "},
        {"role": "human", "content": "pergunta anterior válida"},
        {"role": "claude", "content": "resposta"},
        {"role": "human", "content": "pergunta atual"},
    ]

    prompt = builder.build(agent="codex", history=history)

    conversation_block = _extract_block(prompt, "recent_conversation")
    lines = [l for l in conversation_block.splitlines() if "[ALEX]" in l]
    assert len(lines) == 1
    assert "pergunta anterior válida" in lines[0]

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
    sections = {
        "BASE_RULES": "Base rules",
        "GOAL_EXECUTION_RULES": "Goal execution rules",
        "REVIEWER_RULE": "Reviewer rule",
        "STATE_UPDATE_RULE": "State update rule",
        "HANDOFF_RULE": "Handoff rule",
        "TOOL_RULE": "Tool rule",
        "DEBATE_RULE": "Debate rule {marker}",
        "SHARED_STATE": "<shared_state>{shared_state_json}</shared_state>",
        "GOAL_LOCK": "Goal lock",
        "STEP_LOCK": "Step lock",
        "ACCEPTANCE_CRITERIA": "Acceptance criteria",
        "SCOPE_CONTROL": "Scope control",
        "REQUEST": "<request>{request_text}</request>",
        "FACTS": "<facts>{facts}</facts>",
    }
    blocks = ["<full>{base_rules}|{agent}|{user_name}</full>"]
    for name, content in sections.items():
        blocks.append(f"<!-- {name}:START -->\n{content}\n<!-- {name}:END -->")
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
        '<rules title="Regras">',
        '<tools title="Ferramentas disponíveis">',
        '<session_state title="Estado da sessão">',
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

    assert template.base_rules == "Base rules"
    assert template.render(agent="CODEX", user_name="ALEX") == "Base rules|CODEX|ALEX"


def test_prompt_no_tools():
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [{"role": "human", "content": "test"}]

    prompt = builder.build(agent="claude", history=history, skip_tool_prompt=True)

    assert '<tools title="Ferramentas disponíveis">' not in prompt
    assert 'Ferramentas disponíveis' not in prompt
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
    assert '<session_state' not in prompt_secondary
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


def test_prompt_completed_tasks():
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [{"role": "human", "content": "test"}]
    shared_state = {
        "goal_canonical": "Complete task",
        "completed_task_results": "Task 1: Success\nTask 2: Success",
    }

    prompt = builder.build(agent="claude", history=history, shared_state=shared_state)

    assert '<completed_tasks title="Tarefas concluídas">' in prompt
    assert 'Task 1: Success' in prompt

from unittest.mock import MagicMock
import re

from quimera.context import ContextManager
from quimera.evidence import Evidence, EvidenceStore
from quimera.modes import get_mode
from quimera.prompt import PromptBuilder
from quimera.prompt_kinds import PromptKind
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
        '<shared_state title="Estado compartilhado">',
        '<handoff title="Mensagem direta do outro agente">',
        '<recent_agent_messages title="Mensagens recentes de outros agentes (referência auxiliar — não canônico sem evidência)">',
        '<persistent_context title="Contexto persistente do workspace">',
        '<recent_conversation title="Conversa recente">',
        '<current_turn title="Pedido atual de ALEX">',
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


def test_prompt_includes_render_debug_block_when_active():
    session_state = {
        "session_id": "test-session",
        "current_job_id": 123,
        "workspace_root": "/tmp/test",
        "current_dir": ".",
        "render_debug_active": True,
        "render_log_path": "/tmp/test/data/logs/render/render.jsonl",
        "render_ansi_path": "/tmp/test/data/logs/render/render.ansi",
        "metrics_path": "/tmp/test/data/logs/metrics/test-session.jsonl",
    }

    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        session_state=session_state,
        user_name="ALEX",
    )

    prompt = builder.build(agent="codex", history=[{"role": "human", "content": "investigue o bug visual"}])

    debug_block = _extract_block(prompt, "debug_state")
    assert "Auditoria de renderização ativa nesta sessão." in debug_block
    assert "/tmp/test/data/logs/render/render.jsonl" in debug_block
    assert "/tmp/test/data/logs/render/render.ansi" in debug_block
    assert "/tmp/test/data/logs/metrics/test-session.jsonl" in debug_block


def test_prompt_omits_render_debug_block_when_inactive():
    session_state = {
        "session_id": "test-session",
        "current_job_id": 123,
        "workspace_root": "/tmp/test",
        "current_dir": ".",
        "render_debug_active": False,
    }

    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        session_state=session_state,
        user_name="ALEX",
    )

    prompt = builder.build(agent="codex", history=[{"role": "human", "content": "pedido"}])

    assert '<debug_state title="Debug de render ativo">' not in prompt


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
    assert '<current_turn title="Pedido atual de >>>">' in prompt
    assert '<recent_conversation title="Conversa recente">' in prompt
    assert '<response_prefix title="Prefixo de resposta">' not in prompt
    assert "\n\n\n" not in prompt


def test_prompt_injects_execution_mode_prompt_for_all_modes():
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [{"role": "human", "content": "test"}]
    mode_expectations = {
        "/analysis": "[MODO: ANÁLISE]",
        "/planning": "[MODO: PLANEJAMENTO]",
        "/design": "[MODO: DESIGN]",
        "/review": "[MODO: REVISÃO]",
        "/execute": "[MODO: EXECUÇÃO]",
    }

    for mode_cmd, expected_marker in mode_expectations.items():
        mode = get_mode(mode_cmd)
        prompt = builder.build(agent="claude", history=history, execution_mode=mode)
        assert expected_marker in prompt
        assert prompt.count(expected_marker) == 1
        assert '<execution_mode title="Modo de execução ativo">' in prompt


def test_prompt_without_execution_mode_does_not_inject_mode_addon():
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [{"role": "human", "content": "test"}]

    prompt = builder.build(agent="claude", history=history, execution_mode=None)

    assert '<execution_mode title="Modo de execução ativo">' not in prompt
    assert "[MODO:" not in prompt


def test_prompt_primary_false_omits_only_session_state():
    session_state = {"session_id": "test"}
    builder = PromptBuilder(context_manager=_make_context_manager("Contexto"), session_state=session_state)
    history = [{"role": "human", "content": "test"}]

    prompt_primary = builder.build(agent="claude", history=history)
    assert '<session_state' in prompt_primary

    prompt_secondary = builder.build(agent="claude", history=history, primary=False)
    assert '<session_state title="Estado da sessão">' not in prompt_secondary
    assert '<persistent_context title="Contexto persistente do workspace">' in prompt_secondary
    assert '<current_turn title="Pedido atual de >>>">' in prompt_secondary


def test_prompt_uses_current_active_agents_in_header_and_route_list():
    state = {"active_agents": ["claude", "codex", "deepseek"]}
    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        active_agents=["claude", "codex", "deepseek"],
        active_agents_provider=lambda: state["active_agents"],
    )
    history = [{"role": "human", "content": "valide o handoff"}]

    state["active_agents"] = ["codex", "deepseek"]
    prompt = builder.build(agent="codex", history=history)

    assert "Agentes de IA nesta conversa: DEEPSEEK" in prompt
    assert "- Agentes: codex, deepseek" in prompt
    assert "CLAUDE" not in prompt
    assert "claude" not in prompt


def test_handoff_prompt_uses_current_active_agents_for_route_candidates():
    state = {"active_agents": ["claude", "codex", "deepseek"]}
    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        active_agents=["claude", "codex", "deepseek"],
        active_agents_provider=lambda: state["active_agents"],
    )

    state["active_agents"] = ["codex", "deepseek"]
    prompt = builder.build(
        agent="deepseek",
        history=[],
        handoff_only=True,
        from_agent="codex",
    )

    assert "- Agentes:" not in prompt
    assert "claude" not in prompt


def test_prompt_includes_updated_handoff_contract_in_route_rules():
    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        active_agents=["claude", "codex", "deepseek"],
    )

    prompt = builder.build(agent="codex", history=[{"role": "human", "content": "delegue"}])

    assert '"handoffs"' in prompt
    assert "_pending_handoffs" in prompt
    assert "Não use `routes`, `_pending_handoffs` nem `[ROUTE:agente]`." in prompt
    assert '"metadata":{"context":"...","expected":"..."}' in prompt


def test_handoff_only_prompt_includes_updated_handoff_contract():
    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        active_agents=["claude", "codex", "deepseek"],
    )

    prompt = builder.build(
        agent="deepseek",
        history=[],
        handoff_only=True,
        from_agent="codex",
    )

    assert '"handoffs"' in prompt
    assert "_pending_handoffs" in prompt
    assert "Handoff simples:" in prompt
    assert "handoff em sequência com tarefas independentes:" in prompt


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


def test_prompt_renders_evidence_context_when_session_has_entries(tmp_path):
    store = EvidenceStore(tmp_path, "sessao-1")
    try:
        store.append(
            Evidence(
                ts="2026-05-18T20:36:11.000Z",
                path="quimera/prompt.py",
                digest="",
                type="file_read",
                agent="codex",
                session_id="sessao-1",
            )
        )
        store.append(
            Evidence(
                ts="2026-05-18T20:36:12.000Z",
                path="",
                digest="",
                type="tool_call",
                summary="exec_command: ok | cmd: rg",
                agent="codex",
                session_id="sessao-1",
            )
        )
    finally:
        store.close()

    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        session_state={
            "session_id": "sessao-1",
            "workspace_root": "/tmp/test",
            "workspace_tmp_root": str(tmp_path),
            "current_job_id": 123,
            "current_dir": ".",
        },
    )

    prompt = builder.build(
        agent="claude",
        history=[{"role": "human", "content": "continue"}],
        shared_state={"session_id": "sessao-1"},
    )

    assert '<evidence_context title="Contexto Compartilhado de Evidências">' in prompt
    assert "- quimera/prompt.py" in prompt
    assert "### Execução recente" in prompt
    assert "exec_command: ok | cmd: rg" in prompt
    assert prompt.index('<evidence_context title="Contexto Compartilhado de Evidências">') < prompt.index(
        '<recent_conversation title="Conversa recente">'
    )


def test_prompt_keeps_empty_optional_blocks_in_output():
    builder = PromptBuilder(context_manager=_make_context_manager(""))

    prompt = builder.build(agent="claude", history=[])

    assert '<current_turn title="Pedido atual de VOCÊ">' not in prompt
    assert "[sem pedido atual]" not in prompt
    assert '<recent_agent_messages title=' not in prompt
    assert '<shared_state title="Estado compartilhado">' not in prompt
    assert '<completed_tasks title="Tarefas concluídas">' not in prompt
    assert '<handoff title="Mensagem direta do outro agente">' not in prompt
    assert '<agent_metrics title="Suas métricas (apenas referência)">' not in prompt


def test_prompt_template_uses_explicit_bool_for_state_update_block(tmp_path):
    template_path = tmp_path / "prompt.md"
    template_path.write_text(
        "<!-- IF:state_update_enabled -->state<!-- ENDIF:state_update_enabled -->",
        encoding="utf-8",
    )
    template = PromptTemplate(template_path)

    assert template.render(state_update_enabled=True) == "state"
    assert template.render(state_update_enabled=False) == ""


def test_prompt_template_treats_boolean_like_strings_explicitly(tmp_path):
    template_path = tmp_path / "prompt.md"
    template_path.write_text(
        "\n".join(
            [
                "<!-- IF:enabled -->enabled<!-- ENDIF:enabled -->",
                "<!-- NOT_IF:disabled -->disabled<!-- ENDNOT_IF:disabled -->",
            ]
        ),
        encoding="utf-8",
    )
    template = PromptTemplate(template_path)

    assert template.render(enabled="1", disabled="0") == "enabled\ndisabled"
    assert template.render(enabled="true", disabled="false") == "enabled\ndisabled"
    assert template.render(enabled="yes", disabled="off") == "enabled\ndisabled"
    assert template.render(enabled="0", disabled="1") == ""


def test_prompt_template_keeps_presence_semantics_for_non_boolean_strings(tmp_path):
    template_path = tmp_path / "prompt.md"
    template_path.write_text(
        "<!-- IF:session_id -->session<!-- ENDIF:session_id -->",
        encoding="utf-8",
    )
    template = PromptTemplate(template_path)

    assert template.render(session_id="sessao-123") == "session"


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


def test_collect_recent_facts_skips_diff_like_tool_output_claims():
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [
        {"role": "codex", "content": "diff --git a/app.py b/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@"},
        {"role": "claude", "content": "Teste falhou em test_x"},
        {"role": "human", "content": "e agora?"},
    ]

    prompt = builder.build(agent="outro", history=history)

    facts_block = _extract_block(prompt, "recent_agent_messages")
    assert "diff --git" not in facts_block
    assert "Teste falhou em test_x" in facts_block


def test_collect_recent_facts_skips_protocol_control_markers():
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [
        {"role": "codex", "content": '{"type": "handoff", "route": "claude", "content": "revisar testes"}'},
        {"role": "claude", "content": "[ACK:abc123] recebido"},
        {"role": "human", "content": "qual o próximo passo?"},
    ]

    prompt = builder.build(agent="outro", history=history)

    assert '<recent_agent_messages title=' not in prompt
    conversation_block = _extract_block(prompt, "recent_conversation")
    assert "[sem itens residuais na conversa recente]" in conversation_block


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


def test_prompt_build_accepts_history_none_and_iterables():
    builder = PromptBuilder(context_manager=_make_context_manager(""))

    prompt_none = builder.build(agent="claude", history=None)
    assert "<current_turn" not in prompt_none

    tuple_history = (
        {"role": "human", "content": "pedido"},
        {"role": "codex", "content": "resposta"},
    )
    prompt_tuple = builder.build(agent="claude", history=tuple_history)
    assert '<current_turn title="Pedido atual de >>>">' in prompt_tuple
    assert "pedido" in prompt_tuple


def test_prompt_history_window_property_setter_updates_memory_selector():
    builder = PromptBuilder(context_manager=_make_context_manager(""), history_window=6)
    assert builder.history_window == 6
    builder.history_window = 2
    assert builder.history_window == 2


def test_prompt_handoff_only_filters_agent_and_from_agent_from_route_list():
    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        active_agents=["codex", "claude", "gemini"],
    )

    prompt = builder.build(
        agent="codex",
        history=[{"role": "human", "content": "faça a revisão"}],
        handoff_only=True,
        from_agent="claude",
    )

    rules_block = _extract_block(prompt, "rules")
    assert "- Agentes: gemini" in rules_block
    assert "codex" not in rules_block
    assert "claude" not in rules_block


def test_task_executor_prompt_uses_dedicated_template_without_chat_blocks():
    builder = PromptBuilder(
        context_manager=_make_context_manager("contexto persistente que não deve entrar"),
        session_state={"session_id": "sessao-1", "current_job_id": 123, "workspace_root": "/tmp/test", "current_dir": "."},
        active_agents=["codex", "claude", "gemini"],
    )

    prompt = builder.build(
        agent="codex",
        history=[
            {"role": "human", "content": "pedido geral"},
            {"role": "claude", "content": "contexto paralelo"},
        ],
        handoff={
            "handoff_id": "task-123",
            "task": "corrigir parser",
            "context": "TAREFA:\ncorrigir parser",
            "expected": "validar com pytest",
        },
        handoff_only=True,
        from_agent="claude",
        prompt_kind=PromptKind.TASK_EXECUTOR,
    )

    assert '<header title="Task Executor">' in prompt
    assert "HANDOFF_ID:\ntask-123" in prompt
    assert '<recent_conversation title="Conversa recente">' not in prompt
    assert '<current_turn title=' not in prompt
    assert '<recent_agent_messages title=' not in prompt
    assert '<persistent_context title=' not in prompt
    assert "contexto persistente que não deve entrar" not in prompt


def test_task_reviewer_prompt_uses_dedicated_template_and_review_material():
    builder = PromptBuilder(context_manager=_make_context_manager("contexto persistente"))

    prompt = builder.build(
        agent="pickle",
        history=[{"role": "human", "content": "pedido geral"}],
        handoff={
            "handoff_id": "task-review-123",
            "task": "revisar parser",
            "context": "Task original:\nparser\n\nResultado do executor:\nok",
            "expected": "ACEITE, RETENTATIVA, REPLANEJAR ou REJEITAR",
        },
        handoff_only=True,
        prompt_kind=PromptKind.TASK_REVIEWER,
    )

    assert '<header title="Task Reviewer">' in prompt
    assert "Task original:\nparser" in prompt
    assert "Resultado do executor:\nok" in prompt
    assert '<recent_conversation title="Conversa recente">' not in prompt
    assert '<current_turn title=' not in prompt
    assert '<recent_agent_messages title=' not in prompt


def test_chat_prompt_still_uses_default_template():
    builder = PromptBuilder(context_manager=_make_context_manager("Contexto"))

    prompt = builder.build(
        agent="claude",
        history=[{"role": "human", "content": "pedido"}],
        prompt_kind=PromptKind.CHAT,
    )

    assert '<rules title="Suas regras">' in prompt
    assert '<current_turn title="Pedido atual de >>>">' in prompt
    assert '<recent_conversation title="Conversa recente">' in prompt

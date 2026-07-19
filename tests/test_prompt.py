from unittest.mock import MagicMock
import re

import pytest
from rich.console import Console

from quimera.context import ContextManager
from quimera.constants import Visibility
from quimera.evidence import Evidence, EvidenceStore
from quimera.modes import get_mode
from quimera.profiles.codex import _format_codex_spy_event
from quimera.prompt import PromptBuilder
from quimera.prompt_kinds import PromptKind
from quimera.prompt_templates import PromptParser, PromptTemplate, PromptText
from quimera.ui import TerminalRenderer


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
    """Verifica que final prompt contract has sections once in order and without duplication."""
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
        delegation="Revise apenas os testes do prompt.",
        shared_state={
            "goal_canonical": "Melhorar qualidade do prompt",
            "current_step": "Remover blocos duplicados",
            "working_dir": "/tmp/test",
            "workspace_root": "/tmp/test",
            "ignored_internal_note": "não deve aparecer",
        },
    )

    ordered_sections = [
        '<header title="Identificação">',
        '<session_state title="Estado da sessão">',
        '<rules title="Suas regras">',
        '<execution_state title="Estado de execução atual">',
        '<shared_state title="Estado compartilhado">',
        '<delegation title="Mensagem direta do outro agente">',
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

    shared_state_block = _extract_block(prompt, "shared_state")
    assert '"working_dir": "/tmp/test"' in shared_state_block
    assert '"workspace_root": "/tmp/test"' in shared_state_block
    assert "ignored_internal_note" not in shared_state_block

    delegation_block = _extract_block(prompt, "delegation")
    assert "Revise apenas os testes do prompt." in delegation_block

    conversation_block = _extract_block(prompt, "recent_conversation")
    assert "[ALEX]: Contexto inicial" in conversation_block
    assert "[ALEX]: Revise os testes finais" not in conversation_block
    assert "[CLAUDE]: Resposta anterior" in conversation_block
    assert "[GEMINI]: Outro ponto relevante" in conversation_block
    assert "\n\n\n" not in prompt


def test_prompt_includes_render_debug_block_when_active():
    """Verifica que prompt includes render debug block when active."""
    session_state = {
        "session_id": "test-session",
        "current_job_id": 123,
        "workspace_root": "/tmp/test",
        "current_dir": ".",
        "render_debug_active": True,
        "render_log_path": "/tmp/test/data/logs/render/render.jsonl",
        "render_ansi_path": "/tmp/test/data/logs/render/render.ansi",
        "metrics_path": "/tmp/test/data/logs/metrics/test-session.jsonl",
        "app_log_path": "/tmp/test/data/logs/app-test-session.log",
    }

    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        session_state=session_state,
        user_name="ALEX",
    )

    prompt = builder.build(agent="codex", history=[{"role": "human", "content": "investigue o bug visual"}])

    debug_block = _extract_block(prompt, "debug_state")
    assert debug_block.startswith("Logs:")
    assert "/tmp/test/data/logs/render/render.jsonl" in debug_block
    assert "/tmp/test/data/logs/render/render.ansi" in debug_block
    assert "/tmp/test/data/logs/metrics/test-session.jsonl" in debug_block
    assert "Counter" in debug_block
    session_block = _extract_block(prompt, "session_state")
    assert "LOG DA APLICAÇÃO: /tmp/test/data/logs/app-test-session.log" in session_block


def test_prompt_omits_render_debug_block_when_inactive():
    """Verifica que prompt omits render debug block when inactive."""
    session_state = {
        "session_id": "test-session",
        "current_job_id": 123,
        "workspace_root": "/tmp/test",
        "current_dir": ".",
        "render_debug_active": False,
        "app_log_path": "/tmp/test/data/logs/app-test-session.log",
    }

    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        session_state=session_state,
        user_name="ALEX",
    )

    prompt = builder.build(agent="codex", history=[{"role": "human", "content": "pedido"}])

    assert '<debug_state title="Debug de render ativo">' not in prompt
    assert "LOG DA APLICAÇÃO:" not in prompt
    assert "/tmp/test/data/logs/app-test-session.log" not in prompt
    assert "- SISTEMA OPERACIONAL: " in prompt
    assert "- SISTEMA OPERACIONAL: \n\n</session_state>" not in prompt


def test_prompt_omits_mcp_runtime_details_when_enabled():
    """O prompt não expõe detalhes da infraestrutura MCP da sessão."""
    session_state = {
        "session_id": "test-session",
        "current_job_id": 123,
        "workspace_root": "/tmp/test",
        "current_dir": ".",
        "mcp_enabled": True,
        "mcp_socket_path": "/tmp/quimera.sock",
    }

    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        session_state=session_state,
        user_name="ALEX",
    )

    prompt = builder.build(agent="codex", history=[{"role": "human", "content": "pedido"}])
    rules_block = _extract_block(prompt, "rules")

    assert "MCP bridge" not in rules_block
    assert "servidor MCP" not in rules_block
    assert "mcp_socket_path" not in prompt
    assert "ToolExecutor" not in rules_block


def test_prompt_template_loads_file_lazily(tmp_path):
    """Verifica que prompt template loads file lazily."""
    template_path = tmp_path / "prompt.md"
    template = PromptTemplate(template_path)

    template_path.write_text(_build_prompt_template_fixture(), encoding="utf-8")

    assert template.render(agent="CODEX", user_name="ALEX") == "base rules inline|CODEX|ALEX|no-tools"
    assert template.render(agent="CODEX", user_name="ALEX", tools="TOOLS") == "base rules inline|CODEX|ALEX|TOOLS"


def test_prompt_no_tools():
    """Verifica que prompt no tools."""
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
    """Verifica que prompt injects execution mode prompt for all modes."""
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
    """Verifica que prompt without execution mode does not inject mode addon."""
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [{"role": "human", "content": "test"}]

    prompt = builder.build(agent="claude", history=history, execution_mode=None)

    assert '<execution_mode title="Modo de execução ativo">' not in prompt
    assert "[MODO:" not in prompt


def test_prompt_uses_request_override_when_latest_human_differs():
    """Verifica que prompt uses request override when latest human differs."""
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [
        {"role": "human", "content": "pedido A"},
        {"role": "claude", "content": "resposta A"},
        {"role": "human", "content": "pedido B"},
    ]

    prompt = builder.build(agent="claude", history=history, request_override="pedido A")

    current_turn = _extract_block(prompt, "current_turn")
    assert "pedido A" in current_turn
    assert "pedido B" not in current_turn


def test_prompt_primary_false_omits_only_session_state():
    """Verifica que prompt primary false omits only session state."""
    session_state = {"session_id": "test"}
    builder = PromptBuilder(context_manager=_make_context_manager("Contexto"), session_state=session_state)
    history = [{"role": "human", "content": "test"}]

    prompt_primary = builder.build(agent="claude", history=history)
    assert '<session_state' in prompt_primary

    prompt_secondary = builder.build(agent="claude", history=history, primary=False)
    assert '<session_state title="Estado da sessão">' not in prompt_secondary
    assert '<persistent_context title="Contexto persistente do workspace">' in prompt_secondary
    assert '<current_turn title="Pedido atual de >>>">' in prompt_secondary


def test_prompt_uses_current_active_agents_in_header_without_route_list():
    """Chat prompt lista agentes no header, sem regra route_agents inline."""
    state = {"active_agents": ["claude", "codex", "deepseek"]}
    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        active_agents=["claude", "codex", "deepseek"],
        active_agents_provider=lambda: state["active_agents"],
    )
    history = [{"role": "human", "content": "valide o delegation"}]

    state["active_agents"] = ["codex", "deepseek"]
    prompt = builder.build(agent="codex", history=history)

    assert "Agentes de IA nesta conversa: DEEPSEEK" in prompt
    assert "- Agentes:" not in prompt
    assert "CLAUDE" not in prompt
    assert "claude" not in prompt


def test_delegation_prompt_uses_current_active_agents_for_route_candidates():
    """Verifica que delegation prompt uses current active agents for route candidates."""
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
        delegation_only=True,
        from_agent="codex",
    )

    assert "- Agentes:" not in prompt
    assert "claude" not in prompt


def test_prompt_omits_generic_delegation_contract_from_route_rules():
    """Chat prompt não injeta contrato genérico de delegação por route_agents."""
    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        active_agents=["claude", "codex", "deepseek"],
    )

    prompt = builder.build(agent="codex", history=[{"role": "human", "content": "delegue"}])

    assert "Delegação padrão:" not in prompt
    assert "tool estruturada `delegate`" not in prompt
    assert "target_agent" not in prompt
    assert "fallback_agents" not in prompt


def test_delegation_only_prompt_includes_updated_delegation_contract():
    """Verifica que delegation only prompt includes updated delegation contract."""
    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        active_agents=["claude", "codex", "deepseek"],
    )

    prompt = builder.build(
        agent="deepseek",
        history=[],
        delegation_only=True,
        from_agent="codex",
    )

    assert "tool estruturada `delegate` via MCP" in prompt
    assert "Delegação padrão:" in prompt
    assert "fallback_agents" in prompt
    assert "steps" in prompt


def test_prompt_shared_state():
    """Verifica que prompt shared state."""
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
    """Verifica que prompt renders evidence context when session has entries."""
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


def test_prompt_evidence_pipeline_is_identical_across_compact_and_wide_tool_rendering(tmp_path):
    """Verifica que prompt evidence pipeline is identical across compact and wide tool rendering."""
    from quimera.spy_output_presenter import SpyOutputPresenter

    def _collect_evidence_section(session_id: str, width: int) -> tuple[str, str]:
        renderer = TerminalRenderer(theme="rule")
        renderer._console = Console(width=width, record=True, force_terminal=False)
        presenter = SpyOutputPresenter(
            renderer,
            Visibility.SUMMARY,
            session_id=session_id,
            base_dir=tmp_path,
        )

        for event in _format_codex_spy_event(
            '{"type":"item.started","item":{"type":"command_execution","command":"pytest -q","id":"t_21"}}'
        ):
            presenter.emit("codex", event)
        for event in _format_codex_spy_event(
            '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q","exit_code":0,"id":"t_21"}}'
        ):
            presenter.emit("codex", event)
        presenter.finalize_turn("codex", render_summary=True)
        renderer.flush()

        builder = PromptBuilder(
            context_manager=_make_context_manager(""),
            session_state={"workspace_tmp_root": str(tmp_path)},
        )
        evidence_section = builder._build_evidence_section({"session_id": session_id}, session_id)
        rendered_summary = renderer._console.export_text()
        return evidence_section, rendered_summary

    compact_evidence, compact_render = _collect_evidence_section("sessao-compact", 40)
    wide_evidence, wide_render = _collect_evidence_section("sessao-wide", 120)

    assert compact_evidence == wide_evidence
    assert "exec_command: ok | cmd: pytest -q" in compact_evidence
    assert "TOOLS: 1 chamadas" in compact_render
    assert "TOOLS: 1 chamadas" in wide_render


def test_prompt_keeps_empty_optional_blocks_in_output():
    """Verifica que prompt keeps empty optional blocks in output."""
    builder = PromptBuilder(context_manager=_make_context_manager(""))

    prompt = builder.build(agent="claude", history=[])

    assert '<current_turn title="Pedido atual de VOCÊ">' not in prompt
    assert "[sem pedido atual]" not in prompt
    assert '<recent_agent_messages title=' not in prompt
    assert '<shared_state title="Estado compartilhado">' not in prompt
    assert '<completed_tasks title="Tarefas concluídas">' not in prompt
    assert '<delegation title="Mensagem direta do outro agente">' not in prompt
    assert '<agent_metrics title="Suas métricas (apenas referência)">' not in prompt


def test_prompt_template_uses_explicit_bool_for_state_update_block(tmp_path):
    """Verifica que prompt template uses explicit bool for state update block."""
    template_path = tmp_path / "prompt.md"
    template_path.write_text(
        "<!-- IF:state_update_enabled -->state<!-- ENDIF:state_update_enabled -->",
        encoding="utf-8",
    )
    template = PromptTemplate(template_path)

    assert template.render(state_update_enabled=True) == "state"
    assert template.render(state_update_enabled=False) == ""


def test_prompt_template_treats_boolean_like_strings_explicitly(tmp_path):
    """Verifica que prompt template treats boolean like strings explicitly."""
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
    """Verifica que prompt template keeps presence semantics for non boolean strings."""
    template_path = tmp_path / "prompt.md"
    template_path.write_text(
        "<!-- IF:session_id -->session<!-- ENDIF:session_id -->",
        encoding="utf-8",
    )
    template = PromptTemplate(template_path)

    assert template.render(session_id="sessao-123") == "session"


def test_prompt_completed_tasks():
    """Verifica que prompt completed tasks."""
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
    """Verifica que prompt keeps infra shared state visible even with goal canonical."""
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


def test_prompt_parser_reads_block_with_prompt_symbol_in_title():
    """Parser lê bloco com símbolo de prompt no title."""
    prompt = (
        '<recent_conversation title="Conversa recente">\n'
        'USER: Leia o README\n'
        '</recent_conversation>\n'
        '<current_turn title="Pedido atual de >>>">\n'
        'Execute pwd via shell usando MCP\n'
        '</current_turn>'
    )

    blocks = [block for block in PromptParser(prompt).blocks if block.name == "current_turn"]
    assert blocks
    block = blocks[-1]
    current_turn = block.content
    remaining = (prompt[:block.start] + prompt[block.end:]).strip()

    assert current_turn == "Execute pwd via shell usando MCP"
    assert "Leia o README" in remaining
    assert "Execute pwd" not in remaining


def test_safe_format_replaces_missing_keys_with_empty_string(tmp_path):
    """_SafeDict.__missing__ deve retornar '' para chaves não fornecidas."""
    template_path = tmp_path / "prompt.md"
    template_path.write_text("hello {name} and {missing_key}", encoding="utf-8")
    template = PromptTemplate(template_path)

    result = template.render(name="world")

    assert "world" in result
    assert "{missing_key}" not in result
    assert result == "hello world and"


def test_conversation_block_skips_empty_content():
    """Mensagens com content vazio não devem aparecer na conversa recente."""
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [
        {"role": "claude", "content": ""},
        {"role": "claude", "content": "  "},
        {"role": "human", "content": "pergunta"},
        {"role": "codex", "content": "resposta válida"},
    ]

    prompt = builder.build(agent="outro", history=history)

    conversation_block = _extract_block(prompt, "recent_conversation")
    assert "resposta válida" in conversation_block
    assert "[ALEX]" not in conversation_block


def test_conversation_block_shows_all_messages():
    """Bloco de conversa recente mostra todas as mensagens não puladas."""
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [
        {"role": "agent1", "content": f"mensagem {i}"}
        for i in range(10)
    ] + [{"role": "human", "content": "última pergunta"}]

    prompt = builder.build(agent="claude", history=history)

    conversation_block = _extract_block(prompt, "recent_conversation")
    count = conversation_block.count("[AGENT1]")
    assert count == 10


def test_conversation_block_skips_diff_like_tool_output():
    """Verifica que conversation block skips diff like tool output."""
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [
        {"role": "codex", "content": "diff --git a/app.py b/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@"},
        {"role": "claude", "content": "Teste falhou em test_x"},
        {"role": "human", "content": "e agora?"},
    ]

    prompt = builder.build(agent="outro", history=history)

    conversation_block = _extract_block(prompt, "recent_conversation")
    assert "diff --git" not in conversation_block
    assert "Teste falhou em test_x" in conversation_block


def test_conversation_block_skips_protocol_control_markers():
    """Verifica que conversation block skips protocol control markers."""
    builder = PromptBuilder(context_manager=_make_context_manager(""))
    history = [
        {"role": "codex", "content": '{"type": "delegation", "route": "claude", "content": "revisar testes"}'},
        {"role": "claude", "content": "[ACK:abc123] recebido"},
        {"role": "human", "content": "qual o próximo passo?"},
    ]

    prompt = builder.build(agent="outro", history=history)

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
    """Verifica que prompt build accepts history none and iterables."""
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
    """Verifica que prompt history window property setter updates memory selector."""
    builder = PromptBuilder(context_manager=_make_context_manager(""), history_window=6)
    assert builder.history_window == 6
    builder.history_window = 2
    assert builder.history_window == 2


def test_prompt_delegation_only_omits_route_list():
    """Delegation-only chat prompt não injeta lista route_agents inline."""
    builder = PromptBuilder(
        context_manager=_make_context_manager(""),
        active_agents=["codex", "claude", "gemini"],
    )

    prompt = builder.build(
        agent="codex",
        history=[{"role": "human", "content": "faça a revisão"}],
        delegation_only=True,
        from_agent="claude",
    )

    rules_block = _extract_block(prompt, "rules")
    assert "- Agentes:" not in rules_block
    assert "codex" not in rules_block
    assert "claude" not in rules_block


def test_task_executor_prompt_uses_dedicated_template_without_chat_blocks():
    """Verifica que task executor prompt uses dedicated template without chat blocks."""
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
        delegation={
            "delegation_id": "task-123",
            "task": "corrigir parser",
            "context": "TAREFA:\ncorrigir parser",
            "expected": "validar com pytest",
        },
        delegation_only=True,
        from_agent="claude",
        prompt_kind=PromptKind.TASK_EXECUTOR,
    )

    assert '<header title="Task Executor">' in prompt
    assert "DELEGATION_ID:\ntask-123" in prompt
    assert '<recent_conversation title="Conversa recente">' not in prompt
    assert '<current_turn title=' not in prompt
    assert '<recent_agent_messages title=' not in prompt
    assert '<persistent_context title=' not in prompt
    assert "contexto persistente que não deve entrar" not in prompt
    assert "MCP da sessão está ativo" not in prompt
    assert "servidor MCP" not in prompt
    assert "conectividade" not in prompt


def test_task_reviewer_prompt_uses_dedicated_template_and_review_material():
    """Verifica que task reviewer prompt uses dedicated template and review material."""
    builder = PromptBuilder(context_manager=_make_context_manager("contexto persistente"))

    prompt = builder.build(
        agent="pickle",
        history=[{"role": "human", "content": "pedido geral"}],
        delegation={
            "delegation_id": "task-review-123",
            "task": "revisar parser",
            "context": "Task original:\nparser\n\nResultado do executor:\nok",
            "expected": "ACEITE, RETENTATIVA, REPLANEJAR ou REJEITAR",
        },
        delegation_only=True,
        prompt_kind=PromptKind.TASK_REVIEWER,
    )

    assert '<header title="Task Reviewer">' in prompt
    assert "Task original:\nparser" in prompt
    assert "Resultado do executor:\nok" in prompt
    assert '<recent_conversation title="Conversa recente">' not in prompt
    assert '<current_turn title=' not in prompt
    assert '<recent_agent_messages title=' not in prompt


def test_chat_prompt_still_uses_default_template():
    """Verifica que chat prompt still uses default template."""
    builder = PromptBuilder(context_manager=_make_context_manager("Contexto"))

    prompt = builder.build(
        agent="claude",
        history=[{"role": "human", "content": "pedido"}],
        prompt_kind=PromptKind.CHAT,
    )

    assert '<rules title="Suas regras">' in prompt
    assert '<current_turn title="Pedido atual de >>>">' in prompt
    assert '<recent_conversation title="Conversa recente">' in prompt


def test_prompt_parser_ignores_xml_inside_current_turn():
    """Verifica que prompt parser ignores xml inside current turn."""
    rendered = (
        '<current_turn title="Pedido atual">\n'
        'Analise este XML:\n'
        '<section>\n'
        '<current_turn>não é bloco do template</current_turn>\n'
        '</section>\n'
        '</current_turn>\n'
    )

    blocks = PromptParser(rendered).blocks

    assert [block.name for block in blocks] == ["current_turn"]
    assert "<section>" in blocks[0].content
    assert "não é bloco do template" in blocks[0].content


def test_prompt_parser_ignores_html_xml_inside_markdown_code_block():
    """Verifica que prompt parser ignores html xml inside markdown code block."""
    rendered = (
        '<current_turn title="Pedido atual">\n'
        '```html\n'
        '<html>\n'
        '<recent_conversation>não é bloco do template</recent_conversation>\n'
        '</html>\n'
        '```\n'
        '</current_turn>\n'
    )

    blocks = PromptParser(rendered).blocks

    assert [block.name for block in blocks] == ["current_turn"]
    assert "<recent_conversation>não é bloco do template</recent_conversation>" in blocks[0].content


def test_prompt_parser_reads_multiple_sequential_top_level_blocks():
    """Verifica que prompt parser reads multiple sequential top level blocks."""
    rendered = (
        '<recent_conversation title="Histórico">\n'
        'Mensagem anterior\n'
        '</recent_conversation>\n'
        '<current_turn title="Pedido atual">\n'
        'Pedido de agora\n'
        '</current_turn>\n'
        '<agent_metrics title="Métricas">\n'
        'ok\n'
        '</agent_metrics>\n'
    )

    blocks = PromptParser(rendered).blocks

    assert [block.name for block in blocks] == ["recent_conversation", "current_turn", "agent_metrics"]
    assert [block.title for block in blocks] == ["Histórico", "Pedido atual", "Métricas"]
    assert [block.content for block in blocks] == ["Mensagem anterior", "Pedido de agora", "ok"]


def test_prompt_parser_extracts_title_from_opening_tag():
    """PromptBlock expõe title parseado, sem obrigar consumidores a parsearem tag."""
    rendered = '<rules title="Suas regras">\n- Faça o certo.\n</rules>'

    blocks = PromptParser(rendered).blocks

    assert len(blocks) == 1
    assert blocks[0].name == "rules"
    assert blocks[0].opening == '<rules title="Suas regras">'
    assert blocks[0].title == "Suas regras"
    assert blocks[0].content == "- Faça o certo."


def test_prompt_parser_rejects_template_block_without_title():
    """Blocos estruturados sem title são erro de template, não fallback silencioso."""
    rendered = '<task_review_rules>\n- Validar.\n</task_review_rules>'

    with pytest.raises(ValueError, match="sem atributo title"):
        PromptParser(rendered)


def test_prompt_parser_returns_empty_list_when_no_template_blocks():
    """Verifica que prompt parser returns empty list when no template blocks."""
    rendered = "Texto solto\n<section>HTML do usuário</section>\n```xml\n<foo>bar</foo>\n```"

    assert PromptParser(rendered).blocks == ()


def test_prompt_text_is_str_with_blocks_and_kind():
    """PromptText é string real e expõe kind/blocos; concatenação é proibida."""
    rendered = '<current_turn title="Pedido atual">oi</current_turn>'
    structured = PromptText(rendered, PromptKind.CHAT)

    assert isinstance(structured, str)
    assert str(structured) == rendered
    assert structured.kind is PromptKind.CHAT
    assert structured.blocks[0].name == "current_turn"
    assert structured.blocks[0].content == "oi"

    with pytest.raises(TypeError, match="Concatenação com PromptText não é permitida"):
        _ = structured + ""

    with pytest.raises(TypeError, match="Concatenação com PromptText não é permitida"):
        _ = "prefixo\n\n" + structured

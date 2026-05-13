"""Testes unitários para os serviços extraídos do PromptBuilder (Seção 8)."""

import json
from unittest.mock import MagicMock

from quimera.memory_selector import MemorySelector
from quimera.shared_state_presenter import SharedStatePresenter
from quimera.shared_state import PROMPT_REFERENCE_KEYS, TASK_REFERENCE_KEYS
from quimera.handoff_presenter import HandoffPresenter
from quimera.execution_mode_presenter import ExecutionModePresenter
from quimera.prompt_budget import PromptBudget


class TestMemorySelector:
    def test_select_request_returns_last_human_message(self):
        selector = MemorySelector(history_window=10)
        history = [
            {"role": "human", "content": "primeira"},
            {"role": "claude", "content": "resposta"},
            {"role": "human", "content": "segunda"},
        ]
        idx, content = selector.select_request(history)
        assert idx == 2
        assert content == "segunda"

    def test_select_request_returns_none_for_empty_history(self):
        selector = MemorySelector(history_window=10)
        idx, content = selector.select_request([])
        assert idx is None
        assert content == ""

    def test_select_request_skips_empty_content(self):
        selector = MemorySelector(history_window=10)
        history = [
            {"role": "human", "content": ""},
            {"role": "human", "content": "  "},
        ]
        idx, content = selector.select_request(history)
        assert idx is None
        assert content == ""

    def test_select_facts_skips_human_and_current_agent(self):
        selector = MemorySelector(history_window=10)
        history = [
            {"role": "human", "content": "pergunta"},
            {"role": "claude", "content": "resposta claude"},
            {"role": "codex", "content": "resposta codex"},
            {"role": "human", "content": "outra pergunta"},
        ]
        indexes, facts = selector.select_facts(history, current_agent="claude")
        assert "resposta codex" in facts
        assert "resposta claude" not in facts
        assert "pergunta" not in facts

    def test_select_facts_respects_max_items(self):
        selector = MemorySelector(history_window=10)
        history = [
            {"role": "agent1", "content": f"msg {i}"}
            for i in range(10)
        ]
        indexes, facts = selector.select_facts(history, max_items=3, current_agent="claude")
        count = facts.count("[AGENT1]")
        assert count == 3

    def test_select_facts_returns_empty_for_no_candidates(self):
        selector = MemorySelector(history_window=10)
        history = [{"role": "human", "content": "pergunta"}]
        indexes, facts = selector.select_facts(history, current_agent="claude")
        assert indexes == []
        assert facts == ""

    def test_should_skip_fact_blocks_diff_markers(self):
        assert MemorySelector.should_skip_fact("diff --git a/app.py b/app.py")
        assert MemorySelector.should_skip_fact("```diff\n+ novo codigo")
        assert MemorySelector.should_skip_fact("git diff HEAD~1")
        assert not MemorySelector.should_skip_fact("resposta normal")

    def test_should_skip_fact_blocks_goal_markers(self):
        assert MemorySelector.should_skip_fact("goal_canonical: corrigir bug")
        assert MemorySelector.should_skip_fact("Objetivo fixo é resolver")

    def test_should_skip_fact_blocks_protocol_markers(self):
        assert MemorySelector.should_skip_fact("[ROUTE:codex] task: revisar parser")
        assert MemorySelector.should_skip_fact("[ACK:abc123] recebido")
        assert MemorySelector.should_skip_fact("Aguardando dados [NEEDS_INPUT]")
        assert MemorySelector.should_skip_fact("Encaminhar para debate [DEBATE]")
        assert MemorySelector.should_skip_fact("[STATE_UPDATE]{\"next_step\":\"x\"}[/STATE_UPDATE]")

    def test_build_conversation_block_skips_specified_indexes(self):
        selector = MemorySelector(history_window=10, user_name="ALEX")
        history = [
            {"role": "human", "content": "skip me"},
            {"role": "human", "content": "keep me"},
        ]
        block = selector.build_conversation_block(history, skip_indexes={0})
        assert "skip me" not in block
        assert "keep me" in block

    def test_build_conversation_block_skips_empty_content(self):
        selector = MemorySelector(history_window=10, user_name="ALEX")
        history = [
            {"role": "human", "content": ""},
            {"role": "human", "content": "  "},
            {"role": "human", "content": "valida"},
        ]
        block = selector.build_conversation_block(history)
        assert "[ALEX]: valida" in block
        assert block.count("[ALEX]") == 1

    def test_build_conversation_block_returns_placeholder_when_empty(self):
        selector = MemorySelector(history_window=10)
        block = selector.build_conversation_block([], skip_indexes=set())
        assert "[sem itens residuais na conversa recente]" in block

    def test_build_conversation_block_looks_back_for_current_agent(self):
        selector = MemorySelector(history_window=5, user_name="ALEX")
        history = [
            {"role": "claude", "content": "resposta antiga"},
            {"role": "human", "content": "pergunta"},
            {"role": "codex", "content": "resposta codex"},
        ]
        block = selector.build_conversation_block(
            history,
            skip_indexes=set(),
            current_agent="codex",
        )
        assert "[ALEX]: pergunta" in block
        assert "[CLAUDE]: resposta antiga" in block
        assert "[CODEX]: resposta codex" in block

    def test_build_conversation_block_excludes_current_agent_before_window(self):
        selector = MemorySelector(history_window=2, user_name="ALEX")
        history = [
            {"role": "codex", "content": "fora da janela"},
            {"role": "human", "content": "pergunta"},
            {"role": "claude", "content": "resposta"},
        ]
        block = selector.build_conversation_block(
            history,
            skip_indexes=set(),
            current_agent="claude",
        )
        assert "[ALEX]: pergunta" in block
        assert "fora da janela" not in block

    def test_build_conversation_block_stops_lookback_when_index_is_skipped(self):
        selector = MemorySelector(history_window=2, user_name="ALEX")
        history = [
            {"role": "codex", "content": "mensagem antes da janela"},
            {"role": "human", "content": "pedido"},
            {"role": "claude", "content": "resposta"},
        ]
        block = selector.build_conversation_block(
            history,
            skip_indexes={0},
            current_agent="codex",
        )
        assert "mensagem antes da janela" not in block

    def test_build_conversation_block_stops_lookback_when_latest_is_empty(self):
        selector = MemorySelector(history_window=2, user_name="ALEX")
        history = [
            {"role": "codex", "content": "mensagem válida mais antiga"},
            {"role": "codex", "content": "   "},
            {"role": "human", "content": "pedido"},
            {"role": "claude", "content": "resposta"},
        ]
        block = selector.build_conversation_block(
            history,
            skip_indexes=set(),
            current_agent="codex",
        )
        assert "mensagem válida mais antiga" not in block

    def test_build_conversation_block_lookback_skips_diff_and_keeps_previous_valid(self):
        selector = MemorySelector(history_window=2, user_name="ALEX")
        history = [
            {"role": "codex", "content": "mensagem válida mais antiga"},
            {"role": "codex", "content": "diff --git a/app.py b/app.py"},
            {"role": "human", "content": "pedido"},
            {"role": "claude", "content": "resposta"},
        ]
        block = selector.build_conversation_block(
            history,
            skip_indexes=set(),
            current_agent="codex",
        )
        assert "diff --git" not in block
        assert "[CODEX]: mensagem válida mais antiga" in block

    def test_display_role_uses_user_name_for_human(self):
        selector = MemorySelector(user_name="TESTADOR")
        assert selector._display_role("human") == "TESTADOR"
        assert selector._display_role("claude") == "CLAUDE"


class TestSharedStatePresenter:
    def test_trim_keeps_only_core_keys(self):
        state = {
            "goal_canonical": "objetivo",
            "current_step": "passo",
            "task_overview": {"job_id": 7},
            "internal_note": "secreto",
            "working_dir": "/tmp/worktree",
            "workspace_root": "/tmp/worktree",
            "completed_task_results": "não faz parte do task_reference",
            "random_key": "valor",
        }
        trimmed = SharedStatePresenter.trim(state)
        assert "goal_canonical" in trimmed
        assert "current_step" in trimmed
        assert "task_overview" in trimmed
        assert "working_dir" not in trimmed
        assert "workspace_root" not in trimmed
        assert "completed_task_results" not in trimmed
        assert "internal_note" not in trimmed
        assert "random_key" not in trimmed

    def test_trim_truncates_decisions(self):
        state = {"decisions": list(range(20))}
        trimmed = SharedStatePresenter.trim(state, decisions_tail=3)
        assert trimmed["decisions"] == [17, 18, 19]

    def test_trim_uses_centralized_task_reference_contract(self):
        state = {
            "goal": "corrigir parser",
            "goal_canonical": "corrigir parser legado",
            "decisions": [f"d{i}" for i in range(8)],
            "current_step": "ajustar tokenizer",
            "acceptance_criteria": ["teste verde"],
            "allowed_scope": ["parser.py"],
            "non_goals": ["refatorar CLI"],
            "out_of_scope_notes": ["não tocar UI"],
            "evidence": ["trace reproduzido"],
            "next_step": "executar pytest",
            "task_overview": {"job_id": 42},
            "working_dir": "/tmp/worktree",
            "spy_last_turn_detail": {"agent": "codex"},
        }

        trimmed = SharedStatePresenter.trim(state)

        assert set(trimmed) == TASK_REFERENCE_KEYS
        assert trimmed["decisions"] == ["d3", "d4", "d5", "d6", "d7"]
        assert "working_dir" not in trimmed
        assert "spy_last_turn_detail" not in trimmed

    def test_present_returns_empty_json_when_no_state(self):
        json_str, results = SharedStatePresenter.present(None)
        assert json_str == ""
        assert results == ""

    def test_present_filters_execution_keys(self):
        state = {
            "working_dir": "/tmp",
            "workspace_root": "/tmp/proj",
            "goal_canonical": "deve ser filtrado",
            "current_step": "tambem filtrado",
            "task_overview": {"job_id": 1},
        }
        json_str, results = SharedStatePresenter.present(state)
        assert '"working_dir": "/tmp"' in json_str
        assert '"workspace_root"' in json_str
        assert '"task_overview"' in json_str
        assert "goal_canonical" not in json_str
        assert "current_step" not in json_str
        assert results == ""

    def test_present_includes_completed_task_results(self):
        state = {
            "working_dir": "/tmp",
            "completed_task_results": "Task 1: OK",
        }
        json_str, results = SharedStatePresenter.present(state)
        assert results == "Task 1: OK"
        assert json_str

    def test_present_exposes_only_prompt_reference_keys_without_internal_leaks(self):
        state = {
            "goal_canonical": "não deve vazar no prompt principal",
            "next_step": "também não",
            "task_overview": {"job_id": 9, "recommended_action": "executar task aprovada"},
            "working_dir": "/tmp/worktree",
            "workspace_root": "/tmp/worktree",
            "spy_last_turn_detail": {"agent": "claude"},
            "completed_task_results": "[task 1] ok",
            "internal_note": "segredo",
        }

        json_str, results = SharedStatePresenter.present(state)
        payload = json.loads(json_str)

        assert set(payload) == PROMPT_REFERENCE_KEYS
        assert payload["task_overview"]["job_id"] == 9
        assert payload["working_dir"] == "/tmp/worktree"
        assert payload["workspace_root"] == "/tmp/worktree"
        assert "goal_canonical" not in payload
        assert "spy_last_turn_detail" not in payload
        assert results == "[task 1] ok"


class TestHandoffPresenter:
    def test_present_returns_empty_for_none(self):
        fields = HandoffPresenter.present(None)
        assert fields["handoff_present"] == ""
        assert all(v == "" for v in fields.values())

    def test_present_dict_handoff(self):
        handoff = {
            "task": "Corrigir bug",
            "context": "Parser quebrado",
            "expected": "Patch",
            "handoff_id": "abc123",
        }
        fields = HandoffPresenter.present(handoff, from_agent="claude")
        assert fields["handoff_present"] == "1"
        assert fields["handoff_task"] == "Corrigir bug"
        assert fields["handoff_context"] == "Parser quebrado"
        assert fields["handoff_expected"] == "Patch"
        assert fields["handoff_from"] == "claude"
        assert fields["handoff_id"] == "abc123"
        assert fields["handoff_priority"] == ""

    def test_present_priority_urgent(self):
        handoff = {"task": "Urgente", "priority": "urgent"}
        fields = HandoffPresenter.present(handoff)
        assert fields["handoff_priority"] == "URGENT"

    def test_present_priority_normal_is_empty(self):
        handoff = {"task": "Normal", "priority": "normal"}
        fields = HandoffPresenter.present(handoff)
        assert fields["handoff_priority"] == ""

    def test_present_string_handoff(self):
        fields = HandoffPresenter.present("Mensagem direta", from_agent="codex")
        assert fields["handoff_present"] == "1"
        assert fields["handoff_raw"] == "Mensagem direta"

    def test_present_dict_with_chain(self):
        handoff = {
            "task": "Revisar",
            "chain": ["claude", "codex"],
            "handoff_id": "xyz",
        }
        fields = HandoffPresenter.present(handoff, from_agent="qwen")
        assert fields["handoff_chain"] == "claude -> codex"
        assert fields["handoff_from"] == "qwen"


class TestExecutionModePresenter:
    def test_present_returns_empty_for_none(self):
        assert ExecutionModePresenter.present(None) == ""

    def test_present_returns_prompt_addon(self):
        mode = MagicMock()
        mode.prompt_addon = "[MODO: EXECUÇÃO]"
        result = ExecutionModePresenter.present(mode)
        assert result == "[MODO: EXECUÇÃO]"

    def test_present_strips_whitespace(self):
        mode = MagicMock()
        mode.prompt_addon = "  [MODO: ANÁLISE]  "
        result = ExecutionModePresenter.present(mode)
        assert result == "[MODO: ANÁLISE]"

    def test_present_returns_empty_when_prompt_addon_missing(self):
        mode = MagicMock(spec=[])  # no prompt_addon attr
        result = ExecutionModePresenter.present(mode)
        assert result == ""


class TestPromptBudget:
    def test_measure_returns_all_keys(self):
        metrics = PromptBudget.measure(full_prompt="hello world")
        assert "total_chars" in metrics
        assert "primary" in metrics
        assert "history_messages" in metrics

    def test_measure_primary_defaults_to_true(self):
        metrics = PromptBudget.measure(full_prompt="test")
        assert metrics["primary"] is True

    def test_measure_counts_chars_correctly(self):
        metrics = PromptBudget.measure(
            full_prompt="12345",
            route_agents="abc",
            history=[{"role": "human", "content": "x"}],
            history_window=12,
        )
        assert metrics["total_chars"] == 5
        assert metrics["rules_chars"] == 3
        assert metrics["history_messages"] == 1

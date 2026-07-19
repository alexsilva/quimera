"""Testes unitários para os serviços extraídos do PromptBuilder (Seção 8)."""

import json
from unittest.mock import MagicMock

from quimera.memory_selector import MemorySelector
from quimera.shared_state_presenter import SharedStatePresenter
from quimera.shared_state import PROMPT_REFERENCE_KEYS, TASK_REFERENCE_KEYS
from quimera.delegate_presenter import DelegatePresenter
from quimera.execution_mode_presenter import ExecutionModePresenter
from quimera.prompt import PromptBuilder
from quimera.prompt_budget import PromptBudget
from quimera.prompt_kinds import PromptKind


class _DummyContextManager:
    SUMMARY_MARKER = "<SUMMARY>"

    def load(self):
        return ""

    def load_session(self):
        return ""


class TestMemorySelector:
    def test_select_request_returns_last_human_message(self):
        """Verifica que select request returns last human message."""
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
        """Verifica que select request returns none for empty history."""
        selector = MemorySelector(history_window=10)
        idx, content = selector.select_request([])
        assert idx is None
        assert content == ""

    def test_select_request_skips_empty_content(self):
        """Verifica que select request skips empty content."""
        selector = MemorySelector(history_window=10)
        history = [
            {"role": "human", "content": ""},
            {"role": "human", "content": "  "},
        ]
        idx, content = selector.select_request(history)
        assert idx is None
        assert content == ""

    def test_find_request_index_returns_matching_human_message(self):
        """Verifica que find request index returns matching human message."""
        selector = MemorySelector(history_window=10)
        history = [
            {"role": "human", "content": "primeira"},
            {"role": "claude", "content": "resposta"},
            {"role": "human", "content": "segunda"},
        ]
        assert selector.find_request_index(history, "primeira") == 0
        assert selector.find_request_index(history, "segunda") == 2
        assert selector.find_request_index(history, "inexistente") is None



    def test_should_skip_fact_blocks_diff_markers(self):
        """Verifica que should skip fact blocks diff markers."""
        assert MemorySelector.should_skip_fact("diff --git a/app.py b/app.py")
        assert MemorySelector.should_skip_fact("```diff\n+ novo codigo")
        assert MemorySelector.should_skip_fact("git diff HEAD~1")
        assert not MemorySelector.should_skip_fact("resposta normal")

    def test_should_skip_fact_blocks_goal_markers(self):
        """Verifica que should skip fact blocks goal markers."""
        assert MemorySelector.should_skip_fact("goal_canonical: corrigir bug")
        assert MemorySelector.should_skip_fact("Objetivo fixo é resolver")

    def test_should_skip_fact_blocks_ack_markers(self):
        """Verifica que should skip fact blocks ACK markers."""
        assert MemorySelector.should_skip_fact("[ACK:abc123] recebido")

    def test_build_conversation_block_skips_specified_indexes(self):
        """Verifica que build conversation block skips specified indexes."""
        selector = MemorySelector(history_window=10, user_name="ALEX")
        history = [
            {"role": "human", "content": "skip me"},
            {"role": "human", "content": "keep me"},
        ]
        block = selector.build_conversation_block(history, skip_indexes={0})
        assert "skip me" not in block
        assert "keep me" in block

    def test_build_conversation_block_skips_empty_content(self):
        """Verifica que build conversation block skips empty content."""
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
        """Verifica que build conversation block returns placeholder when empty."""
        selector = MemorySelector(history_window=10)
        block = selector.build_conversation_block([], skip_indexes=set())
        assert "[sem itens residuais na conversa recente]" in block

    def test_build_conversation_block_looks_back_for_current_agent(self):
        """Verifica que build conversation block looks back for current agent."""
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
        """Verifica que build conversation block excludes current agent before window."""
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
        """Verifica que build conversation block stops lookback when index is skipped."""
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
        """Verifica que build conversation block stops lookback when latest is empty."""
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
        """Verifica que build conversation block lookback skips diff and keeps previous valid."""
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
        """Verifica que display role uses user name for human."""
        selector = MemorySelector(user_name="TESTADOR")
        assert selector._display_role("human") == "TESTADOR"
        assert selector._display_role("claude") == "CLAUDE"


class TestSharedStatePresenter:
    def test_trim_keeps_only_core_keys(self):
        """Verifica que trim keeps only core keys."""
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
        """Verifica que trim truncates decisions."""
        state = {"decisions": list(range(20))}
        trimmed = SharedStatePresenter.trim(state, decisions_tail=3)
        assert trimmed["decisions"] == [17, 18, 19]

    def test_trim_uses_centralized_task_reference_contract(self):
        """Verifica que trim uses centralized task reference contract."""
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
        """Verifica que present returns empty json when no state."""
        json_str, results = SharedStatePresenter.present(None)
        assert json_str == ""
        assert results == ""

    def test_present_filters_execution_keys(self):
        """Verifica que present filters execution keys."""
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
        """Verifica que present includes completed task results."""
        state = {
            "working_dir": "/tmp",
            "completed_task_results": "Task 1: OK",
        }
        json_str, results = SharedStatePresenter.present(state)
        assert results == "Task 1: OK"
        assert json_str

    def test_present_exposes_only_prompt_reference_keys_without_internal_leaks(self):
        """Verifica que present exposes only prompt reference keys without internal leaks."""
        state = {
            "goal_canonical": "não deve vazar no prompt principal",
            "next_step": "também não",
            "task_overview": {"job_id": 9, "recommended_action": "executar task aprovada"},
            "working_dir": "/tmp/worktree",
            "workspace_root": "/tmp/worktree",
            "spy_last_turn_detail": {"agent": "claude"},
            "completed_task_results": "[task 1] ok",
            "internal_note": "segredo",
            "agent_todos": [],
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


class TestDelegatePresenter:
    def test_present_returns_empty_for_none(self):
        """Verifica que present returns empty for none."""
        fields = DelegatePresenter.present(None)
        assert fields["delegation_present"] == ""
        assert all(v == "" for v in fields.values())

    def test_present_dict_delegation(self):
        """Verifica que present dict delegation."""
        delegation = {
            "task": "Corrigir bug",
            "context": "Parser quebrado",
            "expected": "Patch",
            "delegation_id": "abc123",
        }
        fields = DelegatePresenter.present(delegation, from_agent="claude")
        assert fields["delegation_present"] == "1"
        assert fields["delegation_request"] == "Corrigir bug"
        assert fields["delegation_context"] == "Parser quebrado"
        assert fields["delegation_expected"] == "Patch"
        assert fields["delegation_from"] == "claude"
        assert fields["delegation_id"] == "abc123"
        assert fields["delegation_priority"] == ""

    def test_present_priority_urgent(self):
        """Verifica que present priority urgent."""
        delegation = {"task": "Urgente", "priority": "urgent"}
        fields = DelegatePresenter.present(delegation)
        assert fields["delegation_priority"] == "URGENT"

    def test_present_priority_normal_is_empty(self):
        """Verifica que present priority normal is empty."""
        delegation = {"task": "Normal", "priority": "normal"}
        fields = DelegatePresenter.present(delegation)
        assert fields["delegation_priority"] == ""

    def test_present_string_delegation(self):
        """Verifica que present string delegation."""
        fields = DelegatePresenter.present("Mensagem direta", from_agent="codex")
        assert fields["delegation_present"] == "1"
        assert fields["delegation_raw"] == "Mensagem direta"

    def test_present_dict_with_chain(self):
        """Verifica que present dict with chain."""
        delegation = {
            "task": "Revisar",
            "chain": ["claude", "codex"],
            "delegation_id": "xyz",
        }
        fields = DelegatePresenter.present(delegation, from_agent="qwen")
        assert fields["delegation_chain"] == "claude -> codex"
        assert fields["delegation_from"] == "qwen"

    def test_present_dict_with_role_and_access_list(self):
        """Verifica que role e access_list são renderizados para o prompt delegado."""
        delegation = {
            "task": "Revisar",
            "role": "reviewer",
            "access_list": ["diff", "tests"],
        }
        fields = DelegatePresenter.present(delegation, from_agent="claude")

        assert fields["delegation_role"] == "revisor"
        assert "Revise" in fields["delegation_role_contract"]
        assert "não edite" in fields["delegation_role_contract"]
        assert fields["delegation_access_list"] == "- diff\n- tests"


class TestDelegationPromptRendering:
    def test_prompt_builder_omits_empty_role_and_access_list_blocks(self):
        """Prompt final legado não renderiza blocos vazios de role/access_list."""
        builder = PromptBuilder(_DummyContextManager())

        prompt = builder.build(
            "codex",
            [],
            delegation={"task": "Implementar", "context": "Arquivo alvo"},
            delegation_only=True,
            from_agent="claude",
            prompt_kind=PromptKind.TASK_EXECUTOR,
        )

        assert "TASK:\nImplementar" in prompt
        assert "CONTEXTO MÍNIMO:\nArquivo alvo" in prompt
        assert "PAPEL:" not in prompt
        assert "CONTRATO DO PAPEL:" not in prompt
        assert "ESCOPO DE CONTEXTO DECLARADO:" not in prompt
        assert "{delegation_role}" not in prompt
        assert "{delegation_access_list}" not in prompt

    def test_prompt_builder_renders_role_and_access_list_blocks(self):
        """Prompt final inclui role/access_list quando a delegação declara esses campos."""
        builder = PromptBuilder(_DummyContextManager())

        prompt = builder.build(
            "codex",
            [],
            delegation={
                "task": "Revisar",
                "role": "reviewer",
                "access_list": ["diff", "tests"],
            },
            delegation_only=True,
            from_agent="claude",
            prompt_kind=PromptKind.TASK_EXECUTOR,
        )

        assert "TASK:\nRevisar" in prompt
        assert "PAPEL:\nrevisor" in prompt
        assert "CONTRATO DO PAPEL:\nRevise, aponte riscos e não edite o código." in prompt
        assert "ESCOPO DE CONTEXTO DECLARADO:\n- diff\n- tests" in prompt


class TestExecutionModePresenter:
    def test_present_returns_empty_for_none(self):
        """Verifica que present returns empty for none."""
        assert ExecutionModePresenter.present(None) == ""

    def test_present_returns_prompt_addon(self):
        """Verifica que present returns prompt addon."""
        mode = MagicMock()
        mode.prompt_addon = "[MODO: EXECUÇÃO]"
        result = ExecutionModePresenter.present(mode)
        assert result == "[MODO: EXECUÇÃO]"

    def test_present_strips_whitespace(self):
        """Verifica que present strips whitespace."""
        mode = MagicMock()
        mode.prompt_addon = "  [MODO: ANÁLISE]  "
        result = ExecutionModePresenter.present(mode)
        assert result == "[MODO: ANÁLISE]"

    def test_present_returns_empty_when_prompt_addon_missing(self):
        """Verifica que present returns empty when prompt addon missing."""
        mode = MagicMock(spec=[])  # no prompt_addon attr
        result = ExecutionModePresenter.present(mode)
        assert result == ""


class TestPromptBudget:
    def test_measure_returns_all_keys(self):
        """Verifica que measure returns all keys."""
        metrics = PromptBudget.measure(full_prompt="hello world")
        assert "total_chars" in metrics
        assert "primary" in metrics
        assert "history_messages" in metrics

    def test_measure_primary_defaults_to_true(self):
        """Verifica que measure primary defaults to true."""
        metrics = PromptBudget.measure(full_prompt="test")
        assert metrics["primary"] is True

    def test_measure_counts_chars_correctly(self):
        """Verifica que measure counts chars correctly."""
        metrics = PromptBudget.measure(
            full_prompt="12345",
            route_agents="abc",
            history=[{"role": "human", "content": "x"}],
            history_window=12,
        )
        assert metrics["total_chars"] == 5
        assert metrics["rules_chars"] == 3
        assert metrics["history_messages"] == 1

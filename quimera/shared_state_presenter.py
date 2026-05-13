import json


class SharedStatePresenter:
    """Formata estado compartilhado para inclusão no prompt."""

    EXECUTION_KEYS = {
        "goal", "goal_canonical", "decisions", "current_step",
        "acceptance_criteria", "allowed_scope", "non_goals",
        "out_of_scope_notes", "evidence", "next_step",
    }
    CORE_KEYS = {
        "goal_canonical", "current_step", "acceptance_criteria",
        "task_overview", "decisions", "working_dir", "workspace_root",
        "evidence", "next_step", "goal", "allowed_scope",
        "non_goals", "out_of_scope_notes",
    }

    @staticmethod
    def trim(state, decisions_tail=5):
        """Mantém apenas chaves centrais e limita o histórico de decisões."""
        trimmed = {}
        for k in SharedStatePresenter.CORE_KEYS:
            if k in state:
                trimmed[k] = state[k]
        if "decisions" in state:
            trimmed["decisions"] = state["decisions"][-decisions_tail:]
        return trimmed

    @staticmethod
    def present(shared_state):
        """Serializa o estado compartilhado não operacional para o prompt."""
        shared = shared_state or {}
        fallback_shared = {}
        completed_task_results = shared.get("completed_task_results", "") or ""
        if shared:
            fallback_shared = {
                k: v
                for k, v in SharedStatePresenter.trim(shared).items()
                if k not in SharedStatePresenter.EXECUTION_KEYS
            }
        shared_state_json = ""
        if fallback_shared:
            shared_state_json = json.dumps(fallback_shared, ensure_ascii=False, indent=2)
        return shared_state_json, completed_task_results

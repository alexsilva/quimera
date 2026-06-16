class PromptBudget:
    """Calcula métricas simples de tamanho para depuração de prompts."""

    @staticmethod
    def measure(full_prompt, route_agents="", session_id="", current_job_id="",
                workspace_root="", current_dir="", context="", request="",
                execution_state="", shared_state_json="", completed_task_results="",
                recent_conversation="", delegation_fields=None, history=None,
                history_window=12, primary=True):
        """Retorna um resumo dos principais blocos que compõem o prompt final."""
        delegation_fields = delegation_fields or {}
        history = history or []
        return {
            "rules_chars": len(route_agents),
            "session_state_chars": len(session_id) + len(str(current_job_id)) + len(workspace_root) + len(current_dir),
            "persistent_chars": len(context),
            "request_chars": len(request),
            "execution_state_chars": len(execution_state),
            "shared_state_chars": len(shared_state_json) + len(completed_task_results),
            "history_chars": len(recent_conversation),
            "delegation_chars": sum(len(str(v)) for v in delegation_fields.values()),
            "total_chars": len(full_prompt),
            "history_messages": len(history[-history_window:]),
            "primary": primary,
        }

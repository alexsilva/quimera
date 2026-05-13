class ExecutionModePresenter:
    """Extrai instruções de modo de execução para compor o prompt."""

    @staticmethod
    def present(execution_mode):
        """Retorna o texto complementar do modo de execução atual, se houver."""
        if execution_mode is None:
            return ""
        prompt_addon = str(getattr(execution_mode, "prompt_addon", "") or "").strip()
        return prompt_addon

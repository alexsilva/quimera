from .shared_state import (
    AGENT_STATE_KEYS,
    TASK_REFERENCE_KEYS,
    build_prompt_state_payload,
    build_task_reference_payload,
    trim_state,
)


class SharedStatePresenter:
    """Formata estado compartilhado para inclusão no prompt."""

    EXECUTION_KEYS = AGENT_STATE_KEYS
    CORE_KEYS = TASK_REFERENCE_KEYS

    @staticmethod
    def trim(state, decisions_tail=5):
        """Mantém apenas chaves centrais e limita o histórico de decisões."""
        return trim_state(state, allowed_keys=SharedStatePresenter.CORE_KEYS, decisions_tail=decisions_tail)

    @staticmethod
    def present(shared_state):
        """Serializa o estado compartilhado não operacional para o prompt."""
        return build_prompt_state_payload(shared_state)

    @staticmethod
    def task_reference(shared_state):
        """Retorna o estado sanitizado usado como referência em tasks."""
        return build_task_reference_payload(shared_state)

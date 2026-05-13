from collections import deque

from quimera.app.task_prompt_factory import TaskPromptFactory


class PromptBuilderStub:
    def __init__(self, history_window=4):
        self.history_window = history_window


def test_task_context_history_window_uses_prompt_builder_value():
    factory = TaskPromptFactory(
        history=[],
        user_name="Alex",
        shared_state={},
        prompt_builder=PromptBuilderStub(history_window=7),
    )

    assert factory.task_context_history_window() == 7


def test_task_context_history_window_falls_back_to_default():
    factory = TaskPromptFactory(
        history=[],
        user_name="Alex",
        shared_state={},
        prompt_builder=PromptBuilderStub(history_window=0),
    )

    assert factory.task_context_history_window() == 12


def test_format_task_chat_context_handles_deque_history():
    history = deque(
        [
            {"role": "human", "content": "Corrija o parser atual"},
            {"role": "codex", "content": "Vou inspecionar o arquivo antes de editar."},
        ]
    )
    factory = TaskPromptFactory(
        history=history,
        user_name="Alex",
        shared_state={},
        prompt_builder=PromptBuilderStub(history_window=4),
    )

    context = factory.format_task_chat_context()

    assert "[ALEX]: Corrija o parser atual" in context
    assert "[CODEX]: Vou inspecionar o arquivo antes de editar." in context


def test_format_task_chat_context_empty_history_returns_placeholder():
    factory = TaskPromptFactory(
        history=[],
        user_name="Alex",
        shared_state={},
        prompt_builder=None,
    )

    assert factory.format_task_chat_context() == "[sem contexto recente do chat]"


def test_build_task_body_includes_protocol_and_instruction():
    factory = TaskPromptFactory(
        history=[{"role": "human", "content": "Corrija o parser atual"}],
        user_name="Alex",
        shared_state={},
        prompt_builder=PromptBuilderStub(history_window=4),
    )

    body = factory.build_task_body("corrigir parser")

    assert "PROTOCOLO OPERACIONAL:" in body
    assert "Descubra o alvo antes de mudar" in body
    assert "apply_patch" in body
    assert "run_shell" in body
    assert "exec_command" in body
    assert "Use o estado compartilhado apenas como referência auxiliar" in body


def test_build_task_body_uses_trimmed_shared_state_when_available():
    factory = TaskPromptFactory(
        history=[{"role": "human", "content": "Corrija o parser atual"}],
        user_name="Alex",
        shared_state={
            "goal_canonical": "Corrigir parser legado",
            "current_step": "Ajustar tokenizer",
            "allowed_scope": ["parser.py"],
            "internal_note": "não deve aparecer",
        },
        prompt_builder=PromptBuilderStub(history_window=4),
    )

    body = factory.build_task_body("corrigir parser")

    assert "ESTADO COMPARTILHADO (referência):" in body
    assert '"goal_canonical": "Corrigir parser legado"' in body
    assert '"current_step": "Ajustar tokenizer"' in body
    assert '"allowed_scope"' in body
    assert '"internal_note"' not in body


def test_format_task_chat_context_skips_empty_messages():
    factory = TaskPromptFactory(
        history=[
            {"role": "human", "content": "  "},
            {"role": "codex", "content": ""},
            {"role": "human", "content": "Pedido válido"},
        ],
        user_name="Alex",
        shared_state={},
        prompt_builder=PromptBuilderStub(history_window=4),
    )

    context = factory.format_task_chat_context()

    assert "[ALEX]: Pedido válido" in context
    assert context.count("[ALEX]") == 1
    assert "[CODEX]" not in context

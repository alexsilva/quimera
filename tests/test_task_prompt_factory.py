import json
import re
from collections import deque

from quimera.tasks.prompt import TaskPromptFactory
from quimera.shared_state_presenter import SharedStatePresenter


class PromptBuilderStub:
    def __init__(self, history_window=4):
        self.history_window = history_window


def _extract_shared_state_payload(body: str) -> dict:
    match = re.search(
        r"ESTADO COMPARTILHADO \(referência\):\n(.*?)\n\nPROTOCOLO OPERACIONAL:",
        body,
        re.DOTALL,
    )
    assert match, "Bloco de shared_state não encontrado no body da task"
    return json.loads(match.group(1))


def test_task_context_history_window_uses_prompt_builder_value():
    """Verifica que task_context_history_window usa o valor do PromptBuilder."""

    factory = TaskPromptFactory(
        history=[],
        user_name="Alex",
        shared_state={},
        prompt_builder=PromptBuilderStub(history_window=7),
    )

    assert factory.task_context_history_window() == 7


def test_task_context_history_window_falls_back_to_default():
    """Verifica que task_context_history_window usa valor padrão quando PromptBuilder retorna 0."""

    factory = TaskPromptFactory(
        history=[],
        user_name="Alex",
        shared_state={},
        prompt_builder=PromptBuilderStub(history_window=0),
    )

    assert factory.task_context_history_window() == 12


def test_format_task_chat_context_handles_deque_history():
    """Verifica que format_task_chat_context lida com histórico em deque."""

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
    """Verifica que format_task_chat_context retorna placeholder quando histórico está vazio."""
    factory = TaskPromptFactory(
        history=[],
        user_name="Alex",
        shared_state={},
        prompt_builder=None,
    )

    assert factory.format_task_chat_context() == "[sem contexto recente do chat]"


def test_build_task_body_includes_protocol_and_instruction():
    """Verifica que build_task_body inclui protocolo operacional e instruções."""
    factory = TaskPromptFactory(
        history=[{"role": "human", "content": "Corrija o parser atual"}],
        user_name="Alex",
        shared_state={},
        prompt_builder=PromptBuilderStub(history_window=4),
    )

    body = factory.build_task_body("corrigir parser")

    assert "PROTOCOLO OPERACIONAL:" in body
    assert "CONTEXTO RECENTE DO CHAT:" not in body
    assert "CONTEXTO DA TASK (sanitizado):" in body
    assert "Descubra o alvo antes de mudar" in body
    assert "apply_patch" in body
    assert "run_shell" in body
    assert "exec_command" in body
    assert "Ignore conversa recente fora da task" in body
    assert "Use o estado compartilhado apenas como referência auxiliar" in body


def test_build_task_body_uses_trimmed_shared_state_when_available():
    """Verifica que build_task_body usa shared_state filtrado, excluindo chaves internas."""
    shared_state = {
        "goal_canonical": "Corrigir parser legado",
        "current_step": "Ajustar tokenizer",
        "allowed_scope": ["parser.py"],
        "task_overview": {"job_id": 7},
        "internal_note": "não deve aparecer",
        "working_dir": "/tmp/worktree",
        "completed_task_results": "[task 1] ok",
    }
    factory = TaskPromptFactory(
        history=[{"role": "human", "content": "Corrija o parser atual"}],
        user_name="Alex",
        shared_state=shared_state,
        prompt_builder=PromptBuilderStub(history_window=4),
    )

    body = factory.build_task_body("corrigir parser")
    payload = _extract_shared_state_payload(body)

    assert "ESTADO COMPARTILHADO (referência):" in body
    assert payload == SharedStatePresenter.task_reference(shared_state)
    assert payload["goal_canonical"] == "Corrigir parser legado"
    assert payload["current_step"] == "Ajustar tokenizer"
    assert payload["task_overview"] == {"job_id": 7}
    assert "internal_note" not in payload
    assert "working_dir" not in payload
    assert "completed_task_results" not in payload


def test_format_task_chat_context_skips_empty_messages():
    """Verifica que format_task_chat_context ignora mensagens vazias."""
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


def test_build_task_body_omits_task_context_block_when_history_is_empty():
    """Verifica que build_task_body omite bloco de contexto da tarefa quando histórico está vazio."""
    factory = TaskPromptFactory(
        history=[],
        user_name="Alex",
        shared_state={},
        prompt_builder=PromptBuilderStub(history_window=4),
    )

    body = factory.build_task_body("corrigir parser")

    assert "CONTEXTO DA TASK (sanitizado):" not in body


def test_build_task_body_serializes_shared_state_reference_as_valid_json():
    """Verifica que build_task_body serializa a referência de shared_state como JSON válido."""
    shared_state = {
        "goal": "corrigir acentuação",
        "evidence": ["stacktrace", "pytest -q"],
        "task_overview": {
            "job_id": 11,
            "recommended_action": "Executar task aprovada\nantes de abrir outra.",
        },
        "spy_last_turn_detail": {"agent": "codex"},
        "workspace_root": "/tmp/worktree",
    }
    factory = TaskPromptFactory(
        history=[{"role": "human", "content": "Corrija o parser atual"}],
        user_name="Alex",
        shared_state=shared_state,
        prompt_builder=PromptBuilderStub(history_window=4),
    )

    body = factory.build_task_body("corrigir parser")
    payload = _extract_shared_state_payload(body)

    assert payload == SharedStatePresenter.task_reference(shared_state)
    assert payload["goal"] == "corrigir acentuação"
    assert payload["task_overview"]["job_id"] == 11
    assert "spy_last_turn_detail" not in payload
    assert "workspace_root" not in payload

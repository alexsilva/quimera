"""Cancelamento seletivo dos slots de chat."""
from __future__ import annotations

from unittest.mock import Mock

from quimera.app.chat_lifecycle import ChatExecutionSnapshot, ChatLifecycle


class _Renderer:
    supports_submission_status = True

    def __init__(self) -> None:
        self.transitions: list[tuple[str, str, dict]] = []

    def update_submission_status(self, submission_id, status, **metadata):
        self.transitions.append((submission_id, status, metadata))

    def reset_visual_state(self) -> None:
        return None


class _SelectiveClient:
    def __init__(self) -> None:
        self.cancel_count = 0

    def cancel_own_work(self) -> None:
        self.cancel_count += 1


def _make_lifecycle() -> ChatLifecycle:
    runtime_state = Mock(chat_executor=Mock(), chat_slot_semaphore=None)
    runtime_state.get_chat_outstanding_count.return_value = 0
    runtime_state.get_chat_pending_count.return_value = 0
    return ChatLifecycle(
        chat_round_orchestrator=Mock(),
        system_layer=Mock(),
        renderer=_Renderer(),
        runtime_state=runtime_state,
        turn_manager=None,
        agent_client=Mock(),
        ui_event_handler=Mock(),
        session_services=None,
        task_services=None,
        session_state=None,
        dispatch_services=None,
        parse_routing=None,
        parse_response=None,
        refresh_parallel_toolbar=Mock(),
    )


def test_stop_by_number_cancels_only_selected_chat_client() -> None:
    lifecycle = _make_lifecycle()
    first = _SelectiveClient()
    second = _SelectiveClient()
    lifecycle.register_submission("submission:first", "primeiro pedido")
    lifecycle.register_submission("submission:second", "segundo pedido")
    unbind_first = lifecycle._bind_chat_client(
        "submission:first",
        "claude",
        first,
    )
    unbind_second = lifecycle._bind_chat_client(
        "submission:second",
        "codex",
        second,
    )

    executions = lifecycle.list_chat_executions()
    assert [(item.number, item.agent, item.prompt) for item in executions] == [
        (1, "claude", "primeiro pedido"),
        (2, "codex", "segundo pedido"),
    ]

    result = lifecycle.stop_chat_execution("#2")

    assert result.stopped is True
    assert result.submission_id == "submission:second"
    assert first.cancel_count == 0
    assert second.cancel_count == 1
    assert [item.submission_id for item in lifecycle.list_chat_executions()] == [
        "submission:first"
    ]
    assert "submission:second" in lifecycle._cancelled_submission_ids
    unbind_first()
    unbind_second()


def test_stop_by_agent_requires_number_when_same_agent_has_two_runs() -> None:
    lifecycle = _make_lifecycle()
    clients = [_SelectiveClient(), _SelectiveClient()]
    for index, client in enumerate(clients, start=1):
        submission_id = f"submission:{index}"
        lifecycle.register_submission(submission_id, f"pedido {index}")
        lifecycle._bind_chat_client(submission_id, "codex", client)

    result = lifecycle.stop_chat_execution("codex")

    assert result.stopped is False
    assert "mais de uma execução" in result.message
    assert "#1 codex" in result.message
    assert "#2 codex" in result.message
    assert [client.cancel_count for client in clients] == [0, 0]


def test_stop_without_selector_stops_the_only_execution() -> None:
    lifecycle = _make_lifecycle()
    client = _SelectiveClient()
    lifecycle.register_submission("submission:only", "pedido")
    lifecycle._bind_chat_client("submission:only", "claude", client)

    result = lifecycle.stop_chat_execution()

    assert result.stopped is True
    assert client.cancel_count == 1


def test_stop_queued_submission_cancels_its_future_only() -> None:
    lifecycle = _make_lifecycle()
    target_future = Mock()
    other_future = Mock()
    lifecycle.register_submission("submission:queued", "na fila")
    lifecycle.update_submission_status(
        "submission:queued",
        "queued",
        queue_position=2,
    )
    lifecycle._submission_futures = {
        1: (target_future, "submission:queued", False),
        2: (other_future, "submission:other", True),
    }

    result = lifecycle.stop_chat_execution("#1")

    assert result.stopped is True
    target_future.cancel.assert_called_once_with()
    other_future.cancel.assert_not_called()


def test_client_bound_after_stop_is_cancelled_immediately() -> None:
    """Fecha a corrida entre clicar em Parar e o fork entrar no dispatch."""
    lifecycle = _make_lifecycle()
    lifecycle.register_submission("submission:race", "pedido")
    lifecycle.stop_chat_submission("submission:race")
    late_client = _SelectiveClient()

    lifecycle._bind_chat_client("submission:race", "codex", late_client)

    assert late_client.cancel_count == 1
    assert lifecycle.list_chat_executions() == []


def test_stop_by_agent_accepts_unique_name_prefix() -> None:
    """Nomes reais são longos; ``/stop codex`` deve alcançar ``codex-gpt-5-6-sol``."""
    lifecycle = _make_lifecycle()
    codex = _SelectiveClient()
    claude = _SelectiveClient()
    lifecycle.register_submission("submission:codex", "pedido codex")
    lifecycle.register_submission("submission:claude", "pedido claude")
    lifecycle._bind_chat_client("submission:codex", "codex-gpt-5-6-sol", codex)
    lifecycle._bind_chat_client("submission:claude", "claude-sonnet", claude)

    result = lifecycle.stop_chat_execution("codex")

    assert result.stopped is True
    assert result.submission_id == "submission:codex"
    assert codex.cancel_count == 1
    assert claude.cancel_count == 0


def test_stop_by_ambiguous_prefix_lists_candidates_without_stopping() -> None:
    lifecycle = _make_lifecycle()
    clients = {
        "codex-gpt-5-6-sol": _SelectiveClient(),
        "codex-gpt-5-6-luna": _SelectiveClient(),
    }
    for index, (agent, client) in enumerate(clients.items(), start=1):
        submission_id = f"submission:{index}"
        lifecycle.register_submission(submission_id, f"pedido {index}")
        lifecycle._bind_chat_client(submission_id, agent, client)

    result = lifecycle.stop_chat_execution("codex")

    assert result.stopped is False
    assert "mais de uma execução" in result.message
    assert "#1 codex-gpt-5-6-sol" in result.message
    assert "#2 codex-gpt-5-6-luna" in result.message
    assert [client.cancel_count for client in clients.values()] == [0, 0]


def test_snapshot_label_compacta_prompt_e_mostra_tempo_decorrido() -> None:
    snapshot = ChatExecutionSnapshot(
        3, "submission:c", "claude", "  pense\n bastante  " + "x" * 80, "running", None, 3661.9
    )
    assert snapshot.elapsed_label == "1:01:01"
    assert snapshot.label.startswith("#3 claude · 1:01:01 · pense bastante xxx")
    assert snapshot.label.endswith("…")
    assert len(snapshot.label.split(" · ", 2)[2]) == 72

    queued = ChatExecutionSnapshot(4, "submission:d", "", "", "queued", 2, 0)
    assert queued.label == "#4 aguardando agente · 00:00"

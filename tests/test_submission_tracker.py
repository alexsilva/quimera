"""Testes do lifecycle rastreável de prompts do chat."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock

import pytest

from quimera.app.chat_lifecycle import ChatLifecycle
from quimera.app.submission_tracker import SubmissionTracker


class _Timer:
    def __init__(self, _interval, callback, args=()):
        self.callback = callback
        self.args = args
        self.cancelled = False
        self.daemon = False

    def start(self):
        return None

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback(*self.args)


def test_submission_tracker_watchdog_marks_prompt_waiting():
    emitted = []
    timers = []
    now = [10.0]

    def timer_factory(*args, **kwargs):
        timer = _Timer(*args, **kwargs)
        timers.append(timer)
        return timer

    tracker = SubmissionTracker(
        emitted.append,
        clock=lambda: now[0],
        watchdog_seconds=5,
        timer_factory=timer_factory,
    )

    record = tracker.start()
    tracker.transition(record.submission_id, "queued", queue_position=2)
    now[0] = 15.0
    timers[0].fire()

    assert tracker.get(record.submission_id).status == "waiting"
    assert emitted[-1]["message"] == "Aguardando início há mais de 5s"
    assert emitted[-1]["elapsed_seconds"] == 5.0


def test_submission_tracker_running_cancels_watchdog_and_terminal_is_immutable():
    emitted = []
    timers = []

    def timer_factory(*args, **kwargs):
        timer = _Timer(*args, **kwargs)
        timers.append(timer)
        return timer

    tracker = SubmissionTracker(emitted.append, timer_factory=timer_factory)
    record = tracker.start()

    tracker.transition(record.submission_id, "running")
    tracker.transition(record.submission_id, "completed")
    tracker.transition(record.submission_id, "failed", message="evento atrasado")

    assert timers[0].cancelled is True
    assert tracker.get(record.submission_id).status == "completed"
    assert emitted[-1]["status"] == "completed"


def test_submission_tracker_never_evicts_active_prompt_to_enforce_history_limit():
    tracker = SubmissionTracker(lambda _payload: None, watchdog_seconds=0, max_records=1)

    first = tracker.start()
    second = tracker.start()

    assert tracker.get(first.submission_id) is not None
    assert tracker.get(second.submission_id) is not None


def test_submission_tracker_watchdog_cannot_override_running_state():
    tracker = SubmissionTracker(lambda _payload: None, watchdog_seconds=0)
    record = tracker.start()
    tracker.transition(record.submission_id, "running")

    tracker.transition(
        record.submission_id,
        "waiting",
        expected_statuses=frozenset({"accepted", "queued", "starting"}),
    )

    assert tracker.get(record.submission_id).status == "running"


def test_submission_revisions_allow_feed_to_reject_out_of_order_emissions():
    running_emit_started = threading.Event()
    release_running_emit = threading.Event()
    emitted = []

    def emit(payload):
        if payload["status"] == "running":
            running_emit_started.set()
            release_running_emit.wait(timeout=1)
        emitted.append(payload)

    tracker = SubmissionTracker(emit, watchdog_seconds=0)
    record = tracker.start(emit=False)
    running_thread = threading.Thread(
        target=tracker.transition,
        args=(record.submission_id, "running"),
    )
    running_thread.start()
    assert running_emit_started.wait(timeout=1)

    tracker.transition(record.submission_id, "completed")
    release_running_emit.set()
    running_thread.join(timeout=1)

    assert [payload["status"] for payload in emitted] == ["completed", "running"]
    assert emitted[0]["revision"] > emitted[1]["revision"]


class _SubmissionRenderer:
    supports_submission_status = True

    def __init__(self):
        self.transitions = []

    def update_submission_status(self, submission_id, status, **metadata):
        self.transitions.append((submission_id, status, metadata))

    def reset_visual_state(self):
        return None


def _make_lifecycle(orchestrator, renderer, executor):
    runtime_state = Mock(chat_executor=executor)
    runtime_state.decrement_chat_inflight.return_value = 0
    runtime_state.get_chat_pending_count.return_value = 0
    runtime_state.get_chat_outstanding_count.return_value = 0
    return ChatLifecycle(
        chat_round_orchestrator=orchestrator,
        system_layer=Mock(),
        renderer=renderer,
        runtime_state=runtime_state,
        turn_manager=None,
        agent_client=None,
        ui_event_handler=Mock(),
        session_services=None,
        task_services=None,
        session_state=None,
        dispatch_services=None,
        parse_routing=None,
        parse_response=None,
        refresh_parallel_toolbar=Mock(),
    )


def test_chat_lifecycle_observes_async_future_failure():
    orchestrator = Mock()
    orchestrator.process.side_effect = RuntimeError("backend indisponível")
    renderer = _SubmissionRenderer()
    executor = ThreadPoolExecutor(max_workers=1)
    lifecycle = _make_lifecycle(orchestrator, renderer, executor)

    try:
        future = lifecycle.submit_async_message(
            "teste",
            submission_id="submission:test",
        )
        with pytest.raises(RuntimeError, match="backend indisponível"):
            future.result(timeout=2)
    finally:
        executor.shutdown(wait=True)

    statuses = [status for _submission_id, status, _metadata in renderer.transitions]
    assert statuses[:2] == ["starting", "running"]
    assert statuses[-1] == "failed"
    assert lifecycle._submission_futures == {}


def test_chat_lifecycle_marks_successful_future_completed():
    orchestrator = Mock()
    renderer = _SubmissionRenderer()
    executor = ThreadPoolExecutor(max_workers=1)
    lifecycle = _make_lifecycle(orchestrator, renderer, executor)

    try:
        future = lifecycle.submit_async_message(
            "teste",
            submission_id="submission:success",
        )
        future.result(timeout=2)
    finally:
        executor.shutdown(wait=True)

    statuses = [status for _submission_id, status, _metadata in renderer.transitions]
    assert statuses == ["starting", "running", "completed"]


def test_chat_lifecycle_cancels_accepted_submission_before_executor_submit():
    renderer = _SubmissionRenderer()
    executor = Mock()
    lifecycle = _make_lifecycle(Mock(), renderer, executor)
    lifecycle.register_submission("submission:queued")

    lifecycle.handle_local_interrupt()
    result = lifecycle.submit_async_message(
        "teste",
        submission_id="submission:queued",
    )

    assert result is None
    executor.submit.assert_not_called()
    lifecycle._runtime_state.decrement_chat_inflight.assert_called_once()
    lifecycle._runtime_state.release_chat_slot.assert_called_once()
    assert renderer.transitions[-1][1] == "cancelled"

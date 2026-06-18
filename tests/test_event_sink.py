"""Testes unitários para EventSink."""
from __future__ import annotations

import threading

import pytest

from quimera.app.event_sink import EventSink
from quimera.tasks.events import TaskEvent, TaskProposed, TaskCompleted


def _proposed(task_id=1, job_id=1, description="desc"):
    return TaskProposed(task_id=task_id, job_id=job_id, description=description)


def _completed(task_id=1, job_id=1):
    return TaskCompleted(task_id=task_id, job_id=job_id)


# ── subscribe / publish ────────────────────────────────────────────────────


def test_handler_called_on_matching_event():
    """Verifica que o handler é chamado quando o evento corresponde ao tipo inscrito."""
    sink = EventSink()
    received = []
    sink.subscribe(TaskProposed, received.append)

    ev = _proposed()
    sink.publish(ev)

    assert received == [ev]


def test_handler_not_called_for_different_event_type():
    """Verifica que o handler não é chamado para evento de tipo diferente."""
    sink = EventSink()
    received = []
    sink.subscribe(TaskCompleted, received.append)

    sink.publish(_proposed())

    assert received == []


def test_supertype_handler_receives_subclass_event():
    """Handler registrado em TaskEvent deve receber qualquer subclasse."""
    sink = EventSink()
    received = []
    sink.subscribe(TaskEvent, received.append)

    ev = _proposed()
    sink.publish(ev)

    assert received == [ev]


def test_multiple_handlers_all_called():
    """Verifica que múltiplos handlers para o mesmo evento são todos chamados."""
    sink = EventSink()
    calls_a, calls_b = [], []
    sink.subscribe(TaskProposed, calls_a.append)
    sink.subscribe(TaskProposed, calls_b.append)

    ev = _proposed()
    sink.publish(ev)

    assert calls_a == [ev]
    assert calls_b == [ev]


# ── unsubscribe ────────────────────────────────────────────────────────────


def test_unsubscribe_stops_delivery():
    """Verifica que remover a inscrição interrompe a entrega de eventos."""
    sink = EventSink()
    received = []
    unsubscribe = sink.subscribe(TaskProposed, received.append)

    unsubscribe()
    sink.publish(_proposed())

    assert received == []


def test_unsubscribe_idempotent():
    """Verifica que chamar unsubscribe duas vezes não causa erro."""
    sink = EventSink()
    received = []
    unsubscribe = sink.subscribe(TaskProposed, received.append)
    unsubscribe()
    unsubscribe()  # segunda chamada não deve levantar exceção
    sink.publish(_proposed())
    assert received == []


# ── isolamento de exceções ─────────────────────────────────────────────────


def test_failing_handler_does_not_prevent_others():
    """Verifica que um handler com falha não impede outros handlers de serem chamados."""
    sink = EventSink()
    received = []

    def bad_handler(_ev):
        raise RuntimeError("boom")

    sink.subscribe(TaskProposed, bad_handler)
    sink.subscribe(TaskProposed, received.append)

    ev = _proposed()
    sink.publish(ev)  # não deve propagar

    assert received == [ev]


def test_publish_does_not_raise_on_handler_error():
    """Verifica que publish não propaga exceções lançadas por handlers."""
    sink = EventSink()
    sink.subscribe(TaskProposed, lambda _: (_ for _ in ()).throw(ValueError("x")))
    sink.publish(_proposed())  # deve ser silencioso


# ── clear ──────────────────────────────────────────────────────────────────


def test_clear_removes_all_handlers():
    """Verifica que clear remove todos os handlers registrados."""
    sink = EventSink()
    received = []
    sink.subscribe(TaskProposed, received.append)

    sink.clear()
    sink.publish(_proposed())

    assert received == []


# ── thread safety ──────────────────────────────────────────────────────────


def test_concurrent_publish_does_not_raise():
    """Verifica que publish concorrente não lança exceções."""
    sink = EventSink()
    sink.subscribe(TaskProposed, lambda _: None)
    errors = []

    def publish_many():
        try:
            for _ in range(50):
                sink.publish(_proposed())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=publish_many) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_publish_from_background_thread_defers_until_main_thread_drains():
    """Verifica que publish de thread background é deferido até o drain da thread principal."""
    sink = EventSink()
    received = []
    handler_threads = []

    def handler(event):
        received.append(event)
        handler_threads.append(threading.current_thread())

    sink.subscribe(TaskProposed, handler)
    event = _proposed()

    worker = threading.Thread(target=lambda: sink.publish(event))
    worker.start()
    worker.join()

    assert received == []

    sink.drain_pending()

    assert received == [event]
    assert handler_threads == [threading.main_thread()]

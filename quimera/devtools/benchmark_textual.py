"""Microbenchmarks reproduzíveis dos hot paths da TUI, sem abrir um terminal.

Execute ``python -m quimera.devtools.benchmark_textual``. As referências são
os algoritmos de e34f69c; os dados e o processo são iguais para ambos os lados.
Os resultados não representam RSS/CPU total nem latência de rede de agentes.
"""
from __future__ import annotations

import argparse
import gc
import json
import statistics
import time
import tracemalloc

from quimera.app.submission_tracker import TERMINAL_SUBMISSION_STATUSES
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import TextualFeedModel
from quimera.ui.textual.renderables import _live_tail
from quimera.ui.textual.widgets import _FeedEntry, _UnifiedFeed


def _reference_active_submissions(model):
    active = []
    for index, item in enumerate(model._items):
        if item.event.kind != "user_message":
            continue
        payload = item.event.payload if isinstance(item.event.payload, dict) else {}
        submission = payload.get("submission")
        if not isinstance(submission, dict):
            continue
        status = str(submission.get("status") or "").strip().lower()
        if status and status not in TERMINAL_SUBMISSION_STATUSES:
            active.append((index, item))
    return active


def _reference_live_tail(text):
    lines = text.splitlines()
    tail = "\n".join(lines[-8:])
    if len(tail) > 1600:
        cut = tail[-1600:]
        newline = cut.find("\n")
        if 0 <= newline < len(cut) - 1:
            cut = cut[newline + 1:]
        tail = cut
    if tail == text:
        return text, ""
    hidden = len(lines) - (tail.count("\n") + 1)
    return (tail, f"⋮ +{hidden} linhas anteriores") if hidden > 0 else (tail, "⋮ trecho anterior oculto")


def _reference_stable_slots(feed, entries):
    # Mesma reconciliação anterior no caso steady-state: nenhum mount/remove.
    desired = list(entries)
    shared = min(len(desired), len(feed._entry_widgets))
    for index in range(shared):
        token, transient, renderable, compact = desired[index]
        widget = feed._entry_widgets[index]
        if transient or token != feed._entry_tokens[index]:
            widget.update(renderable)
        widget.set_class(compact, "-compact")
        feed._entry_tokens[index] = token


def _measure(fn, *, loops, repeats):
    fn()
    samples = []
    for _ in range(repeats):
        gc.collect()
        started = time.perf_counter()
        for _ in range(loops):
            fn()
        samples.append((time.perf_counter() - started) * 1000 / loops)
    tracemalloc.start()
    try:
        fn()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {"median_ms_per_call": round(statistics.median(samples), 6), "temporary_peak_bytes": peak}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=10000)
    parser.add_argument("--slots", type=int, default=1000)
    parser.add_argument("--loops", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if min(args.items, args.slots, args.loops, args.repeats) < 1:
        parser.error("todos os parâmetros devem ser positivos")

    model = TextualFeedModel()
    for index in range(args.items):
        model.apply(TextualUiEvent("user_message", {
            "content": f"histórico {index}", "submission": {"status": "completed"},
        }))
    model.apply(TextualUiEvent("user_message", {
        "content": "turno ativo", "submission": {"status": "running"},
    }))
    text = "\n".join(f"linha {index:05d}: " + "conteúdo " * 10 for index in range(args.items))
    assert model.active_submission_items() == _reference_active_submissions(model)
    assert _live_tail(text) == _reference_live_tail(text)

    feed = _UnifiedFeed()
    entries = [(index, False, "linha", False) for index in range(args.slots)]
    feed._entry_widgets = [_FeedEntry("linha", classes="feed-entry") for _ in entries]
    feed._entry_tokens = [entry[0] for entry in entries]
    cases = {
        "active_submissions": (lambda: _reference_active_submissions(model), model.active_submission_items),
        "live_tail": (lambda: _reference_live_tail(text), lambda: _live_tail(text)),
        "stable_slots": (lambda: _reference_stable_slots(feed, entries), lambda: feed.sync_entries(entries)),
    }
    results = {}
    for name, (before, after) in cases.items():
        before_measure = _measure(before, loops=args.loops, repeats=args.repeats)
        after_measure = _measure(after, loops=args.loops, repeats=args.repeats)
        results[name] = {"before": before_measure, "after": after_measure}
    print(json.dumps({
        "baseline": "e34f69c algorithms", "items": args.items + 1,
        "slots": args.slots, "live_text_chars": len(text),
        "loops": args.loops, "repeats": args.repeats, "results": results,
    }, indent=2))


if __name__ == "__main__":
    main()

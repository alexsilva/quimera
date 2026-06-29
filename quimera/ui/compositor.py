"""Terminal compositor — owns the writer thread and event queue.

The compositor is the single owner of:
- The writer thread (event loop)
- The event queue (``emit`` / ``emit_nowait``)
- Output suspension (freeze / thaw)
- Stream Live activation
- Terminal resize signal handling

It delegates renderable construction back to ``TerminalRenderer`` but
controls all terminal I/O sequencing.
"""
from __future__ import annotations

import logging
import queue as _queue_module
import signal as _signal
import sys
import threading
from collections import defaultdict, deque
from typing import Any

from .events import (
    LiveAbortEvent,
    LiveStartEvent,
    LiveStopEvent,
    LiveUpdateChunkEvent,
    NoopEvent,
    OutputControlEvent,
    PendingInputEvent,
    PrintEvent,
    TerminalResizeEvent,
    TransientClearEvent,
    TransientWindowEvent,
)
from .overlay import TransientOverlay
from .text import (
    _apply_stream_diff,
    _normalize_stream_diff,
    _preview_chunk,
    _preview_text,
    strip_ansi,
)

# Lazy Rich imports — resolved at call time via quimera.ui so that
# test patches (e.g. ``patch("quimera.ui.Live")``) take effect.
_LAZY_RICH = None


def _rich():
    global _LAZY_RICH
    if _LAZY_RICH is None:
        import quimera.ui as _ui
        _LAZY_RICH = _ui
    return _LAZY_RICH

_log = logging.getLogger(__name__)

_STOP = object()


class TerminalCompositor:
    """Owns the writer thread and all terminal output sequencing.

    ``TerminalRenderer`` creates one compositor in ``__init__`` and
    delegates all writer-thread ownership to it.
    """

    def __init__(
        self,
        renderer,
        audit_logger=None,
    ):
        self._renderer = renderer
        self._audit_logger = audit_logger

        # Queue and writer thread — the compositor owns these.
        self._queue: _queue_module.Queue = _queue_module.Queue(maxsize=512)
        self._output_suspended = threading.Event()
        self._stream_live_active = threading.Event()

        # Overlay / transient state (managed by the compositor).
        self._overlay_lines: list[int] = [0]
        self._overlay = TransientOverlay(self._overlay_lines)
        self._transient_buf_version: int = 0
        self._last_combined_text: str | None = None

        self._app_sink = None  # set by set_app_sink() for split-UI mode

        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

        if self._audit_logger is not None:
            self._audit_logger.start_queue_sampler(self._queue)

        # SIGWINCH: forward resize events to the writer loop.
        try:
            _prev_sigwinch = _signal.getsignal(_signal.SIGWINCH)

            def _on_sigwinch(signum, frame):
                try:
                    self._queue.put_nowait(TerminalResizeEvent())
                except _queue_module.Full:
                    pass
                if callable(_prev_sigwinch) and _prev_sigwinch not in (
                    _signal.SIG_DFL,
                    _signal.SIG_IGN,
                ):
                    _prev_sigwinch(signum, frame)

            _signal.signal(_signal.SIGWINCH, _on_sigwinch)
        except (AttributeError, OSError):
            pass

    # ------------------------------------------------------------------
    # Public accessors (used by renderer as aliases)
    # ------------------------------------------------------------------

    @property
    def queue(self) -> _queue_module.Queue:
        return self._queue

    @property
    def output_suspended(self) -> threading.Event:
        return self._output_suspended

    @property
    def stream_live_active(self) -> threading.Event:
        return self._stream_live_active

    @property
    def transient_version(self) -> int:
        return self._transient_buf_version

    @property
    def last_combined_text(self) -> str | None:
        return self._last_combined_text

    def set_app_sink(self, sink) -> None:
        """Route compositor output to sink.append_output() instead of stdout (split-UI mode)."""
        self._app_sink = sink

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def emit(self, event) -> None:
        """Enqueue an event for the writer thread (blocking)."""
        self._queue.put(event)

    def emit_nowait(self, event) -> None:
        """Enqueue an event for the writer thread (non-blocking)."""
        self._queue.put_nowait(event)

    # ------------------------------------------------------------------
    # Transient version helpers
    # ------------------------------------------------------------------

    def bump_transient_version(self) -> int:
        with self._renderer._lock:
            self._transient_buf_version += 1
            return self._transient_buf_version

    def mark_transient_changed(self, *, changed: bool = True) -> int:
        """Return the current transient version, bumping it when state changed."""
        if changed:
            return self.bump_transient_version()
        with self._renderer._lock:
            return self._transient_buf_version

    def remember_combined_transient(self, text: str) -> bool:
        """Remember combined overlay text; return True when it changed."""
        if not text or text == self._last_combined_text:
            return False
        self._last_combined_text = text
        return True

    def clear_combined_transient(self) -> None:
        """Forget the last combined overlay text."""
        self._last_combined_text = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self, timeout: float = 5.0) -> None:
        self._queue.put(_STOP)
        self._writer_thread.join(timeout=timeout)

    def stop_nowait(self) -> None:
        try:
            if self._writer_thread.is_alive():
                self._queue.put_nowait(_STOP)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Flush / freeze / thaw
    # ------------------------------------------------------------------

    def flush(self, timeout: float = 5.0) -> None:
        done = threading.Event()
        self._queue.put(NoopEvent(done, force_flush=True))
        if not done.wait(timeout=timeout):
            raise TimeoutError(
                f"TerminalCompositor.flush timed out after {timeout} seconds"
            )

    def flush_quick(self, timeout: float = 0.15) -> bool:
        try:
            self.flush(timeout=timeout)
            return True
        except TimeoutError:
            return False

    def freeze_output(
        self,
        timeout: float = 2.0,
        *,
        render_anchored_windows: bool = False,
    ) -> bool:
        """Suspend compositor output before exclusive terminal input."""
        self._output_suspended.set()
        done = threading.Event()
        self._queue.put(
            OutputControlEvent(
                suspend=True,
                done=done,
                render_anchored_windows=render_anchored_windows,
            )
        )
        return done.wait(timeout=timeout)

    def thaw_output(self, timeout: float = 2.0) -> bool:
        done = threading.Event()
        self._queue.put(OutputControlEvent(suspend=False, done=done))
        resumed = done.wait(timeout=timeout)
        if not resumed:
            self._output_suspended.clear()
        return resumed

    def apply_window_render_plan(self, plan, timeout: float = 2.0) -> bool:
        """Apply a declarative window render plan to the terminal compositor."""
        ok = True
        anchored_requested = getattr(plan, "render_anchored_windows", False)
        overlay_cleared = False
        if plan.clear_overlay and anchored_requested:
            self._clear_overlay_sync()
            overlay_cleared = True
        if plan.suspend_output:
            ok = self.freeze_output(
                timeout=timeout,
                render_anchored_windows=anchored_requested,
            ) and ok
        if plan.clear_overlay and not overlay_cleared:
            self._clear_overlay_sync()
        if plan.resume_output:
            ok = self.thaw_output(timeout=timeout) and ok
        return ok

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clear_overlay_sync(self) -> None:
        n = self._overlay_lines[0]
        if n > 0:
            try:
                sys.stdout.write(f"\033[{n}A\033[J")
                sys.stdout.flush()
            except Exception:
                pass
            self._overlay_lines[0] = 0
        with self._renderer._lock:
            self._transient_buf_version += 1
        self._log_debug("floor_request", prev_lines=n)

    def _log_debug(self, event: str, **payload) -> None:
        if self._audit_logger is None:
            return
        self._audit_logger.log_event(event, **payload)

    # ------------------------------------------------------------------
    # Writer loop
    # ------------------------------------------------------------------

    def _writer_loop(self):
        """Single writer thread — processes all UI events sequentially."""

        _ul: list = [None]
        _local_pending: deque = deque()
        _deferred_post_prompt: deque = deque()
        _sink_sent_len: dict = {}  # agent → chars already sent to sink during streaming

        _renderer = self._renderer

        # -- helpers -------------------------------------------------------

        def _prompt_active() -> bool:
            try:
                return bool(
                    _renderer._is_prompt_active_fn
                    and _renderer._is_prompt_active_fn()
                )
            except Exception:
                return False

        def _get_version() -> int:
            with _renderer._lock:
                return self._transient_buf_version

        def _bump_version() -> int:
            with _renderer._lock:
                self._transient_buf_version += 1
                return self._transient_buf_version

        def _audit(event_name: str, **payload) -> None:
            self._log_debug(event_name, **payload)

        # -- Live helpers --------------------------------------------------

        def _get_renderable():
            _ui = _rich()
            stream_windows = _renderer._deck.active_streams()
            if not stream_windows:
                return _ui.Text("")
            parts = []
            for c in stream_windows.values():
                stream_block = _renderer._build_stream_renderable(
                    c.stream_theme_name, c.label, c.style, c.stream_content
                )
                if c.pending_kind:
                    pending_card = _renderer._build_pending_card_renderable(c)
                    parts.append(_ui.Group(stream_block, pending_card))
                else:
                    parts.append(stream_block)
            main = _ui.Group(*parts) if len(parts) > 1 else parts[0]
            if _renderer._density == "compact":
                return main
            if len(parts) > 1:
                labels = " \u00b7 ".join(
                    _agent_toolbar_label(agent, c.label)
                    for agent, c in stream_windows.items()
                )
                toolbar_text = f"[dim]{labels} \u00b7 Ctrl+C para cancelar[/dim]"
            else:
                only_agent, only_c = next(iter(stream_windows.items()))
                label_text = _agent_toolbar_label(only_agent, only_c.label)
                toolbar_text = (
                    f"[bold {only_c.style}]{label_text}[/] "
                    f"[dim]\u00b7 Ctrl+C para cancelar[/dim]"
                )
            infobar = _ui.Rule(toolbar_text, characters="\u00b7", style="dim")
            return _ui.Group(main, infobar)

        def _build_anchored_prompt_card(owner, window):
            """Build a declarative prompt child renderable for one owner window."""
            _ui = _rich()
            container = _renderer._deck.get(owner)
            label = getattr(container, "label", str(owner)) if container else str(owner)
            style = getattr(container, "style", "yellow") if container else "yellow"
            metadata = getattr(window, "metadata", {}) or {}
            question = str(metadata.get("question") or getattr(window, "title", "") or "").strip()
            options = list(metadata.get("options") or [])
            if options:
                question = "\n".join(
                    [question, *[f"  {index + 1}. {option}" for index, option in enumerate(options)]]
                ).strip()
            kind = getattr(getattr(window, "kind", ""), "value", getattr(window, "kind", ""))
            if not question:
                question = "aguardando resposta"
            card = _renderer._build_approval_card_renderable(
                label,
                style,
                question,
                kind=str(kind or "input"),
            )
            if card is not None:
                return card
            return _ui.Text(question)

        def _get_anchored_windows_renderable():
            """Build agent stream content followed by child prompt windows."""
            _ui = _rich()
            manager = getattr(_renderer, "_window_manager", None)
            if manager is None:
                return None
            stream_windows = _renderer._deck.active_streams()
            if not stream_windows:
                return None
            parts = []
            rendered_child_ids: set[str] = set()
            for owner, container in stream_windows.items():
                owner_text = getattr(container, "stream_content", "")
                if owner_text.strip():
                    parts.append(
                        _renderer._build_stream_renderable(
                            container.stream_theme_name,
                            container.label,
                            container.style,
                            owner_text,
                        )
                    )
                for child in manager.anchored_children(str(owner)):
                    parts.append(_build_anchored_prompt_card(owner, child))
                    rendered_child_ids.add(child.id)
            for window in manager.render_order():
                if window.id in rendered_child_ids:
                    continue
                if getattr(window, "anchor", None) is not None and getattr(window, "owner", None) in stream_windows:
                    continue
                kind = str(getattr(getattr(window, "kind", ""), "value", getattr(window, "kind", "")))
                if kind in {"approval", "input", "selection"}:
                    parts.append(_build_anchored_prompt_card(window.owner or window.id, window))
            if not parts:
                return None
            return _ui.Group(*parts) if len(parts) > 1 else parts[0]

        def _agent_toolbar_label(agent_name: str, base_label: str) -> str:
            _ui = _rich()
            return _ui.markup_escape(base_label)

        def _ensure_live():
            if self._app_sink is not None:
                return
            if _ul[0] is None and _renderer._console:
                if _prompt_active() or self._output_suspended.is_set():
                    return
                _ul[0] = _rich().Live(
                    _get_renderable(),
                    console=_renderer._console,
                    refresh_per_second=8,
                    transient=True,
                    auto_refresh=False,
                )
                _ul[0].start()
                self._stream_live_active.set()

        def _refresh():
            if _ul[0] is not None and not self._output_suspended.is_set():
                _ul[0].update(_get_renderable(), refresh=True)

        def _close_live():
            if _ul[0] is not None:
                _ul[0].stop()
                _ul[0] = None
                self._stream_live_active.clear()

        def _print_anchored_windows():
            """Print current agent content plus anchored prompt children before Live stops."""
            if _ul[0] is None or _renderer._console is None:
                return
            renderable = _get_anchored_windows_renderable()
            if renderable is not None:
                _renderer._console.print(renderable)

        def _stop_if_empty():
            if not _renderer._deck.active_streams():
                _close_live()

        # -- Print helpers -------------------------------------------------

        def _cprint(renderable, **kwargs):
            if self._output_suspended.is_set():
                _deferred_post_prompt.append((renderable, kwargs))
                return
            sink = self._app_sink
            if sink is not None:
                import io as _io
                from rich.console import Console as _RichConsole
                _buf = _io.StringIO()
                _w = _renderer._console.width if _renderer._console else 80
                _tmp = _RichConsole(file=_buf, force_terminal=True, width=_w, no_color=False)
                _tmp.print(renderable, **{k: v for k, v in kwargs.items() if k != "file"})
                sink.ensure_trailing_newline()
                sink.append_output(_buf.getvalue())
                return
            if _ul[0] is not None:
                _ul[0].console.print(renderable, **kwargs)
                return
            if _renderer._console is None:
                return
            run_above = _renderer._run_above_prompt_fn
            if run_above is not None:
                clear_and_print = self._overlay.build_print_above(
                    renderable,
                    kwargs,
                    _renderer._console,
                    _bump_version,
                    audit_fn=_audit,
                )
                if run_above(clear_and_print):
                    _flush_deferred()
                    return
                _deferred_post_prompt.append((renderable, kwargs))
                return
            _deferred_post_prompt.append((renderable, kwargs))

        def _flush_deferred(force=False):
            if self._output_suspended.is_set():
                return
            run_above = _renderer._run_above_prompt_fn
            while _deferred_post_prompt:
                _r, _k = _deferred_post_prompt.popleft()
                if _ul[0] is not None:
                    _ul[0].console.print(_r, **_k)
                elif force and _renderer._console:
                    _renderer._console.print(_r, **_k)
                elif run_above is not None:
                    def _do_print_deferred(r=_r, k=_k):
                        _renderer._console.print(r, **k)
                    if not run_above(_do_print_deferred):
                        _deferred_post_prompt.appendleft((_r, _k))
                        break
                else:
                    _deferred_post_prompt.appendleft((_r, _k))
                    break

        # -- Event loop ----------------------------------------------------

        def _next_event():
            if _local_pending:
                return _local_pending.popleft()
            return self._queue.get()

        while True:
            event = _next_event()
            if event is _STOP:
                _flush_deferred(force=True)
                _close_live()
                break

            try:
                if isinstance(event, PrintEvent):
                    preview = _preview_text(event.renderable)
                    if preview:
                        _audit(
                            "print",
                            kind=event.kind,
                            prompt_active=_prompt_active(),
                            preview=preview,
                        )
                    _cprint(event.renderable, **event.kwargs)

                elif isinstance(event, LiveStartEvent):
                    _audit(
                        "stream_start",
                        agent=event.agent,
                        prompt_active=_prompt_active(),
                    )
                    with _renderer._lock:
                        container = _renderer._deck.get(event.agent)
                    sink = self._app_sink
                    if sink is not None:
                        _label = container.label if container else str(event.agent)
                        _style = (container.style if container else "dim") or "dim"
                        _ui = _rich()
                        _cprint(_ui.Rule(
                            f"[bold {_style}]{_ui.markup_escape(_label)}[/bold {_style}]",
                            style=f"dim {_style}",
                        ))
                        sink.mark_stream_start(event.agent)
                        _sink_sent_len[event.agent] = 0
                    elif container and container.streaming and container.stream_content.strip():
                        _ensure_live()
                        _refresh()

                elif isinstance(event, LiveUpdateChunkEvent):
                    batches = defaultdict(list)
                    batches[event.agent].append(event.chunk)
                    while True:
                        try:
                            next_ev = self._queue.get_nowait()
                        except _queue_module.Empty:
                            break
                        if isinstance(next_ev, LiveUpdateChunkEvent):
                            batches[next_ev.agent].append(next_ev.chunk)
                        else:
                            _local_pending.appendleft(next_ev)
                            break

                    for _a, chunks in batches.items():
                        container = _renderer._deck.get(_a)
                        if container:
                            for chunk in chunks:
                                if isinstance(chunk, dict):
                                    container.stream_content = _apply_stream_diff(
                                        container.stream_content,
                                        _normalize_stream_diff(chunk.get("diff")),
                                    )
                                    text = chunk.get("text")
                                    if text and not chunk.get("diff"):
                                        container.stream_content += strip_ansi(str(text))
                                else:
                                    container.stream_content += strip_ansi(str(chunk))
                            _audit(
                                "stream_chunk",
                                agent=_a,
                                chunk_count=len(chunks),
                                preview=_preview_chunk(chunks[-1]),
                                previews=[_preview_chunk(c) for c in chunks[:5]],
                                previews_truncated=len(chunks) > 5,
                            )
                    if batches:
                        sink = self._app_sink
                        if sink is not None:
                            for _a, _chunks in batches.items():
                                _c = _renderer._deck.get(_a)
                                if _c:
                                    _already = _sink_sent_len.get(_a, 0)
                                    _new_len = len(_c.stream_content)
                                    # Detect replace-style diffs (CLI agents' transient updates)
                                    _had_replace = any(
                                        isinstance(ch, dict) and ch.get("diff") and
                                        any(d.get("op") == "replace" for d in (ch.get("diff") or []))
                                        for ch in _chunks
                                    )
                                    if _had_replace or _new_len < _already:
                                        # Content was replaced in-place: overwrite from stream mark
                                        update_stream = getattr(sink, "update_stream", None)
                                        if callable(update_stream):
                                            update_stream(_a, _c.stream_content)
                                        else:
                                            sink.append_output(_c.stream_content[_already:])
                                        _sink_sent_len[_a] = _new_len
                                    else:
                                        _delta = _c.stream_content[_already:]
                                        if _delta:
                                            sink.append_output(_delta)
                                            _sink_sent_len[_a] = _new_len
                        else:
                            has_visible_content = any(
                                container.stream_content.strip()
                                for container in _renderer._deck.active_streams().values()
                            )
                            if has_visible_content:
                                _ensure_live()
                            _refresh()

                elif isinstance(event, LiveStopEvent):
                    _audit(
                        "stream_stop",
                        agent=event.agent,
                        render_mode=event.render_mode,
                        preview=_preview_text(event.final_content),
                    )
                    container = _renderer._deck.get(event.agent)
                    if container:
                        container.streaming = False
                        if _renderer._deck.active_streams():
                            _refresh()
                        else:
                            _close_live()
                        final_block = _renderer._render_turn_block(
                            container.stream_theme_name,
                            container.label,
                            container.style,
                            content=event.final_content,
                            include_header=True,
                            include_footer_rule=False,
                            render_mode=event.render_mode,
                        )
                        sink = self._app_sink
                        if sink is not None:
                            import io as _io
                            from rich.console import Console as _RichConsole
                            _buf = _io.StringIO()
                            _w = _renderer._console.width if _renderer._console else 80
                            _tmp = _RichConsole(file=_buf, force_terminal=True, width=_w, no_color=False)
                            _tmp.print(final_block)
                            _sink_sent_len.pop(event.agent, None)
                            sink.replace_stream(event.agent, _buf.getvalue())
                        else:
                            _cprint(final_block)

                elif isinstance(event, LiveAbortEvent):
                    _audit("stream_abort", agent=event.agent)
                    container = _renderer._deck.get(event.agent)
                    if container is not None:
                        container.streaming = False
                    if _renderer._deck.active_streams():
                        _refresh()
                    else:
                        _close_live()
                    sink = self._app_sink
                    if sink is not None and _sink_sent_len.pop(event.agent, 0) > 0:
                        sink.ensure_trailing_newline()

                elif isinstance(event, NoopEvent):
                    _flush_deferred(force=event.force_flush)
                    event.done.set()

                elif isinstance(event, OutputControlEvent):
                    if event.suspend:
                        self._output_suspended.set()
                        if event.render_anchored_windows:
                            _print_anchored_windows()
                        _close_live()
                    else:
                        self._output_suspended.clear()
                        _flush_deferred(force=True)
                        if _renderer._deck.active_streams() and any(
                            container.stream_content.strip()
                            for container in _renderer._deck.active_streams().values()
                        ):
                            _ensure_live()
                            _refresh()
                    if event.done is not None:
                        event.done.set()

                elif isinstance(event, TerminalResizeEvent):
                    self._overlay.reset()

                elif isinstance(event, PendingInputEvent):
                    with _renderer._lock:
                        container = _renderer._deck.get(event.agent)
                        if container is not None:
                            container.pending_kind = event.kind
                            container.pending_question = event.question
                    if event.agent in _renderer._deck.active_streams():
                        _refresh()

                elif isinstance(event, TransientWindowEvent):
                    if self._output_suspended.is_set() or self._app_sink is not None:
                        continue

                    while True:
                        try:
                            next_ev = self._queue.get_nowait()
                        except _queue_module.Empty:
                            break
                        if isinstance(next_ev, TransientWindowEvent):
                            event = next_ev
                        else:
                            _local_pending.appendleft(next_ev)
                            break

                    if not event.text:
                        continue

                    replace_overlay = self._overlay.build_replace(
                        event.text,
                        event.buf_version,
                        _get_version,
                        audit_fn=_audit,
                    )
                    run_above = _renderer._run_above_prompt_fn
                    if run_above is not None:
                        run_above(replace_overlay)
                    elif _renderer._console:
                        replace_overlay()

                elif isinstance(event, TransientClearEvent):
                    if self._output_suspended.is_set() or self._app_sink is not None:
                        continue
                    clear_overlay = self._overlay.build_clear(
                        event.buf_version,
                        _get_version,
                        audit_fn=_audit,
                    )
                    run_above = _renderer._run_above_prompt_fn
                    if run_above is not None:
                        run_above(clear_overlay)
                    else:
                        clear_overlay()
                    self.clear_combined_transient()

            except Exception:
                _log.exception(
                    "writer thread: erro ao processar evento %r", event
                )

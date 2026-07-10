"""Controle de modos de terminal para a UI Textual."""
from __future__ import annotations

import sys
from contextlib import contextmanager

from quimera.ui.textual.constants import TERMINAL_MODE_RESET as _TERMINAL_MODE_RESET


class _DiscardWriter:
    """Sink de escrita não-bloqueante usado enquanto o driver está suspenso.

    Ao suspender, o Textual para o ``WriterThread`` real (fila limitada a 30
    escritas). Se o event loop continuar emitindo frames — relógio do header,
    cursor, timers de drain — durante um processo externo, essas escritas caem
    na fila já parada e, após 30 itens, ``Queue.put`` bloqueia o event loop,
    impedindo a retomada e deixando o terminal preso. Descartar as escritas
    evita o deadlock sem corromper o terminal (o frame correto é repintado na
    retomada via ``refresh(layout=True)``).
    """

    def write(self, data: str) -> None:
        return None

    def flush(self) -> None:
        return None


def _install_discard_writer(driver) -> None:
    """Troca o writer parado do driver por um sink que nunca bloqueia."""
    if not hasattr(driver, "_writer_thread"):
        return
    try:
        driver._writer_thread = _DiscardWriter()
    except Exception:
        return


def _restore_terminal_modes() -> None:
    """Desativa modos interativos que não podem vazar para editor/shell."""
    stdout = getattr(sys, "__stdout__", None) or sys.stdout
    if stdout is None:
        return
    try:
        stdout.write(_TERMINAL_MODE_RESET)
        stdout.flush()
    except Exception:
        return


def _restore_textual_input_focus(textual_app) -> None:
    """Restaura foco e cursor do input fixo depois de janelas externas."""
    if textual_app is None:
        return
    try:
        input_widget = textual_app.query_one("#input")
    except Exception:
        return
    try:
        input_widget.focus()
    except Exception:
        pass
    try:
        input_widget.cursor_position = len(str(getattr(input_widget, "value", "") or ""))
    except Exception:
        pass


@contextmanager
def _external_textual_window(textual_app):
    """Suspende Textual para processo externo sem vazar modos de terminal."""
    if textual_app is None:
        _restore_terminal_modes()
        try:
            yield
        finally:
            _restore_terminal_modes()
        return

    driver = getattr(textual_app, "_driver", None)
    call_from_thread = getattr(textual_app, "call_from_thread", None)

    if driver is None or not callable(call_from_thread):
        suspend = getattr(textual_app, "suspend", None)
        if callable(suspend):
            with suspend():
                _restore_terminal_modes()
                try:
                    yield
                finally:
                    _restore_terminal_modes()
            _restore_textual_input_focus(textual_app)
            return
        _restore_terminal_modes()
        try:
            yield
        finally:
            _restore_terminal_modes()
        return

    can_suspend = bool(getattr(driver, "can_suspend", False))

    def _suspend_driver() -> None:
        if not can_suspend:
            return
        try:
            textual_app._suspend_signal()
        except Exception:
            pass
        driver.suspend_application_mode()
        # O writer real foi parado; troca por um sink não-bloqueante para que
        # repaints do loop durante o processo externo não travem o event loop.
        _install_discard_writer(driver)

    def _resume_driver() -> None:
        if not can_suspend:
            return
        driver.resume_application_mode()
        try:
            textual_app._resume_signal()
        except Exception:
            pass
        try:
            textual_app.refresh(layout=True)
        except Exception:
            pass
        _restore_textual_input_focus(textual_app)

    try:
        call_from_thread(_suspend_driver)
    except Exception:
        _restore_terminal_modes()
        try:
            yield
        finally:
            _restore_terminal_modes()
        return

    _restore_terminal_modes()
    try:
        yield
    finally:
        _restore_terminal_modes()
        resumed = False
        try:
            call_from_thread(_resume_driver)
            resumed = True
        finally:
            if not resumed:
                _restore_terminal_modes()


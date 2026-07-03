"""Controle de modos de terminal para a UI Textual."""
from __future__ import annotations

import sys
from contextlib import contextmanager

from quimera.ui.textual.constants import TERMINAL_MODE_RESET as _TERMINAL_MODE_RESET


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


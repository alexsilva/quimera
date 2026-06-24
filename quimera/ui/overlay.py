"""Transient overlay rendering helpers for the terminal compositor."""
from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from typing import Any


class TransientOverlay:
    """Manage progress overlay lines printed above an active prompt.

    ``lines[0]`` is the number of overlay lines currently on screen. The list is
    intentionally shared with the compositor so floor ownership transitions can
    clear any visible overlay before an external prompt takes over stdout.
    """

    def __init__(self, lines: list[int] | None = None):
        self._lines = lines if lines is not None else [0]

    @property
    def lines_on_screen(self) -> int:
        return self._lines[0]

    def reset(self) -> None:
        """Forget visible overlay lines, for example after a terminal resize."""
        self._lines[0] = 0

    def build_replace(
        self,
        text: str,
        version: int,
        get_version_fn: Callable[[], int],
        audit_fn: Callable[..., Any] | None = None,
    ):
        """Build a closure that replaces the overlay in-place.

        The closure always clears previous overlay lines, even when it is stale.
        A stale closure must not print obsolete text, but it still needs to erase
        the old overlay to prevent ghosting above the prompt.
        """
        lines = self._lines

        def _replace() -> None:
            previous_lines = lines[0]
            terminal_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
            cursor_up = min(previous_lines, max(0, terminal_lines - 3))
            current_version = get_version_fn()

            if audit_fn is not None:
                audit_fn(
                    "transient_replace",
                    buf_version=version,
                    prev_lines=previous_lines,
                    cursor_up=cursor_up,
                    term_lines=terminal_lines,
                    stale=(version < current_version),
                )

            if cursor_up > 0:
                sys.stdout.write(f"\033[{cursor_up}A\033[J")
            lines[0] = 0

            if version < current_version:
                if cursor_up > 0:
                    sys.stdout.flush()
                return

            max_visible = max(1, (terminal_lines - 3) // 3)
            visible_lines = text.split("\n")[-max_visible:]
            actual_text = "\n".join(visible_lines)

            sys.stdout.write(f"\033[2m{actual_text}\033[0m")
            sys.stdout.write("\n")
            sys.stdout.flush()
            lines[0] = len(visible_lines)

        return _replace

    def build_clear(
        self,
        version: int,
        get_version_fn: Callable[[], int],
        audit_fn: Callable[..., Any] | None = None,
    ):
        """Build a closure that clears the current overlay."""
        lines = self._lines

        def _clear() -> None:
            if version < get_version_fn():
                return
            previous_lines = lines[0]
            lines[0] = 0
            if audit_fn is not None:
                audit_fn("transient_clear", buf_version=version, prev_lines=previous_lines)
            if previous_lines > 0:
                sys.stdout.write(f"\033[{previous_lines}A\033[J")
                sys.stdout.flush()

        return _clear

    def build_print_above(
        self,
        renderable,
        kwargs: dict,
        console,
        bump_version_fn: Callable[[], int],
        audit_fn: Callable[..., Any] | None = None,
    ):
        """Build a closure that clears overlay before permanent output."""
        lines = self._lines

        def _clear_and_print() -> None:
            previous_lines = lines[0]
            lines[0] = 0
            bump_version_fn()
            if audit_fn is not None:
                audit_fn("transient_print_above", prev_lines=previous_lines)
            if previous_lines > 0:
                sys.stdout.write(f"\033[{previous_lines}A\033[J")
            console.print(renderable, **kwargs)

        return _clear_and_print

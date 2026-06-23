"""Window management policy for Quimera terminal UI.

The manager owns window lifecycle and stacking decisions. It deliberately does
not render or touch stdout; TerminalRenderer remains the terminal compositor for
now and asks this manager to mount/close windows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .windows import (
    RenderWindowState,
    RestorePolicy,
    WindowDeck,
    WindowKind,
    WindowLayer,
    WindowModality,
)


@dataclass(frozen=True)
class WindowRenderPlan:
    """Terminal-compositor actions requested by a window transition.

    The manager declares these actions but does not execute them.
    """

    suspend_output: bool = False
    clear_overlay: bool = False
    resume_output: bool = False


@dataclass(frozen=True)
class WindowTransition:
    """Policy result produced by mounting or closing a window."""

    window: RenderWindowState | None
    exclusive_terminal: bool = False
    restore_deck_after_close: bool = False
    render_plan: WindowRenderPlan = WindowRenderPlan()


class WindowManager:
    """Policy layer over WindowDeck.

    WindowDeck stores declarative state. WindowManager decides how lifecycle
    operations affect modality and restoration policy. The renderer then applies
    the returned transition to the terminal.
    """

    def __init__(self, deck: WindowDeck):
        self.deck = deck
        self.modal_stack: list[str] = []

    def make_floor_window(
        self,
        window_id: str,
        *,
        kind: WindowKind | str = WindowKind.TERMINAL_FLOOR,
        title: str = "Terminal floor",
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RenderWindowState:
        return RenderWindowState(
            id=window_id,
            kind=self._coerce_kind(kind),
            layer=WindowLayer.MODAL,
            modality=WindowModality.EXCLUSIVE_TERMINAL,
            owner=owner,
            title=title,
            restore_policy=RestorePolicy.RESTORE_DECK_AFTER_CLOSE,
            metadata=metadata or {},
        )

    def make_external_window(
        self,
        window_id: str,
        *,
        kind: WindowKind | str = WindowKind.EDITOR,
        title: str = "",
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RenderWindowState:
        return RenderWindowState(
            id=window_id,
            kind=self._coerce_kind(kind),
            layer=WindowLayer.EXTERNAL,
            modality=WindowModality.EXCLUSIVE_TERMINAL,
            owner=owner,
            title=title,
            restore_policy=RestorePolicy.RESTORE_DECK_AFTER_CLOSE,
            metadata=metadata or {},
        )

    def mount(self, window: RenderWindowState) -> WindowTransition:
        mounted = window
        self.deck.managed_windows[mounted.id] = mounted
        if mounted.modality in {
            WindowModality.BLOCKING,
            WindowModality.EXCLUSIVE_TERMINAL,
        }:
            if mounted.id in self.modal_stack:
                self.modal_stack.remove(mounted.id)
            self.modal_stack.append(mounted.id)
        return WindowTransition(
            window=mounted,
            exclusive_terminal=mounted.modality == WindowModality.EXCLUSIVE_TERMINAL,
            restore_deck_after_close=mounted.restore_policy
            == RestorePolicy.RESTORE_DECK_AFTER_CLOSE,
            render_plan=self._mount_render_plan(mounted),
        )

    def close(self, window_id: str) -> WindowTransition:
        closed = self.deck.managed_windows.pop(window_id, None)
        if window_id in self.modal_stack:
            self.modal_stack.remove(window_id)
        if closed is None:
            return WindowTransition(window=None)
        closed.active = False
        return WindowTransition(
            window=closed,
            exclusive_terminal=closed.modality == WindowModality.EXCLUSIVE_TERMINAL,
            restore_deck_after_close=closed.restore_policy
            == RestorePolicy.RESTORE_DECK_AFTER_CLOSE,
            render_plan=self._close_render_plan(closed),
        )

    def active_exclusive_window(self) -> RenderWindowState | None:
        for window_id in reversed(self.modal_stack):
            window = self.deck.managed_windows.get(window_id)
            if window is not None and window.modality == WindowModality.EXCLUSIVE_TERMINAL:
                return window
        return None

    @staticmethod
    def _coerce_kind(kind: WindowKind | str) -> WindowKind:
        if isinstance(kind, WindowKind):
            return kind
        return WindowKind(str(kind))

    @staticmethod
    def _mount_render_plan(window: RenderWindowState) -> WindowRenderPlan:
        if window.modality == WindowModality.EXCLUSIVE_TERMINAL:
            return WindowRenderPlan(suspend_output=True, clear_overlay=True)
        return WindowRenderPlan()

    @staticmethod
    def _close_render_plan(window: RenderWindowState) -> WindowRenderPlan:
        if window.modality == WindowModality.EXCLUSIVE_TERMINAL:
            return WindowRenderPlan(resume_output=True)
        return WindowRenderPlan()

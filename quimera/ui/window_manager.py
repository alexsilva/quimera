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
    WindowAnchor,
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
    render_anchored_windows: bool = False


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
        anchor: WindowAnchor | str = WindowAnchor.TERMINAL_FLOOR,
        metadata: dict[str, Any] | None = None,
    ) -> RenderWindowState:
        """Create a modal floor-backed window with explicit placement policy."""
        return RenderWindowState(
            id=window_id,
            kind=self._coerce_kind(kind),
            layer=WindowLayer.MODAL,
            modality=WindowModality.EXCLUSIVE_TERMINAL,
            owner=owner,
            anchor=self._coerce_anchor(anchor),
            title=title,
            restore_policy=RestorePolicy.RESTORE_DECK_AFTER_CLOSE,
            metadata=metadata or {},
        )

    def make_terminal_floor_window(
        self,
        window_id: str,
        *,
        title: str = "Terminal floor",
        owner: str | None = None,
        anchor: WindowAnchor | str = WindowAnchor.TERMINAL_FLOOR,
        metadata: dict[str, Any] | None = None,
    ) -> RenderWindowState:
        """Create an explicit low-level terminal floor window."""
        return self.make_floor_window(
            window_id,
            kind=WindowKind.TERMINAL_FLOOR,
            title=title,
            owner=owner,
            anchor=anchor,
            metadata=metadata or {},
        )

    def make_approval_window(
        self,
        window_id: str,
        *,
        title: str = "Aprovação",
        owner: str | None = None,
        anchor: WindowAnchor | str = WindowAnchor.AFTER_OWNER,
        metadata: dict[str, Any] | None = None,
    ) -> RenderWindowState:
        """Create an approval modal with exclusive terminal ownership."""
        return self.make_floor_window(
            window_id,
            kind=WindowKind.APPROVAL,
            title=title,
            owner=owner,
            anchor=anchor,
            metadata=metadata or {},
        )

    def make_input_window(
        self,
        window_id: str,
        *,
        title: str = "Entrada",
        owner: str | None = None,
        anchor: WindowAnchor | str = WindowAnchor.AFTER_OWNER,
        metadata: dict[str, Any] | None = None,
    ) -> RenderWindowState:
        """Create an input modal with exclusive terminal ownership."""
        return self.make_floor_window(
            window_id,
            kind=WindowKind.INPUT,
            title=title,
            owner=owner,
            anchor=anchor,
            metadata=metadata or {},
        )

    def make_selection_window(
        self,
        window_id: str,
        *,
        title: str = "Seleção",
        owner: str | None = None,
        anchor: WindowAnchor | str = WindowAnchor.AFTER_OWNER,
        metadata: dict[str, Any] | None = None,
    ) -> RenderWindowState:
        """Create a selection modal with exclusive terminal ownership."""
        return self.make_floor_window(
            window_id,
            kind=WindowKind.SELECTION,
            title=title,
            owner=owner,
            anchor=anchor,
            metadata=metadata or {},
        )

    def make_external_window(
        self,
        window_id: str,
        *,
        kind: WindowKind | str = WindowKind.EDITOR,
        title: str = "",
        owner: str | None = None,
        anchor: WindowAnchor | str = WindowAnchor.TERMINAL_FLOOR,
        metadata: dict[str, Any] | None = None,
    ) -> RenderWindowState:
        return RenderWindowState(
            id=window_id,
            kind=self._coerce_kind(kind),
            layer=WindowLayer.EXTERNAL,
            modality=WindowModality.EXCLUSIVE_TERMINAL,
            owner=owner,
            anchor=self._coerce_anchor(anchor),
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
        """Return the topmost active exclusive terminal window."""
        for window_id in reversed(self.modal_stack):
            window = self.deck.managed_windows.get(window_id)
            if window is not None and window.modality == WindowModality.EXCLUSIVE_TERMINAL:
                return window
        return None

    def visible_windows(self) -> list[RenderWindowState]:
        """Return active managed windows in deck insertion order."""
        return [
            window
            for window in self.deck.managed_windows.values()
            if window.active
        ]

    def windows_by_layer(self, layer: WindowLayer | str) -> list[RenderWindowState]:
        """Return active managed windows that belong to one render layer."""
        target_layer = layer if isinstance(layer, WindowLayer) else WindowLayer(str(layer))
        return [window for window in self.visible_windows() if window.layer == target_layer]

    def anchored_children(self, owner: str) -> list[RenderWindowState]:
        """Return active windows declaratively anchored after an owner."""
        return [
            window
            for window in self.visible_windows()
            if window.owner == owner and window.anchor == WindowAnchor.AFTER_OWNER
        ]

    def render_order(self) -> list[RenderWindowState]:
        """Return active managed windows with children placed after owners."""
        visible = self.visible_windows()
        visible_by_id = {window.id: window for window in visible}
        emitted: set[str] = set()
        ordered: list[RenderWindowState] = []

        def emit(window: RenderWindowState) -> None:
            if window.id in emitted:
                return
            emitted.add(window.id)
            ordered.append(window)
            for child in self.anchored_children(window.id):
                emit(child)

        for window in visible:
            if window.anchor == WindowAnchor.AFTER_OWNER and window.owner in visible_by_id:
                continue
            emit(window)
        return ordered

    @staticmethod
    def _coerce_kind(kind: WindowKind | str) -> WindowKind:
        """Normalize caller-provided window kind values."""
        if isinstance(kind, WindowKind):
            return kind
        return WindowKind(str(kind))

    @staticmethod
    def _coerce_anchor(anchor: WindowAnchor | str) -> WindowAnchor:
        """Normalize caller-provided window anchor values."""
        if isinstance(anchor, WindowAnchor):
            return anchor
        return WindowAnchor(str(anchor))

    @staticmethod
    def _mount_render_plan(window: RenderWindowState) -> WindowRenderPlan:
        if window.modality == WindowModality.EXCLUSIVE_TERMINAL:
            render_anchored_windows = window.kind in {
                WindowKind.APPROVAL,
                WindowKind.INPUT,
                WindowKind.SELECTION,
            }
            return WindowRenderPlan(
                suspend_output=True,
                clear_overlay=True,
                render_anchored_windows=render_anchored_windows,
            )
        return WindowRenderPlan()

    @staticmethod
    def _close_render_plan(window: RenderWindowState) -> WindowRenderPlan:
        if window.modality == WindowModality.EXCLUSIVE_TERMINAL:
            return WindowRenderPlan(resume_output=True)
        return WindowRenderPlan()

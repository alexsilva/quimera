from quimera.ui.window_manager import WindowManager
from quimera.ui.windows import (
    RestorePolicy,
    WindowDeck,
    WindowKind,
    WindowModality,
)


def test_mount_exclusive_window_updates_modal_stack_and_transition():
    deck = WindowDeck()
    manager = WindowManager(deck)
    window = manager.make_external_window("external:editor", title="Editor externo")

    transition = manager.mount(window)

    assert transition.window is window
    assert transition.exclusive_terminal is True
    assert transition.restore_deck_after_close is True
    assert transition.render_plan.suspend_output is True
    assert transition.render_plan.clear_overlay is True
    assert transition.render_plan.resume_output is False
    assert deck.managed_windows["external:editor"] is window
    assert manager.modal_stack == ["external:editor"]
    assert manager.active_exclusive_window() is window


def test_close_window_removes_stack_and_marks_inactive():
    deck = WindowDeck()
    manager = WindowManager(deck)
    window = manager.make_floor_window(
        "floor:1",
        kind=WindowKind.INPUT,
        title="Entrada",
    )
    manager.mount(window)

    transition = manager.close("floor:1")

    assert transition.window is window
    assert transition.exclusive_terminal is True
    assert transition.restore_deck_after_close is True
    assert transition.render_plan.suspend_output is False
    assert transition.render_plan.clear_overlay is False
    assert transition.render_plan.resume_output is True
    assert window.active is False
    assert deck.managed_windows == {}
    assert manager.modal_stack == []
    assert manager.active_exclusive_window() is None


def test_non_modal_agent_window_does_not_enter_modal_stack():
    deck = WindowDeck()
    manager = WindowManager(deck)
    window = manager.make_floor_window("floor:input", kind="input")
    window.modality = WindowModality.NON_BLOCKING
    window.restore_policy = RestorePolicy.KEEP

    transition = manager.mount(window)

    assert transition.exclusive_terminal is False
    assert transition.restore_deck_after_close is False
    assert transition.render_plan.suspend_output is False
    assert transition.render_plan.clear_overlay is False
    assert transition.render_plan.resume_output is False
    assert manager.modal_stack == []

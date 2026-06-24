from quimera.ui.window_manager import WindowManager
from quimera.ui.windows import (
    RestorePolicy,
    WindowAnchor,
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
    assert transition.render_plan.render_anchored_windows is False
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


def test_interactive_factories_create_semantic_exclusive_windows():
    """Interactive factories centralize kind, title and terminal policy."""
    deck = WindowDeck()
    manager = WindowManager(deck)

    approval = manager.make_approval_window("floor:approval")
    input_window = manager.make_input_window("floor:input")
    selection = manager.make_selection_window("floor:selection")
    terminal_floor = manager.make_terminal_floor_window("floor:terminal")

    assert approval.kind == WindowKind.APPROVAL
    assert approval.title == "Aprovação"
    assert input_window.kind == WindowKind.INPUT
    assert input_window.title == "Entrada"
    assert selection.kind == WindowKind.SELECTION
    assert selection.title == "Seleção"
    assert terminal_floor.kind == WindowKind.TERMINAL_FLOOR
    assert approval.anchor == WindowAnchor.AFTER_OWNER
    assert input_window.anchor == WindowAnchor.AFTER_OWNER
    assert selection.anchor == WindowAnchor.AFTER_OWNER
    assert terminal_floor.anchor == WindowAnchor.TERMINAL_FLOOR
    for window in [approval, input_window, selection, terminal_floor]:
        assert window.modality == WindowModality.EXCLUSIVE_TERMINAL
        assert window.restore_policy == RestorePolicy.RESTORE_DECK_AFTER_CLOSE


def test_interactive_factories_preserve_owner_anchor_metadata():
    """Interactive windows retain owner and anchor for future child rendering."""
    deck = WindowDeck()
    manager = WindowManager(deck)

    window = manager.make_selection_window(
        "floor:selection",
        owner="opencode",
        anchor=WindowAnchor.AFTER_OWNER,
        metadata={"question": "Escolha"},
    )

    assert window.owner == "opencode"
    assert window.anchor == WindowAnchor.AFTER_OWNER
    assert window.metadata == {"question": "Escolha"}


def test_interactive_mount_plans_render_anchored_windows_before_prompt():
    """Interactive windows request declarative anchored rendering, not snapshots."""
    deck = WindowDeck()
    manager = WindowManager(deck)

    approval = manager.mount(manager.make_approval_window("floor:approval"))
    manager.close("floor:approval")
    input_window = manager.mount(manager.make_input_window("floor:input"))
    manager.close("floor:input")
    selection = manager.mount(manager.make_selection_window("floor:selection"))
    manager.close("floor:selection")
    terminal_floor = manager.mount(manager.make_terminal_floor_window("floor:terminal"))

    assert approval.render_plan.render_anchored_windows is True
    assert input_window.render_plan.render_anchored_windows is True
    assert selection.render_plan.render_anchored_windows is True
    assert terminal_floor.render_plan.render_anchored_windows is False


def test_interactive_mount_plan_clears_overlay_before_snapshot():
    """Interactive anchored windows clear overlay before rendering prompts."""
    deck = WindowDeck()
    manager = WindowManager(deck)

    transition = manager.mount(manager.make_approval_window("floor:approval"))

    assert transition.render_plan.clear_overlay is True
    assert transition.render_plan.render_anchored_windows is True


def test_visible_windows_returns_only_active_managed_windows():
    """Visible windows exclude closed managed state."""
    deck = WindowDeck()
    manager = WindowManager(deck)
    first = manager.make_terminal_floor_window("floor:first")
    second = manager.make_input_window("floor:second", owner="codex")

    manager.mount(first)
    manager.mount(second)
    manager.close("floor:first")

    assert manager.visible_windows() == [second]
    assert manager.windows_by_layer("modal") == [second]


def test_anchored_children_returns_prompt_windows_for_owner():
    """Anchored children select active AFTER_OWNER windows for an owner."""
    deck = WindowDeck()
    manager = WindowManager(deck)
    approval = manager.make_approval_window("floor:approval", owner="codex")
    selection = manager.make_selection_window("floor:selection", owner="codex")
    other = manager.make_input_window("floor:input", owner="claude")

    manager.mount(approval)
    manager.mount(selection)
    manager.mount(other)

    assert manager.anchored_children("codex") == [approval, selection]


def test_render_order_places_children_after_managed_owner():
    """Render order emits AFTER_OWNER children directly after their owner."""
    deck = WindowDeck()
    manager = WindowManager(deck)
    owner = manager.make_terminal_floor_window("agent:codex")
    child = manager.make_approval_window("floor:approval", owner="agent:codex")
    other = manager.make_terminal_floor_window("floor:other")

    manager.mount(owner)
    manager.mount(child)
    manager.mount(other)

    assert manager.render_order() == [owner, child, other]


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

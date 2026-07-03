"""Tests for explicit Textual direct-input state."""

from quimera.ui.textual.direct_input import DirectInputState


def test_direct_input_state_tracks_nested_activity_and_resets_metadata():
    state = DirectInputState()

    assert state.active is False

    state.begin(owner="claude", kind="approval")
    state.begin()

    assert state.active is True
    assert state.depth == 2
    assert state.owner == "claude"
    assert state.kind == "approval"

    state.end()

    assert state.active is True
    assert state.owner == "claude"
    assert state.kind == "approval"

    state.end()

    assert state.active is False
    assert state.depth == 0
    assert state.owner is None
    assert state.kind is None


def test_direct_input_state_never_goes_negative():
    state = DirectInputState()

    state.end()

    assert state.depth == 0
    assert state.active is False

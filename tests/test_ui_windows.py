from quimera.ui.windows import AgentWindowState, sanitize_window_text


def test_sanitize_window_text_removes_ansi_and_control_characters():
    assert sanitize_window_text("\x1b[31mred\x1b[0m\u200b") == "red"


def test_agent_window_state_composes_question_with_sanitized_options():
    window = AgentWindowState(agent="codex", label="Codex", style="green")

    assert window.compose_question("\x1b[31mEscolha\x1b[0m", ["um", "\x1b[32mdois\x1b[0m"]) == (
        "Escolha\n"
        "  1. um\n"
        "  2. dois"
    )


def test_agent_window_state_rolls_transient_buffer_and_deduplicates_tail():
    window = AgentWindowState(
        agent="codex",
        label="Codex",
        style="green",
        transient_limit=2,
    )

    assert window.push_transient("a") is True
    assert window.push_transient("a") is False
    assert window.push_transient("b") is True
    assert window.push_transient("c") is True

    assert window.transient == ["b", "c"]


def test_agent_window_state_clears_transient_state():
    window = AgentWindowState(agent="codex", label="Codex", style="green")
    window.transient_active = True
    window.push_transient("work")

    window.clear_transient_buffer()

    assert window.transient == []
    assert window.transient_active is False

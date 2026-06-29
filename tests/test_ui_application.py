from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.controls import UIContent

from quimera.constants import CMD_EXIT
from quimera.ui.application import QuimeraApplication


def _content(line_count: int) -> UIContent:
    return UIContent(
        get_line=lambda i: [("", f"line {i}")],
        line_count=line_count,
        cursor_position=Point(x=0, y=max(0, line_count - 1)),
        show_cursor=False,
    )


def _render_output_window(
    app: QuimeraApplication,
    line_count: int,
    height: int = 5,
) -> int:
    app._output_window._scroll_when_linewrapping(
        _content(line_count),
        width=80,
        height=height,
    )
    return app._output_window.vertical_scroll


def test_split_application_keeps_terminal_scrollback_by_default():
    app = QuimeraApplication()

    assert app._app.full_screen is False
    assert app._app.renderer.mouse_support() is False


def test_split_chat_input_can_grow_without_fullscreen():
    app = QuimeraApplication()

    assert app._input_area.buffer.multiline() is True
    assert app._input_area.window.height.max == 5
    assert app._app.full_screen is False


def test_split_output_pane_reserves_visible_history_without_fullscreen():
    app = QuimeraApplication()

    assert app._output_window.height.min >= 8
    assert app._output_window.height.preferred >= app._output_window.height.min
    assert app._app.full_screen is False


def test_split_application_uses_persistent_history_file(tmp_path):
    app = QuimeraApplication(history_file=str(tmp_path / "history.txt"))

    assert app._input_area.buffer.history.__class__.__name__ == "FileHistory"


def test_split_application_keeps_migrated_callbacks():
    cancel = lambda: True
    theme = lambda: None

    app = QuimeraApplication(cancel_agent_fn=cancel, theme_cycle_fn=theme)

    assert app._cancel_agent_fn is cancel
    assert app._theme_cycle_fn is theme


def test_split_application_exit_command_closes_prompt_app():
    submitted = []
    app = QuimeraApplication(submit_fn=submitted.append)
    exited = []

    class DummyApp:
        def exit(self):
            exited.append(True)

    class DummyBuffer:
        text = CMD_EXIT

    app._app = DummyApp()

    app._on_submit(DummyBuffer())

    assert submitted == [CMD_EXIT]
    assert exited == [True]


def test_split_submit_falls_back_to_submit_when_not_injected():
    submitted = []
    app = QuimeraApplication(submit_fn=submitted.append, inject_fn=lambda _text: False)

    class DummyBuffer:
        text = "hello"

    app._on_submit(DummyBuffer())

    assert submitted == ["hello"]
    assert app._awaiting_response is True


def test_split_submit_does_not_queue_when_injected():
    submitted = []
    injected = []
    app = QuimeraApplication(
        submit_fn=submitted.append,
        inject_fn=lambda text: injected.append(text) or True,
    )

    class DummyBuffer:
        text = "continue"

    app._on_submit(DummyBuffer())

    assert injected == ["continue"]
    assert submitted == []
    assert app._awaiting_response is True


def test_split_output_window_follows_tail_by_default():
    app = QuimeraApplication()

    top = _render_output_window(app, line_count=30, height=5)

    assert top == 25
    assert app._output_follow_tail is True
    assert app._output_max_scroll_top == 25


def test_split_output_window_preserves_manual_scroll_when_new_output_arrives():
    app = QuimeraApplication()
    _render_output_window(app, line_count=30, height=5)

    app.scroll_output_lines(-3)
    top = _render_output_window(app, line_count=30, height=5)

    assert top == 22
    assert app._output_follow_tail is False

    top_after_new_output = _render_output_window(app, line_count=35, height=5)

    assert top_after_new_output == 22
    assert app._output_follow_tail is False
    assert app._output_max_scroll_top == 30


def test_split_output_window_resumes_following_tail_at_bottom():
    app = QuimeraApplication()
    _render_output_window(app, line_count=30, height=5)
    app.scroll_output_lines(-3)
    _render_output_window(app, line_count=35, height=5)

    app.scroll_output_to_bottom()
    top = _render_output_window(app, line_count=35, height=5)

    assert top == 30
    assert app._output_follow_tail is True


def test_split_output_window_tails_inside_a_wrapped_line():
    app = QuimeraApplication()
    content = UIContent(
        get_line=lambda i: [("", "x" * 200)],
        line_count=1,
        cursor_position=Point(x=199, y=0),
        show_cursor=False,
    )

    app._output_window._scroll_when_linewrapping(content, width=20, height=3)

    assert app._output_window.vertical_scroll == 0
    assert app._output_window.vertical_scroll_2 > 0
    assert app._output_follow_tail is True


# ------------------------------------------------------------------
# update_stream / replace_stream region tests
# ------------------------------------------------------------------

def test_update_stream_replaces_from_mark():
    app = QuimeraApplication()
    app.append_output("before\n")
    app.mark_stream_start("a")
    app.append_output("status 1\n")
    app.update_stream("a", "status 2\n")
    assert app._output_text == "before\nstatus 2\n"
    assert app._stream_marks["a"] is not None


def test_update_stream_keeps_mark_for_replace_stream():
    app = QuimeraApplication()
    app.mark_stream_start("a")
    app.update_stream("a", "live text\n")
    app.replace_stream("a", "final text\n")
    assert app._output_text == "final text\n"
    assert "a" not in app._stream_marks


def test_update_stream_multiple_sequential_replaces():
    app = QuimeraApplication()
    app.mark_stream_start("a")
    app.update_stream("a", "status 1\n")
    app.update_stream("a", "status 2\n")
    app.update_stream("a", "status 3\n")
    assert app._output_text == "status 3\n"
    app.replace_stream("a", "done\n")
    assert app._output_text == "done\n"


def test_replace_stream_without_update_stream():
    app = QuimeraApplication()
    app.append_output("prefix\n")
    app.mark_stream_start("a")
    app.append_output("stream delta\n")
    app.replace_stream("a", "final\n")
    assert app._output_text == "prefix\nfinal\n"


def test_update_stream_replaces_to_end():
    app = QuimeraApplication()
    app.append_output("before\n")
    app.mark_stream_start("a")
    app.append_output("v1\n")
    app.append_output("after\n")
    app.update_stream("a", "v2\n")
    # update_stream replaces from mark to end (entire tail replaced)
    assert app._output_text == "before\nv2\n"

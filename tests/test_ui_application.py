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

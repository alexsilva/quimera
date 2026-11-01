"""Regressões do limite visual do pensamento vivo no feed Textual."""

from io import StringIO
import re

from rich.console import Console

from quimera.ui.textual import renderables


def _render_live_body(text: str, width: int) -> list[str]:
    stream = StringIO()
    console = Console(file=stream, width=width, color_system=None)
    console.print(renderables._build_agent_live_body(text, None, "cyan"))
    return stream.getvalue().splitlines()


def test_live_tail_limits_single_wrapped_paragraph_by_visual_lines():
    lines = _render_live_body("x" * 2000, 56)

    assert len(lines) == renderables._LIVE_TAIL_LINE_LIMIT + 1
    assert "⋮ +32 linhas anteriores" in lines[0]
    assert "✻ " in lines[1]


def test_live_tail_limits_explicit_line_breaks_with_visual_counter():
    text = "\n".join(f"linha {index}" for index in range(20))
    lines = _render_live_body(text, 56)

    assert len(lines) == renderables._LIVE_TAIL_LINE_LIMIT + 1
    assert "⋮ +12 linhas anteriores" in lines[0]
    assert "linha 12" in lines[1]
    assert "linha 19" in lines[-1]


def test_live_tail_keeps_short_text_without_counter():
    lines = _render_live_body("pensamento curto", 56)

    assert len(lines) == 1
    assert "pensamento curto" in lines[0]
    assert "linhas anteriores" not in lines[0]


def test_live_tail_reflows_and_recounts_when_width_changes():
    narrow = _render_live_body("x" * 2000, 56)
    wide = _render_live_body("x" * 2000, 120)

    assert "⋮ +32 linhas anteriores" in narrow[0]
    assert "⋮ +10 linhas anteriores" in wide[0]
    assert narrow[1:] != wide[1:]


def test_live_tail_caps_giant_unbroken_paragraph_at_eight_visual_lines():
    lines = _render_live_body("x" * 60_000, 56)

    assert len(lines) == renderables._LIVE_TAIL_LINE_LIMIT + 1
    match = re.search(r"⋮ \+(\d+) linhas anteriores", lines[0])
    assert match is not None
    assert int(match.group(1)) > 0
    assert "✻ " in lines[1]

    for width in (4, 8, 12, 120):
        reflowed = _render_live_body("x" * 60_000, width)
        assert len(reflowed) == renderables._LIVE_TAIL_LINE_LIMIT + 1

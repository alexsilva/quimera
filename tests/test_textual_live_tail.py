"""Regressões do limite visual do pensamento vivo no feed Textual."""

from io import StringIO
import math
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


def _hidden_visual_lines(text: str, width: int) -> int:
    first = _render_live_body(text, width)[0]
    match = re.search(r"⋮ \+(\d+) linhas anteriores", first)
    assert match is not None
    return int(match.group(1))


def test_live_tail_counts_ascii_200k_prefix_by_wrapped_cells():
    width = 56
    content_width = width - renderables._GUTTER_WIDTH - 2
    text = "x" * 200_000
    tail, _ = renderables._live_tail(text)
    hidden_prefix = len(text) - len(tail)
    tail_overflow = math.ceil(len(tail) / content_width) - renderables._LIVE_TAIL_LINE_LIMIT

    assert _hidden_visual_lines(text, width) == (
        math.ceil(hidden_prefix / content_width) + tail_overflow
    )


def test_live_tail_counts_multibyte_prefix_with_cell_width():
    width = 56
    content_width = width - renderables._GUTTER_WIDTH - 2
    text = "🙂" * 100_000
    tail, _ = renderables._live_tail(text)
    # O prefixo oculto vale 2 células por emoji (não 1 por caractere); a cauda
    # visível quebra de verdade, com só content_width // 2 emojis por linha.
    prefix_cells = renderables.cell_len(text[: len(text) - len(tail)])
    assert prefix_cells == 2 * (len(text) - len(tail))
    tail_overflow = (
        math.ceil(len(tail) / (content_width // 2))
        - renderables._LIVE_TAIL_LINE_LIMIT
    )

    assert _hidden_visual_lines(text, width) == (
        math.ceil(prefix_cells / content_width) + tail_overflow
    )


def test_live_tail_counts_partial_giant_logical_line_proportionally():
    width = 56
    content_width = width - renderables._GUTTER_WIDTH - 2
    giant = "x" * 20_000
    text = "\n".join([*(f"curta {index}" for index in range(8)), giant])
    tail, hidden_label = renderables._live_tail(text)
    hidden_logical = int(re.search(r"\+(\d+)", hidden_label).group(1))
    partial_cells = len(giant) - len(tail)
    tail_overflow = math.ceil(len(tail) / content_width) - renderables._LIVE_TAIL_LINE_LIMIT

    assert _hidden_visual_lines(text, width) == (
        hidden_logical
        + math.ceil(partial_cells / content_width)
        + tail_overflow
    )


def test_live_body_ascii_200k_never_measures_the_whole_document(monkeypatch):
    measured: list[int] = []
    original_cell_len = renderables.cell_len

    def spy_cell_len(text: str, *args, **kwargs) -> int:
        measured.append(len(text))
        return original_cell_len(text, *args, **kwargs)

    monkeypatch.setattr(renderables, "cell_len", spy_cell_len)
    text = "x" * 200_000

    _render_live_body(text, 56)

    # Prefixo ASCII é contado por len(); nada perto do documento vai a cell_len.
    assert max(measured, default=0) < len(text) // 10

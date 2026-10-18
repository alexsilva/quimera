"""Testes dos helpers de texto de `quimera.ui` (sem renderer)."""
import pytest
from rich.console import Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from quimera.ui import (
    _agent_style,
    _extract_text_from_renderable,
    _highlight_tags,
    strip_ansi,
)
from quimera.runtime.streaming import apply_stream_diff, normalize_stream_diff


class TestStripAnsi:
    """Test suite for strip_ansi function."""

    def test_strips_real_ansi_escape(self):
        """Test removal of real ANSI escape sequences."""
        text = "\x1b[31mred text\x1b[0m"
        result = strip_ansi(text)
        assert "red text" in result
        assert "\x1b[" not in result

    def test_strips_orphaned_ansi_brackets(self):
        """Test removal of orphaned ANSI-like brackets."""
        text = "text [1m more [?25h text"
        result = strip_ansi(text)
        assert "text  more  text" in result
        assert "[1m" not in result
        assert "[?25h" not in result

    def test_preserves_rich_markup_like_brackets(self):
        """Test that Rich markup like [bold] is preserved."""
        text = "This is [bold]bold[/bold] text"
        result = strip_ansi(text)
        assert "[bold]" in result
        assert "[/bold]" in result

    def test_strips_unicode_control_and_bidi_chars(self):
        """Test removal of zero-width and bidi control characters."""
        text = "ab\u200bcd\u202ertl\u2069ef"
        result = strip_ansi(text)
        assert result == "abcdrtlef"


class TestAgentStyle:
    """Test suite for _agent_style function."""

    def test_returns_profile_style(self):
        """Test returns style from injected get_profile_style callable."""
        def get_profile_style(agent):
            if agent == "testagent":
                return ("cyan", "🤖  TestAgent")
            return None

        color, label = _agent_style("testagent", get_profile_style=get_profile_style)
        assert color == "cyan"
        assert label == "🤖  TestAgent"

    def test_fallback_for_unknown_agent(self):
        """Test fallback when get_profile_style returns None."""
        color, label = _agent_style("unknownagent")
        assert color == "white"
        assert label == "🤖  Unknownagent"

    def test_fallback_for_none_agent(self):
        """Test fallback is safe when agent is None."""
        color, label = _agent_style(None)
        assert color == "white"
        assert label == "🤖  Unknown"


class TestStreamingDiffHelpers:
    def test_normalize_stream_diff_accepts_dict(self):
        assert normalize_stream_diff({"op": "add", "content": "abc"}) == [{"op": "add", "text": "abc"}]

    def test_apply_stream_diff_supports_replace_and_add(self):
        assert apply_stream_diff("old", [{"op": "replace", "text": "new"}, {"op": "add", "text": "!"}]) == "new!"


class TestExtractTextFromRenderable:
    def test_returns_empty_string_for_none(self):
        assert _extract_text_from_renderable(None) == ""

    def test_returns_string_unchanged(self):
        assert _extract_text_from_renderable("hello") == "hello"

    def test_extracts_plain_from_text(self):
        t = Text("hello world")
        assert _extract_text_from_renderable(t) == "hello world"

    def test_extracts_text_from_group(self):
        g = Group(Text("first"), Text("second"))
        result = _extract_text_from_renderable(g)
        assert "first" in result
        assert "second" in result

    def test_nested_group_extraction(self):
        inner = Group(Text("a"), Text("b"))
        outer = Group(inner, Text("c"))
        result = _extract_text_from_renderable(outer)
        assert "a" in result
        assert "b" in result
        assert "c" in result

    def test_extracts_text_from_rule_title(self):
        result = _extract_text_from_renderable(Rule("section"))
        assert result == "section"

    def test_extracts_text_from_table_cells(self):
        table = Table()
        table.add_column("Ferramenta")
        table.add_column("Status")
        table.add_row("exec", "ok")
        result = _extract_text_from_renderable(table)
        assert "Ferramenta" in result
        assert "Status" in result
        assert "exec" in result
        assert "ok" in result


class TestHighlightTags:
    """Test suite for _highlight_tags function."""

    @pytest.mark.parametrize(
        ("source", "expected_spans"),
        [
            pytest.param("<tool>", 1, id="tag-simples"),
            pytest.param("</tool>", 1, id="tag-fechamento"),
            pytest.param('<tool name="x"/>', 1, id="tag-auto-fechada"),
            pytest.param("hello world", 0, id="texto-puro"),
            pytest.param("a <tool>b</tool> c", 2, id="conteudo-misto"),
            pytest.param("", 0, id="string-vazia"),
            pytest.param("[bold]text[/bold]", 0, id="rich-markup"),
            pytest.param("a < b > c", 0, id="angle-bracket-solto"),
        ],
    )
    def test_highlight(self, source, expected_spans):
        result = _highlight_tags(source)
        assert isinstance(result, Text)
        assert result.plain == source
        assert len(result.spans) == expected_spans

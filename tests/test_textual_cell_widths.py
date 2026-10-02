"""Casamento da tabela de larguras do Rich com a do terminal (``cell_widths``)."""
from __future__ import annotations

import os

import pytest
from rich._unicode_data import VERSIONS as RICH_VERSIONS
from rich.cells import cell_len, get_character_cell_size
from textual._cells import cell_len as textual_cell_len

from quimera.ui.textual import cell_widths
from quimera.ui.textual.cell_widths import (
    EMOJI_BY_VERSION,
    EMOJI_NARROW_VERSION,
    PROBES,
    VS16,
    TerminalWidths,
    calibrate,
    current_unicode_version,
    interpret,
    materialize_variation_selectors,
    use_unicode_version,
)

LATEST = RICH_VERSIONS[-1]
SGR_RESET = "\x1b[0m"


def xtermjs_unicode6_cells(text: str) -> int:
    """Células que o xterm.js sem addon (tabela Unicode 6) usa para ``text``.

    Só faixas CJK são largas; todo emoji vale 1; seletores e marcas valem 0.
    """
    total = 0
    for character in text:
        codepoint = ord(character)
        if codepoint in (0xFE0F, 0x200D) or 0x0300 <= codepoint <= 0x036F:
            continue
        if (
            0x1100 <= codepoint <= 0x115F
            or 0x2E80 <= codepoint <= 0xA4CF
            or 0xAC00 <= codepoint <= 0xD7A3
            or 0xF900 <= codepoint <= 0xFAFF
            or 0xFE30 <= codepoint <= 0xFE4F
            or 0xFF00 <= codepoint <= 0xFF60
            or 0xFFE0 <= codepoint <= 0xFFE6
            or 0x20000 <= codepoint <= 0x3FFFD
        ):
            total += 2
        else:
            total += 1
    return total


def table_cells(version: str, honors_vs16: bool):
    """Terminal que mede pela tabela Unicode ``version`` do Rich, caractere a caractere."""

    def cells(text: str) -> int:
        return sum(
            (1 if honors_vs16 else 0) if character == VS16 else get_character_cell_size(character, version)
            for character in text
        )

    return cells


def widths_of(cells) -> list[int]:
    return [cells(probe) for probe in PROBES]


@pytest.fixture(autouse=True)
def rich_table_guard():
    """Tabela do Rich isolada: ambiente limpo antes, estado original restaurado depois."""
    original = os.environ.pop("UNICODE_VERSION", None)
    cell_widths._reset_rich_width_caches()
    try:
        yield
    finally:
        os.environ.pop("UNICODE_VERSION", None)
        if original is not None:
            os.environ["UNICODE_VERSION"] = original
        cell_widths._reset_rich_width_caches()


# --- sondas -------------------------------------------------------------------


@pytest.mark.parametrize(("version", "emoji"), EMOJI_BY_VERSION, ids=[v for v, _ in EMOJI_BY_VERSION])
def test_each_probe_turns_wide_exactly_at_its_table(version, emoji):
    previous = RICH_VERSIONS[RICH_VERSIONS.index(version) - 1]

    assert get_character_cell_size(emoji, EMOJI_NARROW_VERSION) == 1
    assert get_character_cell_size(emoji, previous) == 1
    assert get_character_cell_size(emoji, version) == 2
    assert get_character_cell_size(emoji, LATEST) == 2


def test_sanity_samples_are_wide_in_every_rich_table():
    cjk, *_, vs16_sample = PROBES

    assert all(get_character_cell_size(cjk, version) == 2 for version in RICH_VERSIONS)
    assert all(cell_len(vs16_sample, version) == 2 for version in RICH_VERSIONS)


# --- interpretação das medidas ------------------------------------------------


def test_interpret_xtermjs_legacy_table():
    terminal = interpret(widths_of(xtermjs_unicode6_cells))

    assert terminal == TerminalWidths("8.0.0", honors_vs16=False)
    # Com essa tabela o Rich reproduz o terminal: ícone de agente e "🪐" em 1 célula.
    assert get_character_cell_size("🤖", terminal.unicode_version) == 1
    assert get_character_cell_size("🪐", terminal.unicode_version) == 1


def test_interpret_modern_terminal_that_honors_the_selector():
    assert interpret(widths_of(table_cells(LATEST, honors_vs16=True))) == TerminalWidths(
        LATEST, honors_vs16=True
    )


@pytest.mark.parametrize("version", RICH_VERSIONS)
def test_interpret_picks_a_table_that_reproduces_the_terminal(version):
    """Vale para qualquer tabela, inclusive a GLib 15.1 do VTE e as anteriores aos emoji."""
    cells = table_cells(version, honors_vs16=False)

    terminal = interpret(widths_of(cells))

    *width_probes, _ = PROBES
    assert [get_character_cell_size(p, terminal.unicode_version) for p in width_probes] == [
        cells(p) for p in width_probes
    ]
    assert terminal.honors_vs16 is False


@pytest.mark.parametrize(
    "widths",
    [
        [1] * len(PROBES),  # nem o CJK saiu em 2 células: resposta não confiável
        [2] + [0] * (len(PROBES) - 1),  # cursor não andou
        [2] + [3] * (len(PROBES) - 1),
        [2, 1, 1],  # incompleto
        [],
    ],
)
def test_interpret_rejects_unreliable_measurements(widths):
    assert interpret(widths) is None


# --- aplicação ao Rich --------------------------------------------------------


def test_use_unicode_version_switches_the_table_seen_by_textual():
    assert current_unicode_version() == LATEST
    assert textual_cell_len("🪐") == 2

    use_unicode_version("8.0.0")

    assert os.environ["UNICODE_VERSION"] == "8.0.0"
    assert current_unicode_version() == "8.0.0"
    assert textual_cell_len("🪐") == 1
    assert cell_len("🤖  claude-sonnet") == len("🤖  claude-sonnet")


def test_calibrate_applies_the_terminal_table_and_reports_the_selector():
    terminal = calibrate(widths_of(xtermjs_unicode6_cells))

    assert terminal == TerminalWidths("8.0.0", honors_vs16=False)
    assert current_unicode_version() == "8.0.0"


def test_calibrate_respects_a_preset_unicode_version():
    os.environ["UNICODE_VERSION"] = "11.0.0"
    cell_widths._reset_rich_width_caches()

    terminal = calibrate(widths_of(xtermjs_unicode6_cells))

    assert terminal.unicode_version == "8.0.0"
    assert os.environ["UNICODE_VERSION"] == "11.0.0"
    assert current_unicode_version() == "11.0.0"


def test_calibrate_rejects_an_unreliable_measurement_without_touching_rich():
    assert calibrate([2, 1, 1]) is None
    assert "UNICODE_VERSION" not in os.environ
    assert current_unicode_version() == LATEST


# --- materialização do U+FE0F -------------------------------------------------


def test_text_without_selector_is_returned_untouched():
    frame = "\x1b[1;1H🤖  claude-sonnet │ texto ░▒▓" + SGR_RESET
    assert materialize_variation_selectors(frame) is frame


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("☁" + VS16 + "  claudecloud-sonnet", "☁   claudecloud-sonnet"),
        ("⚙" + VS16 + "  opencode-big-pickle", "⚙   opencode-big-pickle"),
        ("⚠" + VS16 + "  REMOÇÃO", "⚠   REMOÇÃO"),
        ("✅" + VS16 + " ok", "✅ ok"),  # já largo: o Rich não contou o seletor
        ("a" + VS16 + "b", "ab"),
        ("☁" + VS16 + VS16 + "x", "☁ x"),  # o segundo seletor não soma nada
        (VS16 + "x", "x"),
        ("☁" + SGR_RESET + VS16 + "x", "☁" + SGR_RESET + "x"),  # separado do símbolo: não contado
    ],
)
def test_selector_becomes_a_space_only_where_rich_counted_a_cell(source, expected):
    result = materialize_variation_selectors(source)

    assert result == expected
    assert cell_len(result) == cell_len(source)


@pytest.mark.parametrize(
    "source",
    [
        "☁" + VS16 + "  claudecloud-fable · 15:48",
        "│ ⚙" + VS16 + " opencode ✓ ☁" + VS16 + " nuvem │",
        "⚠" + VS16 + "  REMOÇÃO ✅" + VS16 + " 🔍 dry-run",
        "mistura ☁" + VS16 + SGR_RESET + " ⚙" + VS16 + VS16 + " fim",
    ],
)
def test_result_fills_in_a_selector_blind_terminal_exactly_what_textual_reserved(source):
    result = materialize_variation_selectors(source)

    assert VS16 not in result
    assert cell_len(result) == cell_len(source)
    assert sum(get_character_cell_size(character) for character in result) == cell_len(source)

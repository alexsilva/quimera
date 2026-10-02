"""Casa a tabela de larguras de célula do Rich com a do terminal.

O Textual reserva no layout as células que o Rich mede e, numa repintura
completa, escreve cada linha de uma vez posicionando o cursor só no início
dela. Se o terminal usar outro número de células para algum caractere, tudo
que vem depois na mesma linha — texto, bordas de telas modais, scrollbar —
sai deslocado. Duas divergências ocorrem na prática:

* **A tabela Unicode.** O Rich mede com a mais recente que conhece, em que
  todo emoji vale 2 células. O xterm.js sem o addon ``unicode11`` (terminal da
  TinyIDE) usa a tabela Unicode 6 e dá 1 célula a "🤖", "🪐", "⚡"; o VTE segue
  a GLib e não conhece emoji mais novos que ela. O Rich aceita outra tabela
  pela variável ``UNICODE_VERSION`` (convenção *Terminal Unicode Core*), que
  quase nenhum terminal define.
* **O seletor de variação U+FE0F.** "☁️", "⚙️" e "⚠️" valem 2 células no Rich
  (apresentação emoji) e 1 em terminais que ignoram o seletor (VTE, xterm.js).

Este módulo é a parte sem I/O. O driver (``driver.py``) mede quantas células
o terminal usa para cada item de ``PROBES``; ``calibrate`` escolhe e ativa a
tabela do Rich que reproduz essas medidas e informa se o seletor precisa ser
materializado por ``materialize_variation_selectors``.

Fica de fora o recorte dos glifos: um terminal que reserva 1 célula para o
emoji desenha metade dele seja qual for a tabela do Rich — isso só se resolve
no terminal (addon ``unicode11``/``unicode-graphemes`` do xterm.js).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Sequence

from rich.cells import cached_cell_len, cell_len, get_character_cell_size, load_cell_table
from rich.segment import Segment

_logger = logging.getLogger(__name__)

VS16 = "\ufe0f"
"""Seletor de variação 16: pede a apresentação emoji do símbolo anterior."""

EMOJI_BY_VERSION: tuple[tuple[str, str], ...] = (
    ("9.0.0", "\U0001f916"),  # 🤖
    ("10.0.0", "\U0001f994"),  # 🦔
    ("11.0.0", "\U0001f970"),  # 🥰
    ("12.0.0", "\U0001fa90"),  # 🪐
    ("13.0.0", "\U0001fa84"),  # 🪄
    ("14.0.0", "\U0001fae0"),  # 🫠
    ("15.0.0", "\U0001fae8"),  # 🫨
    ("16.0.0", "\U0001fae9"),  # 🫩
    ("17.0.0", "\U0001faea"),
)
"""Um emoji por tabela do Rich em que ele passou a valer 2 células.

O terminal que desenha "🪐" em 2 células conhece ao menos a tabela 12.0.
"""

EMOJI_NARROW_VERSION = "8.0.0"
"""Última tabela em que nenhum emoji é largo — a de terminais como o xterm.js sem addon."""

_CJK_SAMPLE = "一"  # 2 células em toda tabela: valida a resposta do terminal
_VS16_SAMPLE = "☁" + VS16  # 2 células só onde o seletor é honrado

PROBES: tuple[str, ...] = (_CJK_SAMPLE, *(emoji for _, emoji in EMOJI_BY_VERSION), _VS16_SAMPLE)
"""Textos a medir no terminal, na ordem que ``calibrate`` espera."""


@dataclass(frozen=True)
class TerminalWidths:
    """Como o terminal mede células, em termos do modelo do Rich."""

    unicode_version: str
    """Tabela do Rich que reproduz as larguras do terminal."""
    honors_vs16: bool
    """O terminal dá a célula extra ao seletor de variação ("☁️" em 2 células)."""


def interpret(widths: Sequence[int]) -> TerminalWidths | None:
    """Interpreta as células que o terminal usou para cada item de ``PROBES``.

    Devolve ``None`` se a medição não é confiável: resposta incompleta, célula
    fora de {1, 2} ou CJK fora de 2 células.
    """
    if len(widths) != len(PROBES) or not set(widths) <= {1, 2}:
        return None
    cjk, *emoji, vs16 = widths
    if cjk != 2:
        return None
    wide = [version for (version, _), width in zip(EMOJI_BY_VERSION, emoji) if width == 2]
    return TerminalWidths(wide[-1] if wide else EMOJI_NARROW_VERSION, honors_vs16=vs16 == 2)


def current_unicode_version() -> str:
    """Tabela que o Rich está usando."""
    return load_cell_table().unicode_version


def use_unicode_version(version: str) -> None:
    """Faz o Rich — e o Textual, que mede por ele — usarem a tabela ``version``.

    O Rich lê a tabela de ``UNICODE_VERSION`` (que os processos filhos herdam,
    como manda a convenção) e guarda tabela e medidas em caches; como a troca
    acontece com o processo já rodando, os caches são descartados.
    """
    os.environ["UNICODE_VERSION"] = version
    _reset_rich_width_caches()


def _reset_rich_width_caches() -> None:
    load_cell_table.cache_clear()
    get_character_cell_size.cache_clear()
    cached_cell_len.cache_clear()
    Segment._split_cells.cache_clear()


def calibrate(widths: Sequence[int]) -> TerminalWidths | None:
    """Interpreta a medição do terminal e põe o Rich na tabela dele.

    Uma ``UNICODE_VERSION`` já definida no ambiente — pelo terminal ou pelo
    usuário — é respeitada. Devolve ``None``, sem mudar nada, se a medição não
    é confiável.
    """
    terminal = interpret(widths)
    if terminal is None:
        _logger.warning("Sondagem de larguras do terminal inconclusiva: medidas %s", tuple(widths))
        return None
    preset = os.environ.get("UNICODE_VERSION")
    if preset:
        _logger.info("UNICODE_VERSION=%s do ambiente mantida", preset)
    else:
        use_unicode_version(terminal.unicode_version)
    _logger.info(
        "Terminal: larguras Unicode %s, U+FE0F %s (medidas %s)",
        terminal.unicode_version,
        "honrado" if terminal.honors_vs16 else "ignorado, será materializado",
        tuple(widths),
    )
    return terminal


def materialize_variation_selectors(text: str) -> str:
    """Troca por espaço cada U+FE0F a que o Rich deu uma célula e descarta os demais.

    Para terminais que ignoram o seletor: o texto passa a ocupar neles as
    células que o Textual reservou — "☁️" vira "☁ " — e o layout continua
    válido. O Rich só conta o seletor quando ele vem logo após um símbolo
    estreito com apresentação emoji; a pergunta é feita ao próprio Rich.
    """
    if VS16 not in text:
        return text
    head, *tails = text.split(VS16)
    pieces = [head]
    before = head
    for tail in tails:
        if before and _rich_widens(before[-1]):
            pieces.append(" ")
        pieces.append(tail)
        before = tail
    return "".join(pieces)


def _rich_widens(character: str) -> bool:
    """O Rich dá uma célula extra a ``character`` seguido de U+FE0F?"""
    return cell_len(character + VS16) > cell_len(character)

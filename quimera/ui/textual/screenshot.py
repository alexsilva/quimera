"""Captura da tela da TUI Textual em texto e SVG, com a geometria do feed.

A paleta de comandos do Textual já salva um SVG da tela ("Screenshot"). Para
diagnóstico por agente isso não basta: um modelo lê texto, não SVG, e os bugs
de layout perseguidos aqui (cabeçalho deslocado, espaço em branco, scroll
errado) só se explicam com a geometria do feed. Este módulo renderiza o frame
atual uma única vez e devolve as três coisas juntas.
"""
from __future__ import annotations

import logging
import io
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TuiScreenshot:
    """Instantâneo da tela: linhas sem estilo, SVG e geometria do feed."""

    text: str
    svg: str
    width: int
    height: int
    feed: dict[str, Any] = field(default_factory=dict)

    @property
    def lines(self) -> list[str]:
        """Linhas da tela, da primeira à última, sem espaços à direita."""
        return self.text.split("\n") if self.text else []


def feed_geometry(app) -> dict[str, Any]:
    """Geometria do feed unificado (``#feed``), ou ``{}`` sem feed montado."""
    try:
        feed = app.query_one("#feed")
    except Exception as exc:
        _logger.debug("feed_geometry: feed não montado: %s", exc)
        return {}
    return feed_widget_geometry(feed)


def feed_widget_geometry(feed) -> dict[str, Any]:
    """Geometria de um feed já montado, ou ``{}`` se ainda sem layout.

    ``scroll_y``/``max_scroll_y`` denunciam o scroll negativo do compositor;
    ``first_slot_y`` comparado a ``content_y`` mostra se o cabeçalho está no
    topo; ``slots`` é o número de itens montados.
    """
    try:
        children = list(feed.children)
        geometry: dict[str, Any] = {
            "scroll_y": int(feed.scroll_y),
            "max_scroll_y": int(feed.max_scroll_y),
            "container_height": int(feed.container_size.height),
            "virtual_height": int(feed.virtual_size.height),
            "content_y": int(feed.content_region.y),
            "slots": len(children),
        }
        if children:
            geometry["first_slot_y"] = int(children[0].region.y)
            geometry["last_slot_bottom"] = int(children[-1].region.bottom)
    except Exception as exc:
        _logger.debug("feed_widget_geometry: geometria indisponível: %s", exc)
        return {}
    return geometry


def capture_textual_screenshot(app, *, title: str | None = None) -> TuiScreenshot:
    """Renderiza a tela atual uma vez e devolve texto, SVG e geometria.

    Reproduz ``App.export_screenshot`` do Textual 8.2.8 (console gravador
    sobre o ``render_update`` completo do compositor) para exportar texto e
    SVG do mesmo frame. Precisa rodar na thread do app: o compositor não é
    thread-safe. De outra thread, use ``TextualUiBridge.capture_screenshot``.
    """
    width, height = app.size
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        record=True,
        legacy_windows=False,
        safe_box=False,
    )
    screen_render = app.screen._compositor.render_update(
        full=True, screen_stack=app._background_screens
    )
    console.print(screen_render)
    text = console.export_text(clear=False)
    svg = console.export_svg(title=title or app.title, clear=True)
    lines = [line.rstrip() for line in text.split("\n")]
    # export_text termina com "\n": a última entrada vazia é só o terminador.
    if lines and lines[-1] == "":
        lines.pop()
    return TuiScreenshot(
        text="\n".join(lines),
        svg=svg,
        width=int(width),
        height=int(height),
        feed=feed_geometry(app),
    )

"""Eventos semânticos trocados entre o runtime e a UI Textual."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TextualUiEvent:
    """Evento thread-safe enviado do runtime para a UI Textual."""

    kind: str
    payload: Any = None
    agent: str | None = None
    compact: bool = False
    #: Pertence ao cabeçalho de boot (logo, projeto, bloco MCP, caminhos de
    #: log). O feed mantém esses itens quando o chat é limpo.
    boot: bool = False

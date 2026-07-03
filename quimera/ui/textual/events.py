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

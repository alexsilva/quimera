"""Abertura de editor externo para entrada multilinha."""
from __future__ import annotations

from ...editor import Editor
from ..interfaces import IRenderer


def read_from_editor(renderer: IRenderer, output_lock=None) -> str | None:
    """Abre editor com arquivo temporário; retorna conteúdo digitado."""
    return Editor(renderer, output_lock=output_lock).compose()

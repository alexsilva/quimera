"""Leitura de conteúdo a partir de arquivo fornecido pelo usuário."""
from __future__ import annotations

from pathlib import Path

from ..interfaces import IRenderer


def read_from_file(renderer: IRenderer, path_str):
    """Lê from file."""
    path = Path(path_str).expanduser()
    if not path.exists():
        renderer.show_error(f"\nArquivo não encontrado: {path}\n")
        return None
    content = path.read_text(encoding="utf-8")
    return _normalize_loaded_content(content)


def _normalize_loaded_content(content: str) -> str | None:
    """Normaliza conteúdo de /edit e /file sem perder linhas úteis."""
    normalized = (content or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if not normalized.strip():
        return None
    return normalized

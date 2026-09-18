"""Persistência por workspace do tema visual da TUI."""
from __future__ import annotations

from pathlib import Path

from quimera.config_store import read_json_object, update_json_object, write_json_object


class TuiThemeStore:
    """Guarda o tema do Textual escolhido pelo usuário no estado do workspace."""

    def __init__(self, path):
        """Inicializa uma instância de TuiThemeStore."""
        self._path = Path(path)

    @property
    def theme(self) -> str | None:
        """Retorna o nome do tema persistido, ou None quando nunca foi salvo."""
        value = read_json_object(self._path).get("theme")
        return value if isinstance(value, str) and value else None

    def set_theme(self, name: str) -> None:
        """Persiste o tema ativo; um arquivo ilegível é substituído por um novo."""

        def apply(data: dict) -> None:
            data["theme"] = name

        try:
            update_json_object(self._path, apply)
        except ValueError:
            write_json_object(self._path, {"theme": name})

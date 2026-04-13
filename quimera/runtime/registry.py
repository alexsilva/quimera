"""Componentes de `quimera.runtime.registry`."""
from __future__ import annotations

from collections.abc import Callable

from .models import ToolCall, ToolResult

ToolHandler = Callable[[ToolCall], ToolResult]


class ToolRegistry:
    """Implementa `ToolRegistry`."""
    def __init__(self) -> None:
        """Inicializa uma instância de ToolRegistry."""
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, name: str, handler: ToolHandler) -> None:
        """Executa register."""
        self._handlers[name] = handler

    def get(self, name: str) -> ToolHandler:
        """Retorna get."""
        try:
            return self._handlers[name]
        except KeyError as exc:
            raise KeyError(f"Ferramenta não registrada: {name}") from exc

    def names(self) -> list[str]:
        """Executa names."""
        return sorted(self._handlers.keys())

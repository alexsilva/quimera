from __future__ import annotations

from collections.abc import Callable

from .models import ToolCall, ToolResult

ToolHandler = Callable[[ToolCall], ToolResult]


class ToolRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, name: str, handler: ToolHandler) -> None:
        self._handlers[name] = handler

    def get(self, name: str) -> ToolHandler:
        try:
            return self._handlers[name]
        except KeyError as exc:
            raise KeyError(f"Ferramenta não registrada: {name}") from exc

    def names(self) -> list[str]:
        return sorted(self._handlers.keys())

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolCall:
    """Representa uma chamada de ferramenta emitida pelo modelo."""

    name: str
    arguments: dict[str, Any]
    call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResult:
    """Resultado padronizado de execução de ferramenta."""

    ok: bool
    tool_name: str
    content: str = ""
    error: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    truncated: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def to_model_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool_name": self.tool_name,
            "content": self.content,
            "error": self.error,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
            "data": self.data,
        }

"""Componentes de `quimera.runtime.models`. """
from __future__ import annotations

from quimera.runtime.errors import (
    ToolError,
    ToolValidationError,
    ToolEnvironmentError,
    ToolLogicError,
    ToolRateLimitError,
)
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
    error: str | ToolError | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    truncated: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def error_type(self) -> str:
        """Classificação do tipo de erro (validation, environment, logic, rate_limit, generic)."""
        if self.error is None:
            return "none"
        if isinstance(self.error, ToolError):
            if isinstance(self.error, ToolValidationError):
                return "validation"
            if isinstance(self.error, ToolEnvironmentError):
                return "environment"
            if isinstance(self.error, ToolLogicError):
                return "logic"
            if isinstance(self.error, ToolRateLimitError):
                return "rate_limit"
        return "generic"

    def to_model_payload(self) -> dict[str, Any]:
        """Executa to model payload."""
        error_metadata = self.error.metadata.copy() if isinstance(self.error, ToolError) else {}
        return {
            "ok": self.ok,
            "tool_name": self.tool_name,
            "content": self.content,
            "error": str(self.error) if isinstance(self.error, ToolError) else self.error,
            "error_type": self.error_type,
            "error_metadata": error_metadata,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
            "data": self.data,
        }

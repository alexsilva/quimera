"""Componentes de `quimera.runtime.models`. """
from __future__ import annotations

from quimera.runtime.errors import (
    ToolError,
    ToolValidationError,
    ToolEnvironmentError,
    ToolLogicError,
    ToolPolicyViolationError,
    ToolRateLimitError,
)
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class JobRecord:
    """Registro imutável de um job."""

    id: int
    description: str
    status: str
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class TaskRecord:
    """Registro imutável de uma task."""

    id: int
    job_id: int
    description: str
    status: str
    task_type: str = "general"
    origin: str = "legacy"
    body: str | None = None
    assigned_to: str | None = None
    result: str | None = None
    notes: str | None = None
    priority: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    created_by: str | None = None
    requested_by: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(slots=True)
class ToolCall:
    """Representa uma chamada de ferramenta emitida pelo modelo."""

    name: str
    arguments: dict[str, Any]
    call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ToolValidationError("ToolCall.name deve ser uma string não vazia", field="name")
        if not isinstance(self.arguments, dict):
            raise ToolValidationError("ToolCall.arguments deve ser um dict", field="arguments")


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

    @staticmethod
    def _truncate_text(value: str, max_chars: int) -> tuple[str, bool]:
        """Trunca texto longo preservando início e fim."""
        if len(value) <= max_chars:
            return value, False
        head = value[: max_chars // 2]
        tail = value[-(max_chars // 4):]
        truncated = f"{head}\n...[truncado, resultado com {len(value)} caracteres]...\n{tail}"
        return truncated, True

    @property
    def error_type(self) -> str:
        """Classificação do tipo de erro (validation, environment, logic, policy, rate_limit, generic)."""
        if self.error is None:
            return "none"
        if isinstance(self.error, ToolError):
            if isinstance(self.error, ToolValidationError):
                return "validation"
            if isinstance(self.error, ToolEnvironmentError):
                return "environment"
            if isinstance(self.error, ToolLogicError):
                return "logic"
            if isinstance(self.error, ToolPolicyViolationError):
                return "policy"
            if isinstance(self.error, ToolRateLimitError):
                return "rate_limit"
        lowered_error = str(self.error).lower()
        if any(
                marker in lowered_error
                for marker in (
                    "sem política para a ferramenta",
                    "bloqueada pelo modo de execução",
                    "comando bloqueado",
                    "comando inválido",
                    "comando fora da allowlist",
                    "path fora da workspace",
                )
        ):
            return "policy"
        return "generic"

    @property
    def error_hint(self) -> str | None:
        """Retorna hint de correção quando disponível."""
        if isinstance(self.error, ToolError):
            hint = self.error.metadata.get("hint")
            if isinstance(hint, str) and hint.strip():
                return hint.strip()
        if self.error_type == "policy":
            return (
                "Respeite a política da ferramenta: ajuste comando/argumentos e tente novamente sem repetir o mesmo bloqueio."
            )
        return None

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

    def to_prompt_payload(self, max_chars: int) -> dict[str, Any]:
        """Retorna payload mínimo e seguro para envio ao modelo."""
        content, content_truncated = self._truncate_text(self.content, max_chars)
        error_value = str(self.error) if isinstance(self.error, ToolError) else self.error
        error_text = error_value or ""
        error, error_truncated = self._truncate_text(error_text, max_chars)
        hint_value = self.error_hint or ""
        hint, hint_truncated = self._truncate_text(hint_value, max_chars)

        return {
            "ok": self.ok,
            "content": content,
            "error": error or None,
            "error_type": self.error_type,
            "hint": hint or None,
            "truncated": self.truncated or content_truncated or error_truncated or hint_truncated,
            "exit_code": self.exit_code,
        }

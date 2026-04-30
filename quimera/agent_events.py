"""Eventos estruturados emitidos pelo pipeline de visibilidade dos agentes."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SpyEvent:
    """Representa uma unidade estruturada de saída resumida do agente."""

    kind: str
    text: str
    transient: bool = False
    final: bool = False
    # Campo opcional para payload estruturado (telemetria/UI).
    # compare=False mantém retrocompatibilidade com testes que comparam SpyEvent por igualdade.
    data: dict[str, Any] | None = field(default=None, compare=False)


class _SyntheticToolResult:
    """Representa uma tool call executada internamente pelo agente CLI."""

    def __init__(self, ok: bool = True, error: str | None = None):
        self.ok = ok
        self.error = error

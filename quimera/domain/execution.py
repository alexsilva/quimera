"""Eventos estruturados de controle da execução.

Produtores descrevem o estado da execução sem incorporar texto ou estilo de UI.
Renderers são responsáveis por apresentar esses eventos.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ExecutionControlStatus(str, Enum):
    """Estados de controle emitidos pelo runtime."""

    CANCELLED = "cancelled"


class ExecutionControlSource(str, Enum):
    """Origem semântica da transição de controle."""

    USER = "user"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class ExecutionControlEvent:
    """Evento imutável de controle da execução."""

    status: ExecutionControlStatus
    source: ExecutionControlSource
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    agent: str | None = None

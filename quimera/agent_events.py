"""Eventos estruturados emitidos pelo pipeline de visibilidade dos agentes."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SpyEvent:
    """Representa uma unidade estruturada de saída resumida do agente."""

    kind: str
    text: str
    transient: bool = False
    final: bool = False

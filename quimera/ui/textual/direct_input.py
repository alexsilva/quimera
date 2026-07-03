"""Estado explícito de input direto da UI Textual.

Input direto é usado por aprovações, seleções e prompts modais. Ele deve
bloquear o roteamento normal para chat/stdin de agente enquanto estiver ativo.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DirectInputState:
    """Estado de sessão de input direto, inicialmente compatível com depth."""

    depth: int = 0
    owner: str | None = None
    kind: str | None = None

    @property
    def active(self) -> bool:
        """Retorna True quando há input direto armado."""
        return self.depth > 0

    def begin(self, *, owner: str | None = None, kind: str | None = None) -> None:
        """Arma input direto, preservando compatibilidade com chamadas aninhadas."""
        self.depth += 1
        if owner is not None:
            self.owner = owner
        if kind is not None:
            self.kind = kind

    def end(self) -> None:
        """Desarma uma camada de input direto."""
        self.depth = max(0, self.depth - 1)
        if self.depth == 0:
            self.owner = None
            self.kind = None

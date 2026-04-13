"""Componentes de `quimera.runtime.approval`."""
from __future__ import annotations

from abc import ABC, abstractmethod


class ApprovalHandler(ABC):
    """Define o contrato de aprovação usado pelo runtime de ferramentas."""

    @abstractmethod
    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Decide se uma ferramenta pode ser executada."""
        raise NotImplementedError


class ConsoleApprovalHandler(ApprovalHandler):
    """Confirmação simples no terminal."""

    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Solicita aprovação interativa ao usuário no terminal."""
        print(f"[aprovação] ferramenta={tool_name} :: {summary}")
        try:
            answer = input("Executar? [y/N]: ").strip().lower()
        except EOFError:
            print("[aprovação] stdin não disponível — negando automaticamente")
            return False
        return answer in {"y", "yes", "s", "sim"}


class AutoApprovalHandler(ApprovalHandler):
    """Aprovação automática sem interação — usar apenas em contextos controlados (REPL/testes)."""

    def __init__(self, approve_all: bool = True) -> None:
        """Inicializa uma instância de AutoApprovalHandler."""
        self._approve_all = approve_all

    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Retorna a política de aprovação automática configurada."""
        status = "aprovado" if self._approve_all else "negado"
        print(f"  [auto-{status}] {tool_name}")
        return self._approve_all

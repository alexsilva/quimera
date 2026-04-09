from __future__ import annotations

from abc import ABC, abstractmethod


class ApprovalHandler(ABC):
    @abstractmethod
    def approve(self, *, tool_name: str, summary: str) -> bool:
        raise NotImplementedError


class ConsoleApprovalHandler(ApprovalHandler):
    """Confirmação simples no terminal."""

    def approve(self, *, tool_name: str, summary: str) -> bool:
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
        self._approve_all = approve_all

    def approve(self, *, tool_name: str, summary: str) -> bool:
        status = "aprovado" if self._approve_all else "negado"
        print(f"  [auto-{status}] {tool_name}")
        return self._approve_all

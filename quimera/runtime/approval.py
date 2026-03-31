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
        answer = input("Executar? [y/N]: ").strip().lower()
        return answer in {"y", "yes", "s", "sim"}

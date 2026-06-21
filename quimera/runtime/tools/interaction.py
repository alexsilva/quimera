"""Ferramentas de interação com o usuário humano via terminal."""
from __future__ import annotations

import sys
from typing import Callable

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from .base import ToolBase


class InteractionTools(ToolBase):
    """Ferramentas que requerem resposta interativa do usuário."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de InteractionTools."""
        super().__init__(config)
        self._ask_user_fn: Callable[[str, list[str]], tuple[int, str]] | None = None

    def set_ask_user_fn(self, fn: Callable[[str, list[str]], tuple[int, str]]) -> None:
        """Injeta callable que exibe a pergunta e lê a resposta do terminal.

        Assinatura esperada: fn(question: str, options: list[str]) -> (index, value)
        Deve bloquear até o usuário responder.
        """
        self._ask_user_fn = fn

    def is_ask_user_available(self) -> bool:
        """Indica se ask_user está operável no contexto atual."""
        return self._ask_user_fn is not None

    def ask_user(self, call: ToolCall) -> ToolResult:
        """Apresenta uma pergunta com opções numeradas e aguarda a seleção do usuário."""
        question = str(call.arguments.get("question") or "").strip()
        raw_options = call.arguments.get("options") or []

        if not question:
            return ToolResult(ok=False, tool_name=call.name, error="'question' é obrigatório")
        if not isinstance(raw_options, list) or len(raw_options) < 2:
            return ToolResult(ok=False, tool_name=call.name, error="'options' deve ter ao menos 2 itens")

        options = [str(o) for o in raw_options]

        if self._ask_user_fn is not None:
            try:
                index, value = self._ask_user_fn(question, options)
                return ToolResult(
                    ok=True,
                    tool_name=call.name,
                    content=value,
                    data={"index": index, "value": value},
                )
            except (EOFError, KeyboardInterrupt):
                return ToolResult(ok=False, tool_name=call.name, error="Interrompido pelo usuário")
            except ValueError as exc:
                return ToolResult(ok=False, tool_name=call.name, error=str(exc))
            except Exception as exc:
                return ToolResult(ok=False, tool_name=call.name, error=f"Erro inesperado: {exc}")

        # Fallback sem injeção: leitura numerada por linha (cooked mode, sem termios)
        error_msg: str | None = None
        while True:
            parts = []
            if error_msg:
                parts.append(f"  ! {error_msg}")
            parts.append(f"\n{question}")
            for i, opt in enumerate(options, 1):
                parts.append(f"  {i}. {opt}")
            parts.append(f"  (número 1-{len(options)} ou texto exato)")
            sys.stdout.write("\n".join(parts) + "\n> ")
            sys.stdout.flush()
            try:
                raw = sys.stdin.readline().rstrip("\n\r").strip()
            except (EOFError, KeyboardInterrupt):
                return ToolResult(ok=False, tool_name=call.name, error="Interrompido pelo usuário")
            result = _parse_selection(call.name, raw, options)
            if result.ok:
                return result
            error_msg = f"'{raw}' não é uma opção válida."


def _parse_selection(tool_name: str, raw: str, options: list[str]) -> ToolResult:
    """Converte a resposta do usuário em índice+valor."""
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return ToolResult(
                ok=True, tool_name=tool_name,
                content=options[idx],
                data={"index": idx, "value": options[idx]},
            )
    except ValueError:
        pass
    raw_lower = raw.lower()
    for idx, opt in enumerate(options):
        if opt.lower() == raw_lower:
            return ToolResult(
                ok=True, tool_name=tool_name,
                content=opt,
                data={"index": idx, "value": opt},
            )
    return ToolResult(
        ok=False, tool_name=tool_name,
        error=f"Resposta inválida: '{raw}'. Use o número (1-{len(options)}) ou o texto exato da opção.",
    )


def register(registry, policy, config: ToolRuntimeConfig) -> InteractionTools:
    """Registra ask_user no registry e retorna o objeto para injeção posterior."""
    tools = InteractionTools(config)
    registry.register("ask_user", tools.ask_user)
    return tools

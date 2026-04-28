"""Componentes de `quimera.runtime.approval`."""
from __future__ import annotations

import select
import sys
import threading
import time
from abc import ABC, abstractmethod


class ApprovalHandler(ABC):
    """Define o contrato de aprovação usado pelo runtime de ferramentas."""

    @abstractmethod
    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Decide se uma ferramenta pode ser executada."""
        raise NotImplementedError


class ConsoleApprovalHandler(ApprovalHandler):
    """Confirmação simples no terminal.

    Se `renderer` for fornecido, usa-o para exibir o prompt (em vez de `print()`).
    Se `read_user_input_fn` for fornecida, usa-a para ler a resposta (em vez de `input_fn`/`input()`).
    Isso permite integração com o sistema de input do app.core, evitando
    conflitos com o leitor não-bloqueante.
    """

    def __init__(self, input_fn=None, renderer=None, read_user_input_fn=None):
        """Inicializa com dependências injetáveis do app.core.

        Args:
            input_fn: Função de input compatível com app.core (ex: input_resolver()).
            renderer: TerminalRenderer para exibir prompts (evita print cru).
            read_user_input_fn: Função (prompt, timeout) -> str | None do app.core.
                Quando fornecida, substitui input_fn para leitura.
        """
        self._input_fn = input_fn
        self._renderer = renderer
        self._read_user_input_fn = read_user_input_fn

    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Solicita aprovação interativa ao usuário no terminal."""
        self._show(f"\n[aprovação] {tool_name} :: {summary}")
        try:
            answer = self._read_line("  Executar? [y/N]: ").strip().lower()
        except EOFError:
            self._show("  [aprovação] stdin não disponível — negando automaticamente")
            return False
        return answer in {"y", "yes", "s", "sim"}

    def _show(self, message: str) -> None:
        """Exibe mensagem via renderer (se disponível) ou fallback print."""
        renderer = self._renderer
        if renderer is not None:
            renderer.show_system(message)
        else:
            print(message)

    def _read_line(self, prompt: str) -> str:
        """Lê uma linha, usando a input_fn injetada quando disponível."""
        read_user_input_fn = self._read_user_input_fn
        if read_user_input_fn is not None:
            result = read_user_input_fn(prompt, timeout=0)
            return result if result is not None else ""
        input_fn = self._input_fn
        if input_fn is None:
            return input(prompt)
        return input_fn(prompt)


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


class NonBlockingConsoleApprovalHandler(ApprovalHandler):
    """Aprovação não-bloqueante com timeout via select — ideal para uso em loop principal.

    Exibe a pergunta e aguarda até `timeout_seconds` por input no stdin.
    Se o usuário digitar 'y'/'yes'/'s'/'sim' a tempo, aprova.
    Qualquer outra entrada ou timeout resulta em negação automática.
    """

    def __init__(self, timeout_seconds: float = 5.0) -> None:
        """Inicializa com timeout configurável (padrão: 5 segundos)."""
        self._timeout = timeout_seconds

    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Exibe prompt e aguarda resposta não-bloqueante do usuário."""
        print(f"\n[aprovação] {tool_name}")
        print(f"  {summary}")
        print(f"  Digite 'y' em até {self._timeout:.0f}s para aprovar, ou qualquer tecla para negar...")

        answer = self._read_with_timeout(self._timeout)
        if answer is None:
            print("  [aprovação] timeout — negando automaticamente")
            return False
        approved = answer.strip().lower() in {"y", "yes", "s", "sim"}
        status = "aprovado" if approved else "negado"
        print(f"  [aprovação] {status}")
        return approved

    def _read_with_timeout(self, timeout: float) -> str | None:
        """Lê uma linha do stdin com timeout via select. Retorna None no timeout."""
        try:
            stdin = sys.stdin
            if stdin is None:
                return None
            # Drena qualquer input pendente antes de esperar
            self._drain_stdin()
            # Exibe o prompt
            sys.stdout.write("  > ")
            sys.stdout.flush()
            # Aguarda input com timeout
            ready, _, _ = select.select([stdin], [], [], timeout)
            if not ready:
                return None
            return stdin.readline()
        except Exception:
            return None

    @staticmethod
    def _drain_stdin() -> None:
        """Descarta dados pendentes no stdin para evitar leitura de lixo."""
        try:
            import termios
            import tty
            old_settings = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
            # Simplesmente tenta ler e descartar qualquer coisa pendente
            while True:
                ready, _, _ = select.select([sys.stdin], [], [], 0.0)
                if not ready:
                    break
                sys.stdin.read(1)
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        except Exception:
            pass


class PreApprovalHandler(ApprovalHandler):
    """Handler que permite pré-aprovar a próxima ferramenta antes dela ser chamada.

    Funciona como um semáforo: quando `pre_approve()` é chamado (ex: via comando /approve),
    a próxima chamada de `approve()` retorna True automaticamente. Após consumida,
    a pré-aprovação é resetada e chamadas subsequentes delegam ao handler base.
    """

    def __init__(self, base_handler: ApprovalHandler) -> None:
        """Inicializa com um handler base para fallback."""
        self._base = base_handler
        self._pre_approved = False
        self._lock = threading.Lock()

    def pre_approve(self) -> None:
        """Pré-aprova a próxima ferramenta (consumida uma única vez)."""
        with self._lock:
            self._pre_approved = True

    def reset(self) -> None:
        """Reseta a pré-aprovação sem consumir."""
        with self._lock:
            self._pre_approved = False

    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Aprova automaticamente se pré-aprovado, senão delega ao handler base."""
        with self._lock:
            if self._pre_approved:
                self._pre_approved = False
                print(f"  [pré-aprovado] {tool_name}")
                return True
        return self._base.approve(tool_name=tool_name, summary=summary)

"""Componentes de `quimera.runtime.approval`."""
from __future__ import annotations

import select
import threading
import sys
from abc import ABC, abstractmethod

class ApprovalHandler(ABC):
    """Define o contrato de aprovação usado pelo runtime de ferramentas."""

    @abstractmethod
    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Decide se uma ferramenta pode ser executada."""
        raise NotImplementedError


class ConsoleApprovalHandler(ApprovalHandler):
    """Confirmação simples no terminal via input() bloqueante."""

    def __init__(self, input_fn=None, renderer=None, suspend_fn=None, resume_fn=None):
        """Inicializa com dependências injetáveis.

        Args:
            input_fn: Função de input (fallback: builtins.input).
                      Se None, usa input() dinamicamente para
                      compatibilidade com @patch('builtins.input').
            renderer: TerminalRenderer opcional para exibir prompts.
            suspend_fn: Callback chamado antes de input() bloqueante
                        para suspender estado não-bloqueante do app.
            resume_fn: Callback chamado após input() bloqueante
                       para restaurar estado não-bloqueante do app.
        """
        self._input_fn = input_fn
        self._renderer = renderer
        self._suspend_fn = suspend_fn
        self._resume_fn = resume_fn
        self._suspend_spinner_fn = None
        self._resume_spinner_fn = None
        self._approve_all_callback = None

    def set_spinner_callbacks(self, suspend_spinner_fn, resume_spinner_fn):
        """Define callbacks para pausar/retomar o spinner do Rich.

        Esses callbacks são injetados por _call_api (client.py) para evitar
        que o refresh do Rich Console.status() compita com input() bloqueante
        durante a aprovação. Chamados em _approve_interactive.
        """
        self._suspend_spinner_fn = suspend_spinner_fn
        self._resume_spinner_fn = resume_spinner_fn

    def set_approve_all_callback(self, callback):
        """Define callback chamado quando o usuário digita 'a' (approve all).

        O callback recebe zero argumentos e deve ativar o modo
        'approve all' no handler de aprovação (ex: PreApprovalHandler).
        """
        self._approve_all_callback = callback

    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Tenta aprovação rápida; retorna False se precisar de interação.

        Sempre tenta aprovação interativa via input_fn.
        Se input_fn for o builtin input(), usa input() bloqueante padrão.
        Em contextos controlados (testes), input_fn injetada responde
        diretamente sem bloqueio.
        """
        return self._approve_interactive(tool_name, summary)

    def _approve_interactive(self, tool_name: str, summary: str) -> bool:
        """Aprovação interativa via input_fn (usado em testes/REPL).

        Suspende o estado não-bloqueante do app (via suspend_fn) antes de
        chamar input() bloqueante para evitar conflito com a thread de
        input não-bloqueante competindo pelo mesmo stdin.
        Também suspende o spinner do Rich (via suspend_spinner_fn) para
        evitar que o refresh periódico do Live compita com input().
        """
        input_fn = self._input_fn if self._input_fn is not None else input
        if self._suspend_fn:
            self._suspend_fn()
        if self._suspend_spinner_fn:
            self._suspend_spinner_fn()
        self._show(f"\n[aprovação] {tool_name} :: {summary}")
        try:
            answer = input_fn("  Executar? [y/N/a=todas]: ").strip().lower()
        except EOFError:
            self._show("  [aprovação] stdin não disponível — negando automaticamente")
            return False
        finally:
            if self._resume_spinner_fn:
                self._resume_spinner_fn()
            if self._resume_fn:
                self._resume_fn()
        if answer in {"a", "all", "todas"}:
            if self._approve_all_callback:
                self._approve_all_callback()
            return True
        return answer in {"y", "yes", "s", "sim"}

    def _show(self, message: str) -> None:
        renderer = self._renderer
        if renderer is not None:
            renderer.show_system(message)
        else:
            print(message)


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
        self._approve_all = False
        self._approve_all_permanent = False
        self._lock = threading.Lock()

    def pre_approve(self) -> None:
        """Pré-aprova a próxima ferramenta (consumida uma única vez)."""
        with self._lock:
            self._pre_approved = True

    def reset(self) -> None:
        """Reseta a pré-aprovação sem consumir."""
        with self._lock:
            self._pre_approved = False

    def set_approve_all(self, enabled: bool = True, permanent: bool = False) -> None:
        """Ativa/desativa modo 'approve all' — aprova todas as ferramentas sem perguntar.

        Args:
            enabled: True para ativar, False para desativar.
            permanent: Se True, o modo sobrevive ao fim do ciclo de tool hops.
                       Se False (padrão), é resetado automaticamente ao fim do ciclo.
        """
        with self._lock:
            self._approve_all = enabled
            self._approve_all_permanent = permanent if enabled else False
            if enabled:
                self._pre_approved = False

    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Aprova automaticamente se pré-aprovado ou em modo approve-all, senão delega ao handler base."""
        with self._lock:
            if self._approve_all:
                print(f"  [approve-all] {tool_name}")
                return True
            if self._pre_approved:
                self._pre_approved = False
                print(f"  [pré-aprovado] {tool_name}")
                return True
        return self._base.approve(tool_name=tool_name, summary=summary)
\n    def reset_approve_all_after_cycle(self) -> None:
        """Reseta approve-all ao final do ciclo de tool hops, a menos que seja permanente."""
        with self._lock:
            if self._approve_all and not self._approve_all_permanent:
                self._approve_all = False

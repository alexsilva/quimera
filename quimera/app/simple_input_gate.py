"""Input gate de stdin para o modo sem TUI (pipe, redirecionamento, REPL).

Mantém a interface pública do ``TextualInputGate`` para os consumidores
(``InputBroker``, ``ApprovalManager``, loop de chat), sem depender de uma UI
Textual. Dois regimes, decididos por ``stdin.isatty()`` na primeira leitura:

- **TTY** (``--driver-repl``): cada leitura é um ``input(prompt)`` comum, com
  edição de linha do terminal; o humano digita na ordem em que é perguntado.
- **Pipe** (``quimera`` com stdin redirecionado): o gate é o único leitor de
  stdin. Uma thread lê as linhas na ordem e cada linha vai para quem está
  esperando: uma leitura direta (``ask_user``, aprovação, seleção) tem
  prioridade sobre o chat, que por sua vez só pede linha quando não há prompt
  em execução (ver ``run_chat_loop``). Assim um script que escreve
  ``pergunta\\n2\\n`` tem o ``2`` consumido pelo ``ask_user`` do agente, não
  por uma segunda submissão em paralelo. Timeouts das leituras diretas são
  respeitados; no fim do stdin elas devolvem ``None`` (resposta padrão) e o
  chat recebe ``EOFError``.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import deque


class DirectReservation:
    """Reserva das próximas linhas do stdin para leituras diretas.

    ``release`` é idempotente e thread-safe (produtor e consumidor podem
    concluir o mesmo pedido ao mesmo tempo); ``wake`` acorda uma leitura
    direta em curso para ela reavaliar o ``should_abort`` do pedido.
    """

    def __init__(self, gate: "SimpleInputGate") -> None:
        self._gate = gate
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        with self._gate._cond:
            self._gate._direct_waiters -= 1
            self._gate._cond.notify_all()

    def wake(self) -> None:
        with self._gate._cond:
            self._gate._cond.notify_all()


class SimpleInputGate:
    """Input gate sobre stdin, sem UI Textual."""

    def __init__(
        self,
        renderer=None,
        toolbar_context_resolver=None,
        history_file=None,
        command_resolver=None,
        argument_resolver=None,
        stdin=None,
    ) -> None:
        self._renderer = renderer
        self._toolbar_context_resolver = toolbar_context_resolver
        self._command_resolver = command_resolver
        self._argument_resolver = argument_resolver
        self._theme_cycle_handler = None
        # ``stdin=None`` resolve ``sys.stdin`` na primeira leitura, para
        # redirecionamentos e patches feitos depois da construção valerem.
        self._stdin = stdin
        self._active_lock = threading.Lock()
        self._active = False
        self._owner_thread_id: int | None = None
        self._cond = threading.Condition()
        self._lines: deque[str] = deque()
        self._closed = False
        self._direct_waiters = 0
        self._reader: threading.Thread | None = None
        self._is_tty: bool | None = None

    # ------------------------------------------------------------------
    # Interface comum com TextualInputGate
    # ------------------------------------------------------------------

    def set_toolbar_context_resolver(self, resolver) -> None:
        self._toolbar_context_resolver = resolver

    def set_command_resolver(self, resolver) -> None:
        self._command_resolver = resolver

    def set_argument_resolver(self, resolver) -> None:
        self._argument_resolver = resolver

    def set_theme_cycle_handler(self, handler) -> None:
        self._theme_cycle_handler = handler

    def is_active(self) -> bool:
        with self._active_lock:
            return self._active

    def get_owner_thread_id(self) -> int | None:
        with self._active_lock:
            return self._owner_thread_id

    def redisplay(self) -> None:
        pass

    def get_line_buffer(self) -> str:
        return ""

    def _set_active_state(self, active: bool) -> None:
        with self._active_lock:
            self._active = active
            self._owner_thread_id = threading.get_ident() if active else None

    # ------------------------------------------------------------------
    # Regime e saída
    # ------------------------------------------------------------------

    def _stream(self):
        return self._stdin if self._stdin is not None else sys.stdin

    def _stdin_is_tty(self) -> bool:
        if self._is_tty is None:
            stream = self._stream()
            isatty = getattr(stream, "isatty", None)
            try:
                self._is_tty = bool(callable(isatty) and isatty())
            except (OSError, ValueError):
                self._is_tty = False
        return self._is_tty

    @property
    def sequential(self) -> bool:
        """True em pipe: as linhas são consumidas em ordem por quem pergunta."""
        return not self._stdin_is_tty()

    def _write(self, text: str) -> None:
        renderer = self._renderer
        print_direct = getattr(renderer, "print_direct", None)
        if callable(print_direct):
            print_direct(text)
            return
        print(text, flush=True)

    # ------------------------------------------------------------------
    # Leitura em pipe: thread leitora + roteamento por prioridade
    # ------------------------------------------------------------------

    def _ensure_reader(self) -> None:
        with self._cond:
            if self._reader is not None or self._closed:
                return
            self._reader = threading.Thread(
                target=self._read_loop, daemon=True, name="quimera-stdin-reader"
            )
            self._reader.start()

    def _read_loop(self) -> None:
        stream = self._stream()
        try:
            while True:
                try:
                    line = stream.readline()
                except (OSError, ValueError):
                    line = ""
                if line == "":
                    break
                with self._cond:
                    self._lines.append(line.rstrip("\r\n"))
                    self._cond.notify_all()
        finally:
            with self._cond:
                self._closed = True
                self._cond.notify_all()

    def _take(self, *, direct: bool, timeout: float | None, ticket=None) -> str | None:
        """Próxima linha, ou None no timeout, no fim do stdin ou por ``ticket``.

        Leituras diretas têm prioridade: enquanto uma espera, o chat não
        consome linha nenhuma. ``ticket`` (o pedido do broker) é consultado
        sob o lock: ``should_abort()`` a cada despertar, para um pedido
        expirado ou cancelado sair sem consumir linha, e ``claim()`` logo
        antes de tomar uma linha, para a linha ficar reclamada pelo pedido
        antes de sair do deque; a partir daí o pedido não expira mais e a
        resposta lida é a que vale, por mais que o eco demore.
        """
        self._ensure_reader()
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            if direct:
                self._direct_waiters += 1
            try:
                while True:
                    if ticket is not None and ticket.should_abort():
                        return None
                    if self._lines and (direct or self._direct_waiters == 0):
                        if ticket is not None and not ticket.claim():
                            return None
                        return self._lines.popleft()
                    if self._closed and not self._lines:
                        return None
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return None
                    self._cond.wait(remaining)
            finally:
                if direct:
                    self._direct_waiters -= 1
                    self._cond.notify_all()

    def is_closed(self) -> bool:
        """True quando o stdin acabou e nenhuma linha lida ficou por consumir."""
        with self._cond:
            return self._closed and not self._lines

    def reserve_direct_input(self) -> DirectReservation:
        """Reserva as próximas linhas para leituras diretas.

        O ``InputBroker`` chama ao enfileirar um pedido (ask_user, aprovação),
        antes de a pergunta ser renderizada: a partir daí o chat não consome
        linha nenhuma até o pedido ser respondido, cancelado ou expirar, mesmo
        que o pedido venha de um produtor em background com o chat ocioso.
        Em TTY a reserva é inócua.
        """
        with self._cond:
            self._direct_waiters += 1
        return DirectReservation(self)

    def read_chat_line(self, timeout: float | None = None, prompt: str = "") -> str | None:
        """Próxima linha do chat; None no timeout; ``EOFError`` no fim do stdin.

        Só existe no regime pipe (``sequential``); em TTY o chat usa
        ``__call__``. A linha consumida é ecoada com o prompt, como o feed da
        TUI ecoa a mensagem humana.
        """
        line = self._take(direct=False, timeout=timeout)
        if line is None:
            if self.is_closed():
                raise EOFError
            return None
        if prompt:
            self._write(f"{prompt}{line}")
        return line

    def _direct_read(self, prompt: str, timeout: float | None, ticket=None) -> str | None:
        if self._stdin_is_tty():
            try:
                return input(prompt)
            except EOFError:
                return None
        line = self._take(direct=True, timeout=timeout, ticket=ticket)
        if line is None:
            return None
        self._write(f"{prompt}{line}")
        return line

    # ------------------------------------------------------------------
    # Leituras
    # ------------------------------------------------------------------

    def __call__(self, prompt: str) -> str:
        self._set_active_state(True)
        try:
            if self._stdin_is_tty():
                return input(prompt)
            line = self.read_chat_line(timeout=None, prompt=prompt)
            if line is None:
                raise EOFError
            return line
        finally:
            self._set_active_state(False)

    def read_plain_input(self, prompt: str) -> str:
        return self(prompt)

    def read_input_in_terminal(
        self,
        prompt: str,
        timeout: float = 300.0,
        render_card_fn=None,
        owner: str | None = None,
        ticket=None,
    ) -> str | None:
        return self._direct_read(prompt, timeout, ticket)

    def read_selection_in_terminal(
        self,
        question: str,
        options: list[str],
        timeout: float = 300.0,
        owner: str | None = None,
        ticket=None,
    ) -> tuple[int, str] | None:
        lines = [str(question)]
        lines.extend(f"  {index}. {option}" for index, option in enumerate(options, 1))
        self._write("\n".join(lines))
        raw = self._direct_read(f"Selecione (1-{len(options)}): ", timeout, ticket)
        if raw is None:
            return None
        raw = raw.strip()
        try:
            index = int(raw) - 1
            if 0 <= index < len(options):
                return index, options[index]
        except ValueError:
            pass
        for index, option in enumerate(options):
            if option.lower() == raw.lower():
                return index, option
        return None

    def read_approval_in_terminal(
        self,
        question: str,
        prompt: str,
        timeout: float = 300.0,
        render_card_fn=None,
        owner: str | None = None,
        ticket=None,
    ) -> str | None:
        self._write(str(question))
        raw = self._direct_read(prompt, timeout, ticket)
        if raw is None:
            return None
        return raw.strip().lower() or None

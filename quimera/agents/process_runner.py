"""Watchdog de subprocess unificado para modos silencioso e ao vivo."""
import logging
import queue
import time
import threading

from quimera.agents.signal_guard import terminate_process_group
from quimera.agents.text_filters import _is_rate_limit_signal, _RATE_LIMIT_YIELD_SECONDS

_logger = logging.getLogger(__name__)


class ProcessRunner:
    """Gerencia o loop de watchdog de um subprocess já iniciado.

    Suporta dois modos via parâmetro ``log_queue``:
    - **Silencioso** (``log_queue=None``): detecta rate limit acumulando stderr;
      não renderiza output ao vivo.
    - **Ao vivo** (``log_queue`` fornecido): drena a fila em cada iteração,
      chamando ``on_item(stream_type, line)`` para cada entrada.
    """

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"

    def __init__(
        self,
        proc,
        stdout_thread: threading.Thread,
        stderr_thread: threading.Thread,
        result_holder: dict,
        cancel_event: threading.Event,
        timeout,
    ):
        self.proc = proc
        self.stdout_thread = stdout_thread
        self.stderr_thread = stderr_thread
        self.result_holder = result_holder
        self._cancel_event = cancel_event
        self._timeout = timeout
        self.rate_limit_detected = False
        self.rate_limit_detected_at: float | None = None
        self._rate_checked = 0

    def notify_rate_limit(self) -> None:
        """Registra detecção de rate limit (pode ser chamado externamente em modo ao vivo)."""
        self.rate_limit_detected = True
        if self.rate_limit_detected_at is None:
            self.rate_limit_detected_at = time.time()

    def _check_rate_limit_silent(self) -> None:
        """Verifica novos itens de stderr por sinais de rate limit (modo silencioso)."""
        current_stderr = self.result_holder["stderr"]
        for line in current_stderr[self._rate_checked:]:
            if _is_rate_limit_signal(line):
                self.notify_rate_limit()
        self._rate_checked = len(current_stderr)

    def _check_timeout(self, elapsed: int, now: float):
        """Retorna a razão de término por timeout, ou None se ainda dentro do limite."""
        if self._timeout is None or self._timeout <= 0:
            return None
        if self.rate_limit_detected and self.rate_limit_detected_at is not None:
            if now - self.rate_limit_detected_at > _RATE_LIMIT_YIELD_SECONDS:
                return self.RATE_LIMIT
        else:
            if elapsed > self._timeout * 5:
                return self.TIMEOUT
        return None

    def watch(self, log_queue=None, on_item=None, on_tick=None) -> str:
        """Loop de watchdog unificado.

        Parâmetros
        ----------
        log_queue:
            Fila de saída ao vivo (``queue.Queue``). Se ``None``, modo silencioso.
        on_item:
            Callback ``(stream_type: str, line: str) -> None`` para cada item da fila
            (somente relevante quando ``log_queue`` é fornecido).
        on_tick:
            Callback ``(elapsed: int) -> None`` chamado a cada iteração do loop.

        Retorna
        -------
        Uma das constantes: ``COMPLETED``, ``CANCELLED``, ``TIMEOUT``, ``RATE_LIMIT``.
        """
        start_time = time.time()
        elapsed = 0
        self._rate_checked = 0

        def _threads_active() -> bool:
            active = self.stdout_thread.is_alive() or self.stderr_thread.is_alive()
            if log_queue is not None:
                active = active or not log_queue.empty()
            return active

        while _threads_active():
            # Drena fila em modo ao vivo
            if log_queue is not None and on_item is not None:
                while not log_queue.empty():
                    try:
                        stream_type, line = log_queue.get_nowait()
                        on_item(stream_type, line)
                    except queue.Empty:
                        break

            # Verifica cancelamento pelo usuário
            if self._cancel_event.is_set():
                terminate_process_group(self.proc)
                self.stdout_thread.join(2)
                self.stderr_thread.join(2)
                return self.CANCELLED

            # Em modo silencioso, detecta rate limit no stderr acumulado
            if log_queue is None:
                self._check_rate_limit_silent()

            time.sleep(0.2)
            elapsed = int(time.time() - start_time)

            if on_tick is not None:
                on_tick(elapsed)

            reason = self._check_timeout(elapsed, time.time())
            if reason is not None:
                terminate_process_group(self.proc)
                self.stdout_thread.join(2)
                self.stderr_thread.join(2)
                return reason

        self.stdout_thread.join()
        self.stderr_thread.join()

        # Drena itens restantes da fila após threads encerrarem
        if log_queue is not None and on_item is not None:
            while not log_queue.empty():
                try:
                    stream_type, line = log_queue.get_nowait()
                    on_item(stream_type, line)
                except queue.Empty:
                    break

        return self.COMPLETED

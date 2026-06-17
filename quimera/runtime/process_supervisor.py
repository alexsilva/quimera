"""Owner central de lifecycle de subprocessos.

Gerencia registo, monitoramento e shutdown coordenado de processos
lançados pelo Quimera, garantindo que nenhum processo-filho sobreviva
ao encerramento do app.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from subprocess import Popen

_logger = logging.getLogger(__name__)

_SIGTERM_WAIT_SECONDS = 3.0
_SIGKILL_WAIT_SECONDS = 1.0


@dataclass
class ManagedProcess:
    """Metadados de um processo registrado no supervisor."""

    proc: "Popen" = field(repr=False)
    pid: int
    pgid: int
    owner: str
    label: str | None = None
    run_id: str | None = None
    call_id: str | None = None
    created_at: float = field(default_factory=time.monotonic)


class ProcessSupervisor:
    """Proprietário central de lifecycle de subprocessos.

    Responsabilidades:
    - Registrar processos recém-criados com metadados (owner, pgid).
    - Cancelar/bloquear novos registros durante shutdown.
    - Encerrar coordenadamente todos os processos registrados com
      escalada SIGTERM → SIGKILL.
    - Ser idempotente e thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, ManagedProcess] = {}
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Registro / cancelamento
    # ------------------------------------------------------------------

    def register(
        self,
        proc: "Popen",
        owner: str,
        label: str | None = None,
        run_id: str | None = None,
        call_id: str | None = None,
    ) -> ManagedProcess | None:
        """Registra um subprocesso recém-criado.

        Se o supervisor já estiver em shutdown, tenta terminar o processo
        imediatamente antes de retornar.

        Retorna o ``ManagedProcess`` criado ou ``None`` se o processo já
        tiver terminado antes do registro.
        """
        pid = proc.pid
        if pid is None:
            return None

        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = pid

        managed = ManagedProcess(
            proc=proc,
            pid=pid,
            pgid=pgid,
            owner=owner,
            label=label,
            run_id=run_id,
            call_id=call_id,
        )

        with self._lock:
            if self._shutting_down:
                _logger.warning(
                    "process_supervisor: registrando processo %d durante "
                    "shutdown — terminando imediatamente",
                    pid,
                )
                self._kill_process_group(pgid, pid)
                return None
            self._processes[pid] = managed

        _logger.debug("process_supervisor: registrado pid=%d pgid=%d owner=%s", pid, pgid, owner)
        return managed

    def unregister(self, proc: "Popen") -> None:
        """Remove o registro de um processo previamente registrado.

        É seguro chamar múltiplas vezes ou para processos nunca registrados.
        """
        pid = proc.pid
        if pid is None:
            return
        with self._lock:
            removed = self._processes.pop(pid, None)
        if removed is not None:
            _logger.debug("process_supervisor: removido pid=%d", pid)

    def alive(self) -> list[ManagedProcess]:
        """Retorna snapshot dos processos ainda vivos (seguro para shutdown)."""
        with self._lock:
            return list(self._processes.values())

    # ------------------------------------------------------------------
    # Shutdown coordenado
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Encerra todos os processos registrados.

        Idempotente: uma segunda chamada é segura e não-operacional.
        Escalada: SIGTERM → espera → SIGKILL.
        """
        snapshot = self._enter_shutdown()
        if not snapshot:
            return

        self._terminate_snapshot(snapshot, clear_registry=True)
        _logger.info("process_supervisor: shutdown concluído")

    def terminate_all(self) -> None:
        """Encerra todos os processos registrados sem bloquear novos registros futuros."""
        with self._lock:
            snapshot = list(self._processes.values())
        if not snapshot:
            return
        _logger.info("process_supervisor: terminate_all em %d processo(s)", len(snapshot))
        self._terminate_snapshot(snapshot, clear_registry=True)

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _enter_shutdown(self) -> list[ManagedProcess]:
        """Marca shutting_down e retorna snapshot dos processos atuais."""
        with self._lock:
            if self._shutting_down:
                return []
            self._shutting_down = True
            snapshot = list(self._processes.values())
        return snapshot

    def _terminate_snapshot(self, snapshot: list[ManagedProcess], *, clear_registry: bool) -> None:
        """Termina um snapshot de processos com escalada SIGTERM → SIGKILL."""
        for mp in snapshot:
            if not self._is_process_alive(mp.pid):
                continue
            try:
                os.killpg(mp.pgid, signal.SIGTERM)
                _logger.info("process_supervisor: SIGTERM enviado para pgid=%d (pid=%d)", mp.pgid, mp.pid)
            except OSError as exc:
                _logger.debug("process_supervisor: SIGTERM falhou para pid=%d: %s", mp.pid, exc)

        deadline = time.monotonic() + _SIGTERM_WAIT_SECONDS
        for mp in snapshot:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                mp.proc.wait(timeout=remaining)
            except Exception:
                pass

        for mp in snapshot:
            if not self._is_process_alive(mp.pid):
                continue
            try:
                os.killpg(mp.pgid, signal.SIGKILL)
                _logger.warning("process_supervisor: SIGKILL enviado para pgid=%d (pid=%d)", mp.pgid, mp.pid)
            except OSError as exc:
                _logger.debug("process_supervisor: SIGKILL falhou para pid=%d: %s", mp.pid, exc)

        for mp in snapshot:
            try:
                mp.proc.wait(timeout=_SIGKILL_WAIT_SECONDS)
            except Exception:
                pass

        if clear_registry:
            with self._lock:
                for mp in snapshot:
                    self._processes.pop(mp.pid, None)

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    def _kill_process_group(pgid: int, pid: int) -> None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

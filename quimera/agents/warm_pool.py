"""Pool de processos CLI pré-aquecidos para eliminar cold-start em agentes stdin."""
import logging
import subprocess
import threading

_logger = logging.getLogger(__name__)


class _WarmSlot:
    """Um processo pré-iniciado aguardando prompt via stdin."""

    __slots__ = ("proc", "cmd_key")

    def __init__(self, proc: subprocess.Popen, cmd_key: tuple) -> None:
        self.proc = proc
        self.cmd_key = cmd_key

    def is_alive(self) -> bool:
        """Retorna True se o processo ainda está em execução."""
        return self.proc.poll() is None

    def discard(self) -> None:
        """Encerra o processo se ainda estiver vivo."""
        try:
            if self.is_alive():
                self.proc.kill()
                self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


class WarmPool:
    """Pool de processos CLI pré-aquecidos para agentes com prompt via stdin.

    Mantém no máximo um processo pronto por chave ``(cmd, cwd)``. O processo
    fica bloqueado aguardando o prompt no stdin. Quando ``take()`` é chamado,
    o processo é removido do pool e entregue ao chamador. Um novo processo
    pode ser agendado via ``schedule_warm()`` para a próxima chamada.

    Thread-safe: todas as operações no dicionário interno são protegidas por lock.
    """

    def __init__(self) -> None:
        self._slots: dict[tuple, _WarmSlot] = {}
        self._pending: set[tuple] = set()
        self._lock = threading.Lock()
        self._shutdown = False

    @staticmethod
    def _make_key(cmd: list, cwd: str | None, extra_env: dict | None) -> tuple:
        frozen_extra = tuple(sorted(extra_env.items())) if extra_env else ()
        return (tuple(cmd), cwd, frozen_extra)

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def take(self, cmd: list, cwd: str | None, extra_env: dict | None = None) -> _WarmSlot | None:
        """Remove e retorna um slot aquecido saudável, ou ``None``.

        Se o processo já expirou (morreu antes do uso), descarta silenciosamente
        e retorna ``None`` para que o chamador crie um novo processo normal.
        """
        key = self._make_key(cmd, cwd, extra_env)
        with self._lock:
            slot = self._slots.pop(key, None)
        if slot is None:
            return None
        if not slot.is_alive():
            _logger.debug("[warm-pool] processo expirou: %s", cmd[0] if cmd else "?")
            slot.discard()
            return None
        _logger.debug("[warm-pool] entregando processo pré-aquecido: %s", cmd[0] if cmd else "?")
        return slot

    def schedule_warm(self, cmd: list, env: dict, cwd: str | None, extra_env: dict | None = None) -> None:
        """Agenda aquecimento em background se não houver slot ativo ou pendente.

        A deduplicação garante que apenas uma thread de aquecimento esteja em
        andamento por chave a qualquer momento.
        """
        if self._shutdown:
            return
        key = self._make_key(cmd, cwd, extra_env)
        with self._lock:
            if key in self._slots or key in self._pending:
                return
            self._pending.add(key)
        t = threading.Thread(
            target=self._do_warm,
            args=(cmd, env, cwd, key),
            daemon=True,
            name=f"warm-pool-{cmd[0] if cmd else 'agent'}",
        )
        t.start()

    def shutdown(self) -> None:
        """Encerra todos os processos pré-aquecidos e impede novos aquecimentos."""
        with self._lock:
            self._shutdown = True
            slots = list(self._slots.values())
            self._slots.clear()
            self._pending.clear()
        for slot in slots:
            slot.discard()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _do_warm(self, cmd: list, env: dict, cwd: str | None, key: tuple) -> None:
        """Inicia um processo em background e armazena o slot (chamado em thread)."""
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        except OSError as exc:
            _logger.debug("[warm-pool] falha ao aquecer %s: %s", cmd[0] if cmd else "?", exc)
            with self._lock:
                self._pending.discard(key)
            return

        slot = _WarmSlot(proc=proc, cmd_key=key)
        with self._lock:
            if self._shutdown:
                self._pending.discard(key)
                slot.discard()
                return
            old = self._slots.get(key)
            if old is not None:
                old.discard()
            self._slots[key] = slot
            self._pending.discard(key)
        _logger.debug("[warm-pool] aquecido: %s (cwd=%s)", cmd[0] if cmd else "?", cwd)

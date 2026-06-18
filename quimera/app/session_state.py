"""Gerenciamento de shared state e histórico da sessão."""

import logging
import threading

from ..shared_state import expire_stale_keys

logger = logging.getLogger(__name__)


class SessionStateManager:
    """Gerencia shared state, turn_stamps e histórico da sessão.

    Centraliza operações atômicas de avanço de turno e reset,
    sem depender do ``QuimeraApp``.
    """

    def __init__(self, storage, shared_state: dict | None = None,
                 history: list | None = None):
        self._lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._turn_stamps: dict = {}
        self.shared_state: dict = shared_state if shared_state is not None else {}
        self.history: list = history if history is not None else []
        self._storage = storage

    def advance_turn(self) -> None:
        """Avança turno lógico de conversa e expira agent keys antigas."""
        if not isinstance(self.shared_state, dict):
            return
        with self._lock:
            turn = int(self.shared_state.get("_current_turn", 0) or 0) + 1
            self.shared_state["_current_turn"] = turn
            expired = expire_stale_keys(self.shared_state, self._turn_stamps, turn)
            if expired:
                logger.info("[shared_state] expired stale keys: %s", expired)

    def history_snapshot(self) -> list:
        """Retorna uma cópia rasa do histórico sob lock."""
        with self._history_lock:
            return list(self.history)

    def shared_state_snapshot(self) -> dict:
        """Retorna uma cópia rasa do shared_state sob lock."""
        with self._lock:
            return dict(self.shared_state)

    @property
    def turn_stamps(self) -> dict:
        """Expõe os turn stamps gerenciados pela sessão."""
        return self._turn_stamps

    @property
    def shared_state_lock(self) -> threading.Lock:
        """Expõe o lock do shared_state."""
        return self._lock

    @property
    def history_lock(self) -> threading.Lock:
        """Expõe o lock do histórico."""
        return self._history_lock

    def reset(self, target: str = "state") -> str:
        """Reseta o estado da sessão conforme o alvo especificado.

        Args:
            target: ``"state"`` (shared_state), ``"history"`` (conversa)
                    ou ``"all"`` (ambos).

        Returns:
            Descrição do que foi resetado.
        """
        valid = ("state", "history", "all")
        if target not in valid:
            return f"uso: /reset {{{','.join(valid)}}}"

        if target in ("state", "all"):
            with self._lock:
                self.shared_state.clear()
                self._turn_stamps.clear()
            self._storage.save_history(self.history, shared_state=self.shared_state)

        if target in ("history", "all"):
            with self._history_lock:
                self.history.clear()
            self._storage.save_history(self.history, shared_state=self.shared_state)

        return {"state": "shared_state limpo.",
                "history": "histórico limpo.",
                "all": "shared_state e histórico limpos."}[target]

"""Gerenciamento de shared state e histórico da sessão."""

import logging

from ..shared_state import expire_stale_keys
from .state.session_state import SessionRuntimeState

logger = logging.getLogger(__name__)


class SessionStateManager:
    """Gerencia shared state, turn_stamps e histórico da sessão.

    Centraliza operações atômicas de avanço de turno e reset,
    sem depender do ``QuimeraApp``.
    """

    def __init__(
        self,
        storage,
        shared_state: dict | None = None,
        history: list | None = None,
        runtime_state: SessionRuntimeState | None = None,
    ):
        self._runtime_state = runtime_state or SessionRuntimeState.from_legacy(
            history=history,
            shared_state=shared_state,
        )
        # Compatibilidade com testes e app-like legados: aliases, não locks novos.
        self._lock = self._runtime_state.shared_state_lock
        self._history_lock = self._runtime_state.history_lock
        self._turn_stamps = self._runtime_state.turn_stamps
        self._storage = storage

    def advance_turn(self) -> None:
        """Avança turno lógico de conversa e expira agent keys antigas."""
        if not isinstance(self.shared_state, dict):
            return
        with self.shared_state_lock:
            turn = int(self.shared_state.get("_current_turn", 0) or 0) + 1
            self.shared_state["_current_turn"] = turn
            expired = expire_stale_keys(self.shared_state, self.turn_stamps, turn)
            if expired:
                logger.info("[shared_state] expired stale keys: %s", expired)

    def history_snapshot(self) -> list:
        """Retorna uma cópia rasa do histórico sob lock."""
        return self._runtime_state.history_snapshot()

    def shared_state_snapshot(self) -> dict:
        """Retorna uma cópia rasa do shared_state sob lock."""
        return self._runtime_state.shared_state_snapshot()

    @property
    def history(self) -> list:
        """Expõe o histórico gerenciado pela sessão."""
        return self._runtime_state.history

    @property
    def shared_state(self) -> dict:
        """Expõe o shared_state gerenciado pela sessão."""
        return self._runtime_state.shared_state

    @property
    def turn_stamps(self) -> dict:
        """Expõe os turn stamps gerenciados pela sessão."""
        return self._runtime_state.turn_stamps

    @property
    def shared_state_lock(self):
        """Expõe o lock do shared_state."""
        return self._runtime_state.shared_state_lock

    @property
    def history_lock(self):
        """Expõe o lock do histórico."""
        return self._runtime_state.history_lock

    @property
    def runtime_state(self) -> SessionRuntimeState:
        """Expõe a fonte única de runtime state para adapters de compatibilidade."""
        return self._runtime_state

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
            with self.shared_state_lock:
                self.shared_state.clear()
                self.turn_stamps.clear()
            self._storage.save_history(self.history, shared_state=self.shared_state)

        if target in ("history", "all"):
            with self.history_lock:
                self.history.clear()
            self._storage.save_history(self.history, shared_state=self.shared_state)

        return {"state": "shared_state limpo.",
                "history": "histórico limpo.",
                "all": "shared_state e histórico limpos."}[target]

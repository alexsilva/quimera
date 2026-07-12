"""Contentor de estado mutável compartilhado entre main thread e workers."""
from __future__ import annotations
from typing import Any

from ..app.state.session_state import SessionRuntimeState


class SessionState:
    """Estado mutável thread-safe. Workers recebem esta instância, não app.

    Agrupa o estado que antes estava espalhado como atributos de QuimeraApp:
      - history           (lista de mensagens do histórico)
      - shared_state      (estado goal-driven: goal, step, criteria, …)
      - session_meta      (dict com session_id, summary_loaded, …)
      - round_index       (contador de rodadas)
      - call_index        (contador de chamadas a agentes na sessão)
      - summary_agent_preference
    """

    def __init__(
        self,
        history: list | None = None,
        shared_state: dict | None = None,
        session_meta: dict | None = None,
        shared_state_lock=None,
        runtime_state: SessionRuntimeState | None = None,
    ) -> None:
        self._runtime_state = runtime_state or SessionRuntimeState.from_legacy(
            history=history,
            shared_state=shared_state,
            session_meta=session_meta,
            shared_state_lock=shared_state_lock,
        )

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------

    @property
    def history(self) -> list:
        return self._runtime_state.history

    def history_snapshot(self) -> list:
        """Cópia rasa thread-safe do histórico."""
        return self._runtime_state.history_snapshot()

    def append_history(self, msg: dict) -> None:
        with self.history_lock:
            self.history.append(msg)

    def replace_history(self, messages: list) -> None:
        """Substitui o histórico mantendo a referência da lista original."""
        with self.history_lock:
            self.history[:] = list(messages)

    def trim_history(self, limit: int) -> tuple[int, list]:
        """Aplica limite ao histórico e retorna quantidade de itens removidos."""
        with self.history_lock:
            if not isinstance(limit, int) or limit <= 0 or len(self.history) <= limit:
                return 0, list(self.history)
            dropped = len(self.history) - limit
            self.history[:] = self.history[-limit:]
            return dropped, list(self.history)

    def append_history_trimmed_and_snapshot(self, msg: dict, limit: int) -> tuple[int, list]:
        """Adiciona mensagem, aplica limite e retorna snapshot numa única transação."""
        with self.history_lock:
            self.history.append(msg)
            if isinstance(limit, int) and limit > 0 and len(self.history) > limit:
                dropped = len(self.history) - limit
                self.history[:] = self.history[-limit:]
            else:
                dropped = 0
            return dropped, list(self.history)

    def replace_history_if_prefix_matches(
        self,
        expected_prefix: list,
        prefix_length: int,
        replacement_prefix: list,
    ) -> tuple[bool, list]:
        """Substitui o histórico apenas se o prefixo esperado ainda estiver intacto."""
        with self.history_lock:
            current_snapshot = list(self.history)
            if current_snapshot[:prefix_length] != expected_prefix:
                return False, current_snapshot
            appended = current_snapshot[prefix_length:]
            self.history[:] = list(replacement_prefix) + appended
            return True, list(self.history)

    @property
    def history_lock(self):
        """Lock reentrante que protege operações transacionais no histórico."""
        return self._runtime_state.history_lock

    # ------------------------------------------------------------------
    # shared_state  (lock separado — mais granular)
    # ------------------------------------------------------------------

    @property
    def shared_state(self) -> dict:
        return self._runtime_state.shared_state

    def shared_state_snapshot(self) -> dict:
        """Cópia rasa thread-safe do shared_state."""
        return self._runtime_state.shared_state_snapshot()

    @property
    def shared_state_lock(self):
        return self._runtime_state.shared_state_lock

    # ------------------------------------------------------------------
    # session_meta  (session_id, summary_loaded, …)
    # ------------------------------------------------------------------

    @property
    def session_meta(self) -> dict:
        return self._runtime_state.session_state

    def get_meta(self, key: str, default: Any = None) -> Any:
        return self.session_meta.get(key, default)

    # ------------------------------------------------------------------
    # Contadores
    # ------------------------------------------------------------------

    @property
    def round_index(self) -> int:
        with self.history_lock:
            return self._runtime_state.round_index

    @round_index.setter
    def round_index(self, value: int) -> None:
        with self.history_lock:
            self._runtime_state.round_index = value

    @property
    def call_index(self) -> int:
        with self.history_lock:
            return self._runtime_state.call_index

    def increment_call_index(self) -> int:
        """Incrementa o contador de chamadas e retorna o novo valor."""
        return self._runtime_state.increment_call_index()

    def record_delegation(self, ok: bool) -> None:
        """Registra uma delegação enviada e seu resultado."""
        self._runtime_state.record_delegation(ok)

    # ------------------------------------------------------------------
    # Preferências de rodada
    # ------------------------------------------------------------------

    @property
    def summary_agent_preference(self) -> str | None:
        with self.history_lock:
            return self._runtime_state.summary_agent_preference

    @summary_agent_preference.setter
    def summary_agent_preference(self, value: str | None) -> None:
        with self.history_lock:
            self._runtime_state.summary_agent_preference = value

    # ------------------------------------------------------------------
    # Compat genérico (usado por código legado que acessa session_state["key"])
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self.session_meta.get(key, default)

    def update(self, *args, **kwargs) -> None:
        self.session_meta.update(*args, **kwargs)

    def snapshot(self) -> dict:
        """Cópia thread-safe da session_meta."""
        with self.history_lock:
            return self.session_meta.copy()

    def __getitem__(self, key: str) -> Any:
        return self.session_meta[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.session_meta[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self.session_meta

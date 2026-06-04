"""Contentor de estado mutável compartilhado entre main thread e workers."""
from __future__ import annotations
import threading
from typing import Any


class SessionState:
    """Estado mutável thread-safe. Workers recebem esta instância, não app.

    Agrupa o estado que antes estava espalhado como atributos de QuimeraApp:
      - history           (lista de mensagens do histórico)
      - shared_state      (estado goal-driven: goal, step, criteria, …)
      - session_meta      (dict com session_id, summary_loaded, …)
      - round_index       (contador de rodadas)
      - call_index        (contador de chamadas a agentes na sessão)
      - summary_agent_preference
      - pending_input_for
    """

    def __init__(
        self,
        history: list | None = None,
        shared_state: dict | None = None,
        session_meta: dict | None = None,
        shared_state_lock: threading.Lock | None = None,
    ) -> None:
        self._lock = threading.RLock()
        # Referências mutáveis — mantidas por referência para compatibilidade
        self._history: list = history if history is not None else []
        self._shared_state: dict = shared_state if shared_state is not None else {}
        self._shared_state_lock: threading.Lock = shared_state_lock or threading.Lock()
        self._session_meta: dict = session_meta if session_meta is not None else {}
        # Contadores e preferências
        self._round_index: int = 0
        self._call_index: int = 0
        self._summary_agent_preference: str | None = None
        self._pending_input_for: str | None = None

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------

    @property
    def history(self) -> list:
        return self._history

    def history_snapshot(self) -> list:
        """Retorna uma cópia rasa thread-safe do histórico."""
        with self._lock:
            return list(self._history)

    def append_history(self, msg: dict) -> None:
        with self._lock:
            self._history.append(msg)

    def replace_history(self, messages: list) -> None:
        """Substitui o conteúdo do histórico mantendo a referência da lista."""
        with self._lock:
            self._history[:] = list(messages)

    def trim_history(self, limit: int) -> tuple[int, list]:
        """Aplica limite ao histórico mantendo a referência e retorna snapshot final."""
        with self._lock:
            if not isinstance(limit, int) or limit <= 0 or len(self._history) <= limit:
                return 0, list(self._history)
            dropped = len(self._history) - limit
            self._history[:] = self._history[-limit:]
            return dropped, list(self._history)

    def replace_history_if_prefix_matches(
        self,
        expected_prefix: list,
        prefix_length: int,
        replacement_prefix: list,
    ) -> tuple[bool, list]:
        """Substitui histórico se o prefixo esperado ainda estiver intacto.

        Mensagens adicionadas após ``prefix_length`` são preservadas e um snapshot
        do histórico atual/final é retornado junto com o resultado da comparação.
        """
        with self._lock:
            current_snapshot = list(self._history)
            if current_snapshot[:prefix_length] != expected_prefix:
                return False, current_snapshot
            appended = current_snapshot[prefix_length:]
            self._history[:] = list(replacement_prefix) + appended
            return True, list(self._history)

    @property
    def history_lock(self) -> threading.RLock:
        """Lock reentrante que protege operações transacionais no histórico."""
        return self._lock

    # ------------------------------------------------------------------
    # shared_state  (lock separado — mais granular)
    # ------------------------------------------------------------------

    @property
    def shared_state(self) -> dict:
        return self._shared_state

    def shared_state_snapshot(self) -> dict:
        """Retorna uma cópia rasa do shared_state sob o lock dedicado."""
        with self._shared_state_lock:
            return dict(self._shared_state)

    @property
    def shared_state_lock(self) -> threading.Lock:
        return self._shared_state_lock

    # ------------------------------------------------------------------
    # session_meta  (session_id, summary_loaded, …)
    # ------------------------------------------------------------------

    @property
    def session_meta(self) -> dict:
        return self._session_meta

    def get_meta(self, key: str, default: Any = None) -> Any:
        return self._session_meta.get(key, default)

    # ------------------------------------------------------------------
    # Contadores
    # ------------------------------------------------------------------

    @property
    def round_index(self) -> int:
        with self._lock:
            return self._round_index

    @round_index.setter
    def round_index(self, value: int) -> None:
        with self._lock:
            self._round_index = value

    @property
    def call_index(self) -> int:
        with self._lock:
            return self._call_index

    def increment_call_index(self) -> int:
        """Incrementa e retorna o novo valor atomicamente."""
        with self._lock:
            self._call_index += 1
            return self._call_index

    # ------------------------------------------------------------------
    # Preferências de rodada
    # ------------------------------------------------------------------

    @property
    def summary_agent_preference(self) -> str | None:
        with self._lock:
            return self._summary_agent_preference

    @summary_agent_preference.setter
    def summary_agent_preference(self, value: str | None) -> None:
        with self._lock:
            self._summary_agent_preference = value

    @property
    def pending_input_for(self) -> str | None:
        with self._lock:
            return self._pending_input_for

    @pending_input_for.setter
    def pending_input_for(self, value: str | None) -> None:
        with self._lock:
            self._pending_input_for = value

    # ------------------------------------------------------------------
    # Compat genérico (usado por código legado que acessa session_state["key"])
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self._session_meta.get(key, default)

    def update(self, **kwargs) -> None:
        with self._lock:
            self._session_meta.update(kwargs)

    def snapshot(self) -> dict:
        """Cópia superficial thread-safe da session_meta."""
        with self._lock:
            return dict(self._session_meta)

    def __getitem__(self, key: str) -> Any:
        return self._session_meta[key]

    def __setitem__(self, key: str, value: Any) -> None:
        with self._lock:
            self._session_meta[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._session_meta

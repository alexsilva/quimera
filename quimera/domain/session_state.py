"""Canonical session-scoped mutable state — domain owns the state.

This module is the single source of truth for ``SessionRuntimeState`` and
its collaborators.  The ``app`` layer re-exports these names via thin shims
so that existing ``from quimera.app.state.session_state import …`` statements
keep working during the transition.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields
from typing import Any


# ------------------------------------------------------------------
# Value objects
# ------------------------------------------------------------------

@dataclass
class SessionMeta:
    session_id: str = ""
    history_count: int = 0
    history_restored: bool = False
    summary_loaded: bool = False
    current_job_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionMetrics:
    delegations_sent: int = 0
    delegations_received: int = 0
    delegations_succeeded: int = 0
    delegations_failed: int = 0
    total_latency: float = 0.0
    agent_metrics: dict[str, Any] = field(default_factory=dict)
    rounds_without_progress: int = 0
    consecutive_redundant_responses: int = 0
    delegation_invalid_count: int = 0
    responses_with_clear_next_step: int = 0
    total_responses: int = 0


_META_FIELDS = {f.name for f in fields(SessionMeta)} - {"extra"}
_METRIC_FIELDS = {f.name for f in fields(SessionMetrics)}


# ------------------------------------------------------------------
# Runtime state — canonical implementation
# ------------------------------------------------------------------

_MISSING = object()


@dataclass
class SessionRuntimeState(dict):
    """Session-scoped mutable state with dict-compatible interface.

    Inherits from ``dict`` so that ``isinstance(obj, dict)`` checks in
    existing code continue to work.  The dict storage is kept in sync
    with ``meta`` and ``metrics`` fields — reads go through the dict
    for speed, writes sync back to the underlying dataclass fields.
    """

    history: list = field(default_factory=list)
    shared_state: dict = field(default_factory=dict)
    meta: SessionMeta = field(default_factory=SessionMeta)
    metrics: SessionMetrics = field(default_factory=SessionMetrics)
    turn_stamps: dict = field(default_factory=dict)
    round_index: int = 0
    call_index: int = 0
    summary_agent_preference: str | None = None
    _history_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _shared_state_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._sync_dict_from_fields()

    # ------------------------------------------------------------------
    # Dict ↔ dataclass synchronisation
    # ------------------------------------------------------------------

    def _sync_dict_from_fields(self) -> None:
        """Populate dict storage from meta/metrics (full rebuild)."""
        self.clear()
        for key in _META_FIELDS:
            value = getattr(self.meta, key)
            if value is not None:
                dict.__setitem__(self, key, value)
        for key in _METRIC_FIELDS:
            dict.__setitem__(self, key, getattr(self.metrics, key))
        dict.__setitem__(self, "extra", self.meta.extra)
        dict.update(self, self.meta.extra)

    def _sync_field_from_key(self, key: str, value: Any) -> None:
        """Write-through: set the underlying dataclass field for *key*."""
        if key in _META_FIELDS:
            setattr(self.meta, key, value)
        elif key in _METRIC_FIELDS:
            setattr(self.metrics, key, value)
        elif key == "extra":
            pass
        else:
            self.meta.extra[key] = value

    # ------------------------------------------------------------------
    # dict protocol — reads
    # ------------------------------------------------------------------

    def __getitem__(self, key: str) -> Any:
        return dict.__getitem__(self, key)

    def __contains__(self, key: object) -> bool:
        return dict.__contains__(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            return default

    def __iter__(self) -> Iterator[str]:
        return dict.__iter__(self)

    def __len__(self) -> int:
        return dict.__len__(self)

    def keys(self):
        return dict.keys(self)

    def values(self):
        return dict.values(self)

    def items(self):
        return dict.items(self)

    # ------------------------------------------------------------------
    # dict protocol — writes (sync back to dataclass fields)
    # ------------------------------------------------------------------

    def __setitem__(self, key: str, value: Any) -> None:
        self._sync_field_from_key(key, value)
        if value is not None:
            dict.__setitem__(self, key, value)
        elif dict.__contains__(self, key):
            dict.__delitem__(self, key)

    def __delitem__(self, key: str) -> None:
        if key in _META_FIELDS:
            setattr(self.meta, key, None)
        elif key in _METRIC_FIELDS:
            raise KeyError(f"cannot delete session metric {key!r}")
        elif key != "extra":
            self.meta.extra.pop(key, None)
        dict.__delitem__(self, key)

    def update(self, *args, **kwargs) -> None:
        if args:
            if len(args) > 1:
                raise TypeError(f"update expected at most 1 argument, got {len(args)}")
            source = args[0]
            if isinstance(source, Mapping):
                iterable = source.items()
            else:
                iterable = source
            for key, value in iterable:
                self[key] = value
        for key, value in kwargs.items():
            self[key] = value

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key: str, default: Any = _MISSING) -> Any:
        if key not in self:
            if default is _MISSING:
                raise KeyError(key)
            return default
        value = self[key]
        del self[key]
        return value

    def clear(self) -> None:
        for key in _META_FIELDS:
            setattr(self.meta, key, None)
        self.meta.extra.clear()
        self.metrics = SessionMetrics()
        dict.clear(self)

    def copy(self) -> dict[str, Any]:
        return dict.copy(self)

    # ------------------------------------------------------------------
    # SessionRuntimeState-specific methods
    # ------------------------------------------------------------------

    @classmethod
    def from_legacy(
        cls,
        *,
        history: list | None = None,
        shared_state: dict | None = None,
        session_meta: Mapping[str, Any] | None = None,
        turn_stamps: dict | None = None,
        history_lock: threading.RLock | None = None,
        shared_state_lock: threading.RLock | None = None,
    ) -> SessionRuntimeState:
        state = cls(
            history=history if history is not None else [],
            shared_state=shared_state if shared_state is not None else {},
            turn_stamps=turn_stamps if turn_stamps is not None else {},
        )
        if history_lock is not None:
            state._history_lock = history_lock
        if shared_state_lock is not None:
            state._shared_state_lock = shared_state_lock
        if session_meta:
            state.update(session_meta)
        return state

    @property
    def history_lock(self) -> threading.RLock:
        return self._history_lock

    @property
    def shared_state_lock(self) -> threading.RLock:
        return self._shared_state_lock

    @property
    def session_state(self) -> SessionRuntimeState:
        """Backward-compat alias — returns self (dict-compatible)."""
        return self

    @property
    def session_meta(self) -> SessionRuntimeState:
        """Backward-compat alias — returns self (dict-compatible)."""
        return self

    def history_snapshot(self) -> list:
        with self._history_lock:
            return list(self.history)

    def shared_state_snapshot(self) -> dict:
        with self._shared_state_lock:
            return dict(self.shared_state)

    def record_delegation(self, ok: bool) -> None:
        with self._shared_state_lock:
            self.metrics.delegations_sent += 1
            if ok:
                self.metrics.delegations_succeeded += 1
            else:
                self.metrics.delegations_failed += 1

    def increment_call_index(self) -> int:
        with self._history_lock:
            self.call_index += 1
            return self.call_index

    # ------------------------------------------------------------------
    # History manipulation (atomic, thread-safe)
    # ------------------------------------------------------------------

    def append_history(self, msg: dict) -> None:
        with self.history_lock:
            self.history.append(msg)

    def replace_history(self, messages: list) -> None:
        with self.history_lock:
            self.history[:] = list(messages)

    def trim_history(self, limit: int) -> tuple[int, list]:
        with self.history_lock:
            if not isinstance(limit, int) or limit <= 0 or len(self.history) <= limit:
                return 0, list(self.history)
            dropped = len(self.history) - limit
            self.history[:] = self.history[-limit:]
            return dropped, list(self.history)

    def append_history_trimmed_and_snapshot(self, msg: dict, limit: int) -> tuple[int, list]:
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
        with self.history_lock:
            current_snapshot = list(self.history)
            if current_snapshot[:prefix_length] != expected_prefix:
                return False, current_snapshot
            appended = current_snapshot[prefix_length:]
            self.history[:] = list(replacement_prefix) + appended
            return True, list(self.history)

    def snapshot(self) -> dict:
        with self.history_lock:
            return dict.copy(self)

    def set_summary_agent_preference(self, value: str | None) -> None:
        """Setter thread-safe para ``summary_agent_preference``."""
        with self._shared_state_lock:
            self.summary_agent_preference = value

    def record_session_response(self, *, has_clear_next_step: bool = False) -> None:
        """Registra uma resposta e, separadamente, a presença de próximo passo.

        Redundância é uma classificação semântica independente e deve ser
        atualizada exclusivamente por ``increment_redundant_responses`` ou
        ``reset_consecutive_redundant``.
        """
        with self._shared_state_lock:
            self.metrics.total_responses += 1
            if has_clear_next_step:
                self.metrics.responses_with_clear_next_step += 1

    def reset_consecutive_redundant(self) -> None:
        """Reseta o contador de respostas redundantes consecutivas."""
        with self._shared_state_lock:
            self.metrics.consecutive_redundant_responses = 0

    def increment_redundant_responses(self) -> None:
        """Incrementa o contador de respostas redundantes consecutivas."""
        with self._shared_state_lock:
            self.metrics.consecutive_redundant_responses += 1

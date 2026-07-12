"""Single runtime source of truth for session-scoped mutable state."""
from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field, fields
from typing import Any


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


class SessionStateDict(dict):
    """Dict-compatible view over ``SessionRuntimeState`` meta and metrics."""

    def __init__(self, runtime_state: "SessionRuntimeState") -> None:
        super().__init__()
        self._runtime_state = runtime_state

    def _combined(self) -> dict[str, Any]:
        runtime = self._runtime_state
        data: dict[str, Any] = {}
        for key in _META_FIELDS:
            value = getattr(runtime.meta, key)
            if value is not None:
                data[key] = value
        for key in _METRIC_FIELDS:
            data[key] = getattr(runtime.metrics, key)
        data.update(runtime.meta.extra)
        return data

    def __getitem__(self, key: str) -> Any:
        if key in _META_FIELDS:
            value = getattr(self._runtime_state.meta, key)
            if value is not None:
                return value
        if key in _METRIC_FIELDS:
            return getattr(self._runtime_state.metrics, key)
        return self._runtime_state.meta.extra[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if key in _META_FIELDS:
            setattr(self._runtime_state.meta, key, value)
            return
        if key in _METRIC_FIELDS:
            setattr(self._runtime_state.metrics, key, value)
            return
        self._runtime_state.meta.extra[key] = value

    def __delitem__(self, key: str) -> None:
        if key in _META_FIELDS:
            setattr(self._runtime_state.meta, key, None)
            return
        if key in _METRIC_FIELDS:
            raise KeyError(f"cannot delete session metric {key!r}")
        del self._runtime_state.meta.extra[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._combined())

    def __len__(self) -> int:
        return len(self._combined())

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        if key in _META_FIELDS:
            return getattr(self._runtime_state.meta, key) is not None
        return key in _METRIC_FIELDS or key in self._runtime_state.meta.extra

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return self._combined().keys()

    def values(self):
        return self._combined().values()

    def items(self):
        return self._combined().items()

    def copy(self) -> dict[str, Any]:
        return self._combined()

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

    _MISSING = object()

    def pop(self, key: str, default: Any = _MISSING) -> Any:
        if key not in self:
            if default is self._MISSING:
                raise KeyError(key)
            return default
        value = self[key]
        del self[key]
        return value

    def clear(self) -> None:
        meta = self._runtime_state.meta
        for key in _META_FIELDS:
            setattr(meta, key, None)
        meta.extra.clear()
        self._runtime_state.metrics = SessionMetrics()


@dataclass
class SessionRuntimeState:
    """Session-scoped state with one history lock and one shared-state lock."""

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
    _session_state_view: SessionStateDict = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._session_state_view = SessionStateDict(self)

    @classmethod
    def from_legacy(
        cls,
        *,
        history: list | None = None,
        shared_state: dict | None = None,
        session_meta: Mapping[str, Any] | MutableMapping[str, Any] | None = None,
        turn_stamps: dict | None = None,
        history_lock: threading.RLock | None = None,
        shared_state_lock: threading.RLock | None = None,
    ) -> "SessionRuntimeState":
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
            state.session_state.update(session_meta)
        return state

    @property
    def history_lock(self) -> threading.RLock:
        return self._history_lock

    @property
    def shared_state_lock(self) -> threading.RLock:
        return self._shared_state_lock

    @property
    def session_state(self) -> SessionStateDict:
        return self._session_state_view

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

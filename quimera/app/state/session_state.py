"""Shim temporário — Fase 1 da auditoria de arquitetura.

Canonical implementation moved to ``quimera.domain.session_state``.
Re-exports kept so that existing ``from quimera.app.state.session_state import …``
statements continue to work during the transition.
"""
from quimera.domain.session_state import (  # noqa: F401
    SessionMeta,
    SessionMetrics,
    SessionRuntimeState,
)

__all__ = [
    "SessionMeta",
    "SessionMetrics",
    "SessionRuntimeState",
]

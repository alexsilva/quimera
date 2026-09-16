"""Opções de construção de `QuimeraApp` (parâmetros do `__init__` atual)."""
from dataclasses import dataclass

from ...constants import Visibility
from ...profiles.base import ProfileRegistry
from ...session_paths import SessionPaths
from ...workspace import Workspace


@dataclass(frozen=True)
class AppOptions:
    """Parâmetros imutáveis recebidos por `QuimeraApp.__init__`."""

    workspace: Workspace
    session_paths: SessionPaths
    debug: bool = False
    history_window: int | None = None
    agents: list | None = None
    threads: int = 1
    idle_timeout_seconds: int | None = None
    visibility: Visibility = Visibility.SUMMARY
    theme: str | None = None
    auto_approve_mutations: bool = False
    profile_registry: ProfileRegistry | None = None
    renderer_override: object = None
    input_gate_factory: object = None

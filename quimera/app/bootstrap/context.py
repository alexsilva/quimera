"""Opções de construção de `QuimeraApp` (parâmetros do `__init__` atual)."""
from dataclasses import dataclass
from pathlib import Path

from ...constants import Visibility
from ...profiles.base import ProfileRegistry
from ...workspace import Workspace


@dataclass(frozen=True)
class AppOptions:
    """Parâmetros imutáveis recebidos por `QuimeraApp.__init__`."""

    cwd: Path
    debug: bool = False
    history_window: int | None = None
    agents: list | None = None
    threads: int = 1
    idle_timeout_seconds: int | None = None
    visibility: Visibility = Visibility.SUMMARY
    theme: str | None = None
    workspace: Workspace | None = None
    auto_approve_mutations: bool = False
    profile_registry: ProfileRegistry | None = None
    renderer_override: object = None
    input_gate_factory: object = None

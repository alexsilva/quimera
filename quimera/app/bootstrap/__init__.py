"""Composition root de `QuimeraApp`: opções, bundles e o assembler."""
from .context import AppOptions
from .bundles import (
    AppBundles,
    ChatBundle,
    PlatformBundle,
    RuntimeBundle,
    SessionBundle,
    TaskBundle,
    UiBundle,
)
from .wiring import AppAssembler

__all__ = [
    "AppOptions",
    "AppBundles",
    "ChatBundle",
    "PlatformBundle",
    "RuntimeBundle",
    "SessionBundle",
    "TaskBundle",
    "UiBundle",
    "AppAssembler",
]

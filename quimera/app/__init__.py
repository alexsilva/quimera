"""Superfície pública estável do pacote ``quimera.app``."""

from .config import logger
from .handlers import PromptAwareStderrHandler

# Keep package-level exports limited to the supported public API.
__all__ = ["QuimeraApp", "logger", "PromptAwareStderrHandler"]


def __getattr__(name: str):
    """Importa ``QuimeraApp`` sob demanda para evitar ciclos de import."""
    if name == "QuimeraApp":
        from .core import QuimeraApp

        return QuimeraApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

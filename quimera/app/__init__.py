"""Superfície pública estável do pacote ``quimera.app``."""

from .config import logger
from .core import QuimeraApp
from .handlers import PromptAwareStderrHandler

# Keep package-level exports limited to the supported public API.
__all__ = ["QuimeraApp", "logger", "PromptAwareStderrHandler"]

"""Componentes de `quimera.app.config`."""
import logging
import os
import sys

from .handlers import PromptAwareStderrHandler

logger = logging.getLogger("quimera.staging")
log_level = os.environ.get("QUIMERA_LOG_LEVEL", "INFO").upper()
numeric_level = getattr(logging, log_level, logging.INFO)

handler = PromptAwareStderrHandler(sys.stderr)
handler.setFormatter(logging.Formatter("%(asctime)s: %(message)s"))
logger.addHandler(handler)
logger.propagate = False

logger.setLevel(numeric_level)
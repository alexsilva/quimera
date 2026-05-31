"""Componentes de `quimera.app.config`."""
import logging
import os
import sys

from .handlers import PromptAwareStderrHandler

logger = logging.getLogger("quimera.staging")
mcp_server_logger = logging.getLogger("quimera.runtime.mcp.server")
log_level = os.environ.get("QUIMERA_LOG_LEVEL", "INFO").upper()
numeric_level = getattr(logging, log_level, logging.INFO)

handler = PromptAwareStderrHandler(sys.stderr)
handler.setFormatter(logging.Formatter("%(asctime)s: %(message)s"))
for configured_logger in (logger, mcp_server_logger):
    configured_logger.addHandler(handler)
    configured_logger.propagate = False
    configured_logger.setLevel(numeric_level)

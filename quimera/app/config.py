"""Componentes de `quimera.app.config`."""
import logging
import os
import sys
from pathlib import Path

from .handlers import PromptAwareStderrHandler

logger = logging.getLogger("quimera.staging")
mcp_server_logger = logging.getLogger("quimera.runtime.mcp.server")
log_level = os.environ.get("QUIMERA_LOG_LEVEL", "INFO").upper()
numeric_level = getattr(logging, log_level, logging.INFO)

# File handler — captura TODOS os logs; path atualizado via set_app_log_file()
_default_log = Path(os.environ.get("QUIMERA_LOG_FILE", "/tmp/quimera-app.log"))
_file_handler = logging.FileHandler(str(_default_log), mode="a", encoding="utf-8", delay=True)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(module)s: %(message)s"
))
_file_handler.setLevel(logging.DEBUG)

# Screen handler — só WARNING+ com formato amigável (via callbacks)
handler = PromptAwareStderrHandler(sys.stderr)
handler.setFormatter(logging.Formatter("%(asctime)s: %(message)s"))

for configured_logger in (logger, mcp_server_logger):
    configured_logger.addHandler(handler)
    configured_logger.addHandler(_file_handler)
    configured_logger.propagate = False
    configured_logger.setLevel(numeric_level)

# Captura todos os outros loggers quimera.* (agents, session, workspace, etc.)
# que antes propagavam para o root e caíam no lastResort (stderr flash).
_quimera_root = logging.getLogger("quimera")
_quimera_root.addHandler(handler)
_quimera_root.addHandler(_file_handler)
_quimera_root.propagate = False
_quimera_root.setLevel(numeric_level)


def set_app_log_file(path: "Path | str") -> None:
    """Redireciona o FileHandler para o path definitivo do workspace."""
    global _file_handler
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for lg in (logger, mcp_server_logger):
        lg.removeHandler(_file_handler)
    try:
        _file_handler.close()
    except Exception:
        pass
    _file_handler = logging.FileHandler(str(path), mode="a", encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(module)s: %(message)s"
    ))
    _file_handler.setLevel(logging.DEBUG)
    for lg in (logger, mcp_server_logger):
        lg.addHandler(_file_handler)

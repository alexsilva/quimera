"""Constantes de runtime sem módulo de domínio próprio."""
from __future__ import annotations

import os

MAX_STDERR_LINES = 5
_env_limit = os.getenv("QUIMERA_MAX_STDERR_LINES")
if _env_limit is not None:
    try:
        MAX_STDERR_LINES = int(_env_limit)
    except ValueError:
        pass  # valor não numérico na variável de ambiente: mantém o padrão

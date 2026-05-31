"""Helpers compartilhados entre os módulos de tools."""
from __future__ import annotations

import os


def resolve_current_job_id() -> int | None:
    """Resolve o job_id atual via variável de ambiente QUIMERA_CURRENT_JOB_ID."""
    env_val = os.environ.get("QUIMERA_CURRENT_JOB_ID")
    if env_val is not None:
        try:
            return int(env_val)
        except ValueError:
            return None
    return None

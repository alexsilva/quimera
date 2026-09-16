"""Helpers mínimos para paths já pertencentes a serviços da sessão."""

from pathlib import Path
from typing import Any


def resolve_session_log_path(storage: Any) -> str | Path:
    """Retorna o log persistente fornecido pelo próprio storage."""
    get_log_file = getattr(storage, "get_log_file", None)
    if not callable(get_log_file):
        return ""
    return get_log_file() or ""


def resolve_render_debug_log_path(
    storage: Any,
    session_paths: Any,
    debug_prompt_metrics: bool,
) -> str | Path:
    """Retorna o audit temporário de render somente quando debug está ativo."""
    if not debug_prompt_metrics:
        return ""
    session_id = getattr(storage, "session_id", "")
    getter = getattr(session_paths, "render_log_path_for", None)
    if not session_id or not callable(getter):
        return ""
    path = getter(session_id)
    if path is None:
        return ""
    normalized = str(path).strip()
    if not normalized or normalized == ".":
        return ""
    return Path(normalized)

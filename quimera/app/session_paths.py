"""Resolução de paths de sessão e auditoria de render."""

from pathlib import Path
from typing import Any


def _call_path_getter(source: Any, getter_name: str, session_id: str) -> Path | None:
    getter = getattr(source, getter_name, None)
    if not callable(getter) or not session_id:
        return None
    try:
        value = getter(session_id)
    except Exception:
        return None
    if not isinstance(value, (str, Path)):
        return None
    normalized = str(value).strip()
    if not normalized or normalized == ".":
        return None
    return Path(normalized)


def resolve_workspace_render_log_path(workspace: Any, session_id: str) -> Path | None:
    if workspace is None:
        return None
    workspace_tmp = getattr(workspace, "tmp", None)
    path = _call_path_getter(workspace_tmp, "render_log_path_for", session_id)
    if path:
        return path
    return _call_path_getter(workspace, "render_log_path_for", session_id)


def resolve_workspace_render_ansi_path(workspace: Any, session_id: str) -> Path | None:
    if workspace is None:
        return None
    workspace_tmp = getattr(workspace, "tmp", None)
    path = _call_path_getter(workspace_tmp, "render_ansi_path_for", session_id)
    if path:
        return path
    return _call_path_getter(workspace, "render_ansi_path_for", session_id)


def resolve_workspace_metrics_path(workspace: Any, session_id: str) -> Path | None:
    if workspace is None:
        return None
    workspace_tmp = getattr(workspace, "tmp", None)
    path = _call_path_getter(workspace_tmp, "metrics_path_for", session_id)
    if path:
        return path
    return _call_path_getter(workspace, "metrics_path_for", session_id)


def resolve_session_log_path(storage: Any, workspace: Any) -> str | Path:
    get_log_file = getattr(storage, "get_log_file", None)
    if callable(get_log_file):
        log_file = get_log_file()
        if log_file:
            return log_file

    logs_dir = getattr(workspace, "logs_dir", None)
    session_id = getattr(storage, "session_id", None)
    if logs_dir and session_id:
        return Path(logs_dir) / f"{session_id}.jsonl"
    return ""


def resolve_app_log_path(workspace: Any, session_id: str) -> Path | None:
    """Resolve o path do arquivo de log da aplicação para agents consultarem."""
    if workspace is None:
        return None
    workspace_tmp = getattr(workspace, "tmp", None)
    path = _call_path_getter(workspace_tmp, "app_log_path_for", session_id)
    if path:
        return path
    return _call_path_getter(workspace, "app_log_path_for", session_id)


def resolve_render_debug_log_path(storage: Any, workspace: Any, debug_prompt_metrics: bool) -> str | Path:
    if not debug_prompt_metrics:
        return ""
    session_id = getattr(storage, "session_id", None)
    if not session_id:
        return ""
    resolved = resolve_workspace_render_log_path(workspace, session_id)
    return resolved if resolved is not None else ""

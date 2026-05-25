"""Centralização de resolução de paths/sessão para startup e debug.

Módulo público de entrada para resolução de caminhos de sessão,
auditoria de render e métricas. Delega para implementação interna
em session_paths.
"""

from pathlib import Path
from typing import Any

from .session_paths import (
    resolve_session_log_path as _resolve_session_log_path,
    resolve_render_debug_log_path as _resolve_render_debug_log_path,
    resolve_workspace_render_log_path as _resolve_workspace_render_log_path,
    resolve_workspace_render_ansi_path as _resolve_workspace_render_ansi_path,
    resolve_workspace_metrics_path as _resolve_workspace_metrics_path,
)


def resolve_session_log_path(storage: Any, workspace: Any) -> str | Path:
    return _resolve_session_log_path(storage, workspace)


def resolve_render_debug_log_path(
    storage: Any, workspace: Any, debug_prompt_metrics: bool
) -> str | Path:
    return _resolve_render_debug_log_path(storage, workspace, debug_prompt_metrics)


def resolve_workspace_render_log_path(workspace: Any, session_id: str) -> Path | None:
    return _resolve_workspace_render_log_path(workspace, session_id)


def resolve_workspace_render_ansi_path(workspace: Any, session_id: str) -> Path | None:
    return _resolve_workspace_render_ansi_path(workspace, session_id)


def resolve_workspace_metrics_path(workspace: Any, session_id: str) -> Path | None:
    return _resolve_workspace_metrics_path(workspace, session_id)

"""Caminhos temporários da execução associados ao workspace ativo.

Esses caminhos não fazem parte do estado persistente do ``Workspace``. A
instância mantém apenas uma referência ao Workspace para acompanhar sua
identidade atual; toda a topologia sob ``/tmp`` pertence a esta classe.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .paths import TMP_BASE_DIR

if TYPE_CHECKING:
    from .workspace import Workspace

logger = logging.getLogger(__name__)


class SessionPaths:
    """Resolve e garante a subtree temporária da execução atual."""

    def __init__(self, workspace: Workspace) -> None:
        self._root = TMP_BASE_DIR / workspace.cwd_hash
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """Garante a infraestrutura temporária desta execução."""
        root = self._root
        paths = (
            (root / "data" / "logs", "logs dir"),
            (root / "data" / "logs" / "render", "render logs dir"),
            (root / "data" / "logs" / "metrics", "metrics dir"),
            (root / "clipboard", "clipboard dir"),
            (root / "data" / "artifacts", "artifacts dir"),
        )
        for path, label in paths:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("Failed to create %s %s: %s", label, path, exc)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def logs_dir(self) -> Path:
        return self.root / "data" / "logs"

    @property
    def render_logs_dir(self) -> Path:
        return self.logs_dir / "render"

    @property
    def metrics_dir(self) -> Path:
        return self.logs_dir / "metrics"

    @property
    def clipboard_dir(self) -> Path:
        return self.root / "clipboard"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "data" / "artifacts"

    def render_log_path_for(self, session_id: str) -> Path:
        return self.render_logs_dir / f"render-{session_id}.jsonl"

    def render_ansi_path_for(self, session_id: str) -> Path:
        return self.render_logs_dir / f"render-{session_id}.ansi"

    def metrics_path_for(self, session_id: str) -> Path:
        return self.metrics_dir / f"{session_id}.jsonl"

    def app_log_path_for(self, session_id: str) -> Path:
        return self.logs_dir / f"app-{session_id}.log"

    def mcp_socket_path(self, suffix: str) -> Path:
        return self.root / f"mcp-{suffix}.sock"

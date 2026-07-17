"""Composição fina de `QuimeraApp`."""

import sys  # noqa: F401
from pathlib import Path

from .bootstrap import AppAssembler, AppOptions
from .bootstrap.wiring import normalize_agent_name  # noqa: F401
from .core_facade import CoreFacadeMixin
from .runtime_state import AppRuntimeState
from .turn import TurnManager
from .worker import ChatWorker
from .chat_processor import run_chat_loop
from .. import profiles  # noqa: F401
from ..constants import Visibility
from ..profiles.base import ProfileRegistry
from ..workspace import Workspace


class QuimeraApp(CoreFacadeMixin):
    """Orquestra comandos locais, roteamento entre agentes e ciclo da sessão."""
    _SESSION_LOG_DISPLAY_MAX_CHARS = 96

    def __init__(self,
                 cwd: Path,
                 debug: bool = False,
                 history_window: int | None = None,
                 agents: list | None = None,
                 threads: int = 1,
                 idle_timeout_seconds: int | None = None,
                 visibility: Visibility = Visibility.SUMMARY,
                 theme: str | None = None,
                 workspace: Workspace | None = None,
                 auto_approve_mutations: bool = False,
                 profile_registry: ProfileRegistry | None = None,
                 renderer_override=None,
                 input_gate_factory=None,
                 ):
        """Inicializa uma instância de QuimeraApp montando os bundles via `AppAssembler`."""
        opts = AppOptions(
            cwd=cwd,
            debug=debug,
            history_window=history_window,
            agents=agents,
            threads=threads,
            idle_timeout_seconds=idle_timeout_seconds,
            visibility=visibility,
            theme=theme,
            workspace=workspace,
            auto_approve_mutations=auto_approve_mutations,
            profile_registry=profile_registry,
            renderer_override=renderer_override,
            input_gate_factory=input_gate_factory,
        )
        self._execution_mode_state = self._create_execution_mode_state()
        self.internal_mcp_server = None
        self.mcp_socket_path = None
        self.mcp_http_url = None
        # runtime_state precisa existir antes do assembler: builders de sessão
        # (AppInputServices) capturam seus setters durante a montagem.
        self.runtime_state = AppRuntimeState()
        AppAssembler().assemble(opts, self)

    def run(self):
        """Executa o loop interativo do chat multiagente."""
        run_chat_loop(
            self,
            chat_worker_cls=ChatWorker,
            turn_manager_cls=TurnManager,
        )

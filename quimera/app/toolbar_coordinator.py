"""Coordenador de toolbar: resolve labels, estado de paralelismo e ciclo de temas."""

from .toolbar import (
    ActiveModelRequest,
    ParallelToolbarSnapshotRequest,
    ToolbarContextRequest,
    ToolbarManager,
)
from .config import logger


class ToolbarCoordinator:
    """Coordena as operações de toolbar que exigem acesso a múltiplos componentes do app.

    É o único dono da lógica de resolução de labels, estado de paralelismo e
    ciclo de temas da toolbar. O App delega completamente a este objeto.
    """

    def __init__(
        self,
        *,
        toolbar_manager: ToolbarManager,
        agent_pool,
        get_agent_profile,
        workspace,
        get_history,
        storage,
        bug_store,
        get_session_started_at,
        renderer,
        config,
        runtime_state,
        input_gate,
        get_execution_mode,
        threads: int,
    ) -> None:
        self._toolbar = toolbar_manager
        self._agent_pool = agent_pool
        self._get_agent_profile = get_agent_profile
        self._workspace = workspace
        self._get_history = get_history
        self._storage = storage
        self._bug_store = bug_store
        self._get_session_started_at = get_session_started_at
        self._renderer = renderer
        self._config = config
        self._runtime_state = runtime_state
        self._input_gate = input_gate
        self._get_execution_mode = get_execution_mode
        self._threads = threads

    def resolve_active_model_label(self) -> str:
        """Resolve o modelo ativo a partir do primeiro profile/agente ativo."""
        request = ActiveModelRequest(
            primary_agent=self._agent_pool.primary,
            get_agent_profile=self._get_agent_profile,
            workspace_cwd=str(getattr(self._workspace, "cwd", ".")),
        )
        return self._toolbar.resolve_active_model_label(request)

    def _format_agent_label(self, agent: str | None) -> str:
        """Formata nome de agente com ícone do profile, sem duplicar ícone existente."""
        name = str(agent or "").strip()
        if not name:
            return ""
        profile = self._get_agent_profile(name)
        icon = str(getattr(profile, "icon", "") or "").strip() if profile is not None else ""
        display_name = str(getattr(profile, "name", "") or name).strip()
        if display_name and display_name != display_name.upper():
            display_name = display_name.capitalize()
        if icon and not display_name.startswith(icon):
            return f"{icon} {display_name}"
        return display_name

    def resolve_next_responder_label(self) -> str:
        """Resolve o agente que deve responder na próxima rodada."""
        responder = self._toolbar.resolve_next_responder_label(self._agent_pool.primary)
        return self._format_agent_label(responder) or responder

    def cycle_renderer_theme(self) -> None:
        """Avança para o próximo tema no TerminalRenderer e persiste na config."""
        self._toolbar.cycle_renderer_theme(self._renderer, self._config)

    def build_input_toolbar_context(self) -> dict[str, str]:
        """Retorna dados de contexto exibidos na toolbar do input."""
        history = self._get_history()
        session_id = getattr(self._storage, "session_id", "")

        def _query_open_bugs(current_session_id: str) -> int:
            bug_store = self._bug_store
            if bug_store is None:
                return 0
            open_bugs = (
                bug_store.query(session_id=current_session_id, status="open", limit=100)
                if current_session_id
                else bug_store.query(status="open", limit=100)
            )
            return len(open_bugs or [])

        request = ToolbarContextRequest(
            responder=self.resolve_next_responder_label(),
            model=self.resolve_active_model_label(),
            branch=str(getattr(self._workspace, "branch", "") or ""),
            theme=str(getattr(self._renderer, "theme_name", "") or ""),
            mode=str(getattr(self._get_execution_mode(), "name", "") or ""),
            threads=int(self._threads or 1),
            history_turns=len(history) if history is not None else None,
            session_id=str(session_id or ""),
            query_open_bugs=_query_open_bugs,
        )
        parallel_state = self.get_parallel_toolbar_state()
        active_agents = parallel_state.get("active_agents", ())
        if active_agents:
            parallel_state["active_agents"] = tuple(
                self._format_agent_label(agent) or str(agent)
                for agent in active_agents
            )
        return self._toolbar.build_input_toolbar_context(
            request,
            parallel_state,
        )

    def set_parallel_toolbar_state(
        self,
        *,
        active: int | None = None,
        queued: int | None = None,
        capacity: int | None = None,
        active_agents: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """Atualiza o snapshot de paralelismo exibido na toolbar do prompt."""
        self._toolbar.set_parallel_toolbar_state(
            active=active,
            queued=queued,
            capacity=capacity,
            active_agents=active_agents,
        )

    def get_parallel_toolbar_state(self) -> dict[str, object]:
        """Retorna cópia do estado de paralelismo da toolbar.

        Usa ``_chat_inflight_count`` como fonte de verdade para slots ativos
        e deriva ``queued`` do tamanho da fila do chat quando disponível.
        """
        active = self._runtime_state.get_chat_inflight_count()
        chat_queue = getattr(self._runtime_state, "chat_queue", None)
        queued_from_queue: int | None = None
        if chat_queue is not None:
            try:
                queued_from_queue = max(0, int(chat_queue.qsize()))
            except Exception:
                queued_from_queue = None
        request = ParallelToolbarSnapshotRequest(
            inflight_count=active,
            queued_count=queued_from_queue,
        )
        return self._toolbar.build_parallel_toolbar_state(request)

    def refresh(self) -> None:
        """Solicita redraw do prompt quando o estado de paralelismo muda."""
        try:
            ToolbarManager.refresh_parallel_toolbar(self._input_gate)
        except Exception:
            logger.debug("falha ao redesenhar toolbar de paralelismo", exc_info=True)

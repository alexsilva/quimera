"""Toolbar management for QuimeraApp."""

import time
import threading
from dataclasses import dataclass
from typing import Callable

from ..profiles.base import extract_model_from_cli_cmd


@dataclass(frozen=True)
class ActiveModelRequest:
    """Dados necessários para resolver o rótulo do modelo ativo."""

    primary_agent: str | None
    get_agent_profile: Callable[[str], object | None]
    workspace_cwd: str


@dataclass(frozen=True)
class ToolbarContextRequest:
    """Dados necessários para montar o contexto da toolbar."""

    responder: str
    model: str
    branch: str
    theme: str
    mode: str
    threads: int
    history_turns: int | None
    session_id: str
    query_open_bugs: Callable[[str], int] | None


@dataclass(frozen=True)
class ParallelToolbarSnapshotRequest:
    """Dados necessários para projetar o estado de paralelismo da toolbar."""

    inflight_count: int
    queued_count: int | None


class ActiveModelResolver:
    """Resolve o modelo exibido na toolbar a partir de dados explícitos."""

    def resolve(self, request: ActiveModelRequest) -> str:
        """Resolve o rótulo do modelo ativo."""
        agent_name = request.primary_agent
        if not agent_name:
            return "unknown"
        profile = request.get_agent_profile(agent_name)
        if profile is None:
            return str(agent_name)
        connection = profile.effective_connection() if hasattr(profile, "effective_connection") else None
        model = getattr(connection, "model", None) if connection is not None else None
        if model:
            return str(model)

        cmd = getattr(connection, "cmd", None) if connection is not None else None
        if not cmd and hasattr(profile, "effective_cmd"):
            try:
                cmd = profile.effective_cmd()
            except Exception:
                cmd = None
        if not cmd:
            cmd = getattr(profile, "cmd", None)

        cli_model: str | None = None
        resolver = getattr(profile, "resolve_runtime_model", None)
        if callable(resolver):
            try:
                resolved = resolver(cwd=request.workspace_cwd)
            except TypeError:
                resolved = resolver()
            if isinstance(resolved, str):
                normalized = resolved.strip()
                if normalized:
                    cli_model = normalized
        if cli_model is None:
            cli_model = extract_model_from_cli_cmd(cmd)
        if isinstance(cli_model, str) and cli_model.strip():
            return cli_model.strip()

        profile_model = getattr(profile, "model", None)
        return str(profile_model) if profile_model else str(profile.name)


class ToolbarManager:
    """Manages toolbar state and related functionality."""

    def __init__(self, threads: int = 1):
        self._parallel_toolbar_lock = threading.Lock()
        self._parallel_toolbar_state = {
            "active": 0,
            "queued": 0,
            "capacity": max(0, threads),
            "active_agents": (),
        }
        self._toolbar_bug_count_cache = {"session_id": "", "count": 0, "ts": 0.0}
        self._toolbar_bug_count_ttl_sec = 1.0
        self._active_model_resolver = ActiveModelResolver()

    def _get_parallel_toolbar_state(self) -> dict[str, object]:
        """Return a copy of the parallelism state from the toolbar."""
        with self._parallel_toolbar_lock:
            return dict(self._parallel_toolbar_state)

    def _set_parallel_toolbar_state(
        self,
        *,
        active: int | None = None,
        queued: int | None = None,
        capacity: int | None = None,
        active_agents: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """Update the parallelism snapshot displayed on the toolbar."""
        with self._parallel_toolbar_lock:
            if active is not None:
                self._parallel_toolbar_state["active"] = max(0, int(active))
            if queued is not None:
                self._parallel_toolbar_state["queued"] = max(0, int(queued))
            if capacity is not None:
                self._parallel_toolbar_state["capacity"] = max(0, int(capacity))
            if active_agents is not None:
                self._parallel_toolbar_state["active_agents"] = tuple(active_agents)

    def set_parallel_toolbar_state(
        self,
        *,
        active: int | None = None,
        queued: int | None = None,
        capacity: int | None = None,
        active_agents: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """Atualiza o snapshot de paralelismo exibido na toolbar."""
        self._set_parallel_toolbar_state(
            active=active,
            queued=queued,
            capacity=capacity,
            active_agents=active_agents,
        )

    def build_parallel_toolbar_state(
        self,
        request: ParallelToolbarSnapshotRequest,
    ) -> dict[str, object]:
        """Projeta o estado de paralelismo da toolbar com dados de runtime."""
        snapshot = self._get_parallel_toolbar_state()
        snapshot["active"] = max(0, int(request.inflight_count))
        if request.queued_count is not None and int(request.queued_count) > 0:
            snapshot["queued"] = max(0, int(request.queued_count))
        return snapshot

    def resolve_active_model_label(self, request: ActiveModelRequest) -> str:
        """Resolve o rótulo do modelo ativo para a toolbar."""
        return self._active_model_resolver.resolve(request)

    @staticmethod
    def resolve_next_responder_label(pending_input_for: str | None, primary_agent: str | None) -> str:
        """Resolve o agente que deve responder na próxima rodada."""
        normalized_pending = str(pending_input_for or "").strip()
        if normalized_pending:
            return normalized_pending
        if primary_agent:
            return str(primary_agent)
        return "unknown"

    @staticmethod
    def cycle_renderer_theme(renderer, config) -> None:
        """Avança para o próximo tema do renderer e persiste na config."""
        if renderer is None:
            return
        cycle = getattr(renderer, "cycle_theme", None)
        if callable(cycle):
            new_name = cycle()
            if new_name and config is not None:
                config.set_theme(new_name)

    @staticmethod
    def _format_active_agents(active_agents: tuple[str, ...] | list[str]) -> str:
        """Compacta a lista de agentes ativos para a toolbar."""
        normalized_agents = [str(agent).strip() for agent in active_agents if str(agent).strip()]
        if not normalized_agents:
            return ""
        visible_agents = normalized_agents[:3]
        extra_agents = len(normalized_agents) - len(visible_agents)
        label = ", ".join(visible_agents)
        if extra_agents > 0:
            label = f"{label} +{extra_agents}"
        return label

    def _resolve_open_bug_count(self, session_id: str, query_open_bugs: Callable[[str], int] | None) -> int:
        """Resolve contagem de bugs com cache local da toolbar."""
        if query_open_bugs is None:
            return 0
        open_bug_count = None
        cache = self.toolbar_bug_count_cache
        cache_ttl = float(self.toolbar_bug_count_ttl_sec or 1.0)
        now_monotonic = time.monotonic()
        if isinstance(cache, dict):
            cached_session = str(cache.get("session_id", ""))
            cached_ts = float(cache.get("ts", 0.0) or 0.0)
            if cached_session == str(session_id or "") and (now_monotonic - cached_ts) < cache_ttl:
                cached_count = cache.get("count", 0)
                try:
                    open_bug_count = int(cached_count)
                except Exception:
                    open_bug_count = 0
        if open_bug_count is None:
            try:
                open_bug_count = int(query_open_bugs(session_id))
                self.toolbar_bug_count_cache = {
                    "session_id": str(session_id or ""),
                    "count": open_bug_count,
                    "ts": now_monotonic,
                }
            except Exception:
                open_bug_count = 0
        return open_bug_count

    def build_input_toolbar_context(
        self,
        request: ToolbarContextRequest,
        parallel_state: dict[str, object],
    ) -> dict[str, str]:
        """Retorna dados de contexto exibidos na toolbar do input."""
        ctx = {
            "responder": request.responder,
            "model": request.model,
        }
        if request.branch:
            ctx["branch"] = request.branch
        ctx["theme"] = request.theme
        if request.mode:
            ctx["mode"] = request.mode
        capacity = int(parallel_state.get("capacity", max(0, request.threads)) or 0)
        active = int(parallel_state.get("active", 0) or 0)
        queued = int(parallel_state.get("queued", 0) or 0)
        if active > 0 or queued > 0 or capacity > 1:
            slots_label = f"{active}/{capacity}"
            if queued:
                slots_label = f"{slots_label} · 📥 {queued}"
            ctx["parallel"] = slots_label
        active_agents = parallel_state.get("active_agents", ())
        if active_agents:
            label = self._format_active_agents(active_agents)
            if label:
                ctx["active_agents"] = label
        if request.history_turns is not None:
            ctx["turns"] = str(request.history_turns)
        if request.session_id:
            ctx["session"] = request.session_id
        open_bug_count = self._resolve_open_bug_count(request.session_id, request.query_open_bugs)
        if open_bug_count > 0:
            ctx["open_bugs"] = str(open_bug_count)
        return ctx

    @staticmethod
    def refresh_parallel_toolbar(input_gate) -> None:
        """Solicita redraw do prompt quando o estado de paralelismo muda."""
        redisplay = getattr(input_gate, "redisplay", None)
        if not callable(redisplay):
            return
        redisplay()

    @property
    def toolbar_bug_count_cache(self) -> dict:
        """Get the toolbar bug count cache."""
        return self._toolbar_bug_count_cache

    @toolbar_bug_count_cache.setter
    def toolbar_bug_count_cache(self, value: dict) -> None:
        """Set the toolbar bug count cache."""
        self._toolbar_bug_count_cache = value

    @property
    def toolbar_bug_count_ttl_sec(self) -> float:
        """Get the toolbar bug count TTL."""
        return self._toolbar_bug_count_ttl_sec

    @toolbar_bug_count_ttl_sec.setter
    def toolbar_bug_count_ttl_sec(self, value: float) -> None:
        """Set the toolbar bug count TTL."""
        self._toolbar_bug_count_ttl_sec = value

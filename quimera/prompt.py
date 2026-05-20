import json
from pathlib import Path

from . import plugins
from .config import DEFAULT_HISTORY_WINDOW, DEFAULT_USER_NAME
from .constants import EXTEND_MARKER
from .evidence import EvidenceFormatter, EvidenceStore
from .execution_mode_presenter import ExecutionModePresenter
from .handoff_presenter import HandoffPresenter
from .memory_selector import MemorySelector
from .prompt_budget import PromptBudget
from .prompt_kinds import PromptKind, coerce_prompt_kind
from .prompt_templates import get_prompt_template
from .shared_state_presenter import SharedStatePresenter


class PromptBuilder:
    """Monta o prompt com contexto persistente e janela recente da conversa."""

    def __init__(
            self,
            context_manager,
            history_window=DEFAULT_HISTORY_WINDOW,
            session_state=None,
            user_name=None,
            active_agents=None,
            active_agents_provider=None,
            metrics_tracker=None,
    ):
        self.context_manager = context_manager
        self.session_state = session_state or {}
        self.active_agents = list(active_agents) if active_agents is not None else plugins.all_names()
        self.active_agents_provider = active_agents_provider
        self.metrics_tracker = metrics_tracker
        self.memory_selector = MemorySelector(history_window, user_name)
        self.shared_state_presenter = SharedStatePresenter()
        self.handoff_presenter = HandoffPresenter()
        self.execution_mode_presenter = ExecutionModePresenter()
        self.prompt_budget = PromptBudget()

    def _get_active_agents(self) -> list[str]:
        """Retorna a lista de agentes ativos atual, com fallback seguro."""
        provider = self.active_agents_provider
        if callable(provider):
            try:
                provided = provider()
            except Exception:
                provided = None
            if provided is not None:
                return list(provided)
        return list(self.active_agents)

    @property
    def history_window(self):
        return self.memory_selector.history_window

    @history_window.setter
    def history_window(self, value):
        self.memory_selector.history_window = value

    def build(
            self,
            agent,
            history,
            is_first_speaker=False,
            handoff=None,
            debug=False,
            primary=True,
            shared_state=None,
            handoff_only=False,
            from_agent=None,
            skip_tool_prompt=False,
            execution_mode=None,
            prompt_kind=PromptKind.CHAT,
            request_override=None,
    ):
        """Gera o prompt final para um agente considerando contexto, handoff e histórico."""
        if history is None:
            history = []
        elif not isinstance(history, list):
            history = list(history)
        normalized_prompt_kind = coerce_prompt_kind(prompt_kind)
        is_chat_prompt = normalized_prompt_kind is PromptKind.CHAT
        context = self.context_manager.load() if is_chat_prompt else ""
        active_agents = self._get_active_agents()

        if handoff_only:
            route_candidates = [n for n in active_agents if n.lower() != agent.lower()]
            if from_agent:
                route_candidates = [n for n in route_candidates if n.lower() != from_agent.lower()]
            route_agents = ", ".join(route_candidates) if route_candidates else ""
            is_first_speaker_flag = False
            is_reviewer = False
        else:
            route_agents = ", ".join(active_agents) if active_agents else "nenhum"
            is_first_speaker_flag = is_first_speaker
            is_reviewer = not is_first_speaker

        other_agents = [n for n in active_agents if n.lower() != agent.lower()]
        agents_list = ", ".join(n.upper() for n in other_agents) if other_agents else "nenhum"

        session_id = ""
        current_job_id = ""
        workspace_root = ""
        current_dir = ""
        os_info = ""
        render_debug_active = False
        render_log_path = ""
        render_ansi_path = ""
        metrics_path = ""
        if self.session_state and primary:
            session_id = self.session_state.get("session_id", "desconhecida")
            current_job_id = self.session_state.get("current_job_id", "desconhecido")
            workspace_root = self.session_state.get("workspace_root", "desconhecido")
            current_dir = self.session_state.get("current_dir", ".")
            os_info = self.session_state.get("os_info", "")
            render_debug_active = bool(self.session_state.get("render_debug_active", False))
            render_log_path = self.session_state.get("render_log_path", "")
            render_ansi_path = self.session_state.get("render_ansi_path", "")
            metrics_path = self.session_state.get("metrics_path", "")

        handoff_fields = self.handoff_presenter.present(handoff, from_agent)
        if is_chat_prompt:
            override_request = (request_override or "").strip()
            if override_request:
                request = override_request
                request_index = self.memory_selector.find_request_index(history, request)
            else:
                request_index, request = self.memory_selector.select_request(history)
            fact_indexes, facts = self.memory_selector.select_facts(history, current_agent=agent)
            recent_conversation = self.memory_selector.build_conversation_block(
                history,
                skip_indexes={idx for idx in [request_index, *fact_indexes] if idx is not None},
                current_agent=agent,
            )
            shared_state_json, completed_task_results = self.shared_state_presenter.present(shared_state)
        else:
            request_index, request = None, ""
            fact_indexes, facts = [], ""
            recent_conversation = ""
            shared_state_json, completed_task_results = "", ""

        metrics = ""
        if self.metrics_tracker:
            feedback = self.metrics_tracker.generate_feedback(agent)
            if feedback:
                metrics = feedback
        execution_mode_prompt = self.execution_mode_presenter.present(execution_mode)
        evidence_section = self._build_evidence_section(shared_state, session_id)
        template = get_prompt_template(normalized_prompt_kind)
        full_prompt = template.render(
            agent=agent.upper(),
            user_name=self.memory_selector.user_name.upper(),
            agents=agents_list,
            route_agents=route_agents,
            handoff_only=handoff_only,
            is_first_speaker=is_first_speaker_flag,
            is_reviewer=is_reviewer,
            marker=EXTEND_MARKER,
            session_id=session_id,
            current_job_id=current_job_id,
            workspace_root=workspace_root,
            current_dir=current_dir,
            os_info=os_info,
            render_debug_active=render_debug_active,
            render_log_path=render_log_path,
            render_ansi_path=render_ansi_path,
            metrics_path=metrics_path,
            context=context,
            request=request,
            facts=facts,
            shared_state_json=shared_state_json,
            completed_task_results=completed_task_results,
            handoff_present=handoff_fields["handoff_present"],
            handoff_id=handoff_fields["handoff_id"],
            handoff_task=handoff_fields["handoff_task"],
            handoff_from=handoff_fields["handoff_from"],
            handoff_context=handoff_fields["handoff_context"],
            handoff_expected=handoff_fields["handoff_expected"],
            handoff_priority=handoff_fields["handoff_priority"],
            handoff_chain=handoff_fields["handoff_chain"],
            handoff_raw=handoff_fields["handoff_raw"],
            state_update_enabled=True,
            recent_conversation=recent_conversation,
            metrics=metrics,
            execution_mode_prompt=execution_mode_prompt,
            evidence_section=evidence_section,
        )

        if debug:
            metrics = self.prompt_budget.measure(
                full_prompt=full_prompt,
                route_agents=route_agents,
                session_id=session_id,
                current_job_id=current_job_id,
                workspace_root=workspace_root,
                current_dir=current_dir,
                context=context,
                request=request,
                facts=facts,
                shared_state_json=shared_state_json,
                completed_task_results=completed_task_results,
                recent_conversation=recent_conversation,
                handoff_fields=handoff_fields,
                history=history,
                history_window=self.memory_selector.history_window,
                primary=primary,
            )
            return full_prompt, metrics

        return full_prompt

    def _build_evidence_section(self, shared_state, session_id: str) -> str:
        if not isinstance(shared_state, dict):
            return ""
        evidence_session_id = shared_state.get("session_id") or session_id
        base_dir = self.session_state.get("workspace_tmp_root") if isinstance(self.session_state, dict) else None
        if not evidence_session_id or not base_dir:
            return ""
        store = EvidenceStore(Path(base_dir), evidence_session_id)
        try:
            evidences = store.query(evidence_session_id)
        finally:
            store.close()
        if not evidences:
            return ""
        return EvidenceFormatter().format(evidences)

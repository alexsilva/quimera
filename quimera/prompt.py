from pathlib import Path

from . import profiles
from .config import DEFAULT_HISTORY_WINDOW
from .evidence import EvidenceFormatter, EvidenceStore
from .execution_mode_presenter import ExecutionModePresenter
from .delegate_presenter import DelegatePresenter
from .memory_selector import MemorySelector
from .prompt_budget import PromptBudget
from .prompt_kinds import PromptKind, coerce_prompt_kind
from .prompt_templates import PromptText, get_prompt_template
from .shared_state_presenter import SharedStatePresenter
from .bugs import BugStore, format_bug_context


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
            orchestrator_provider=None,
            metrics_tracker=None,
    ):
        self.context_manager = context_manager
        self.session_state = session_state or {}
        self.active_agents = list(active_agents) if active_agents is not None else profiles.all_names()
        self.active_agents_provider = active_agents_provider
        self.orchestrator_provider = orchestrator_provider
        self.metrics_tracker = metrics_tracker
        self.memory_selector = MemorySelector(history_window, user_name)
        self.shared_state_presenter = SharedStatePresenter()
        self.delegate_presenter = DelegatePresenter()
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

    def _get_orchestrator(self) -> str | None:
        """Retorna o agente orquestrador ativo, ou None se a rotação estiver livre."""
        provider = self.orchestrator_provider
        if callable(provider):
            try:
                return provider()
            except Exception:
                return None
        return None

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
            delegation=None,
            debug=False,
            primary=True,
            shared_state=None,
            delegation_only=False,
            from_agent=None,
            skip_tool_prompt=False,
            execution_mode=None,
            prompt_kind=PromptKind.CHAT,
            request_override=None,
    ) -> PromptText | tuple[PromptText, dict]:
        """Gera o prompt final para um agente considerando contexto, delegação e histórico."""
        if history is None:
            history = []
        elif not isinstance(history, list):
            history = list(history)
        normalized_prompt_kind = coerce_prompt_kind(prompt_kind)
        is_chat_prompt = normalized_prompt_kind is PromptKind.CHAT
        context = self.context_manager.load() if is_chat_prompt else ""
        active_agents = self._get_active_agents()

        if delegation_only:
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

        orchestrator = self._get_orchestrator()
        is_orchestrator = bool(
            orchestrator
            and not delegation_only
            and agent.lower() == str(orchestrator).lower()
        )
        orchestrator_agents = ""
        if is_orchestrator:
            coordinated = [n for n in active_agents if n.lower() != agent.lower()]
            orchestrator_agents = ", ".join(coordinated) if coordinated else "nenhum"
            # O bloco dedicado de orquestração substitui as regras genéricas de rota,
            # evitando instruções de delegação duplicadas no prompt.
            route_agents = ""

        other_agents = [n for n in active_agents if n.lower() != agent.lower()]
        agents_list = ", ".join(other_agents) if other_agents else "nenhum"

        session_id = ""
        current_job_id = ""
        workspace_root = ""
        current_dir = ""
        os_info = ""
        render_debug_active = False
        render_log_path = ""
        render_ansi_path = ""
        metrics_path = ""
        app_log_path = ""
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
            app_log_path = (
                self.session_state.get("app_log_path", "")
                if render_debug_active
                else ""
            )

        delegation_fields = self.delegate_presenter.present(delegation, from_agent)
        if is_chat_prompt:
            override_request = (request_override or "").strip()
            if override_request:
                request = override_request
                request_index = self.memory_selector.find_request_index(history, request)
            else:
                request_index, request = self.memory_selector.select_request(history)
            recent_conversation = self.memory_selector.build_conversation_block(
                history,
                skip_indexes={idx for idx in [request_index] if idx is not None},
                current_agent=agent,
            )
            shared_state_json, completed_task_results = self.shared_state_presenter.present(shared_state)
        else:
            request_index, request = None, ""
            recent_conversation = ""
            shared_state_json, completed_task_results = "", ""

        metrics = ""
        if self.metrics_tracker:
            feedback = self.metrics_tracker.generate_feedback(agent)
            if feedback:
                metrics = feedback
        execution_state = self._build_execution_state_block(shared_state)
        execution_mode_prompt = self.execution_mode_presenter.present(execution_mode)
        evidence_context_raw = self._build_evidence_context(shared_state, session_id)
        bug_context_raw = self._build_bugs_context(shared_state, session_id)
        template = get_prompt_template(normalized_prompt_kind)
        prompt_text = template.render_prompt(
            normalized_prompt_kind,
            agent=agent,
            user_name=self.memory_selector.user_name.upper(),
            agents=agents_list,
            route_agents=route_agents,
            is_orchestrator=is_orchestrator,
            orchestrator_agents=orchestrator_agents,
            delegation_only=delegation_only,
            is_first_speaker=is_first_speaker_flag,
            is_reviewer=is_reviewer,
            session_id=session_id,
            current_job_id=current_job_id,
            workspace_root=workspace_root,
            current_dir=current_dir,
            os_info=os_info,
            render_debug_active=render_debug_active,
            render_log_path=render_log_path,
            render_ansi_path=render_ansi_path,
            metrics_path=metrics_path,
            app_log_path=app_log_path,
            context=context,
            request=request,
            shared_state_json=shared_state_json,
            completed_task_results=completed_task_results,
            delegation_present=delegation_fields["delegation_present"],
            delegation_id=delegation_fields["delegation_id"],
            delegation_request=delegation_fields["delegation_request"],
            delegation_from=delegation_fields["delegation_from"],
            delegation_context=delegation_fields["delegation_context"],
            delegation_role=delegation_fields["delegation_role"],
            delegation_role_contract=delegation_fields["delegation_role_contract"],
            delegation_access_list=delegation_fields["delegation_access_list"],
            delegation_expected=delegation_fields["delegation_expected"],
            delegation_priority=delegation_fields["delegation_priority"],
            delegation_chain=delegation_fields["delegation_chain"],
            delegation_raw=delegation_fields["delegation_raw"],
            execution_state=execution_state,
            recent_conversation=recent_conversation,
            metrics=metrics,
            execution_mode_prompt=execution_mode_prompt,
            evidence_context_raw=evidence_context_raw,
            bug_context_raw=bug_context_raw,
        )
        if debug:
            metrics = self.prompt_budget.measure(
                full_prompt=prompt_text,
                route_agents=route_agents,
                session_id=session_id,
                current_job_id=current_job_id,
                workspace_root=workspace_root,
                current_dir=current_dir,
                context=context,
                request=request,
                execution_state=execution_state,
                shared_state_json=shared_state_json,
                completed_task_results=completed_task_results,
                recent_conversation=recent_conversation,
                delegation_fields=delegation_fields,
                history=history,
                history_window=self.memory_selector.history_window,
                primary=primary,
            )
            return prompt_text, metrics

        return prompt_text

    def _build_execution_state_block(self, shared_state) -> str:
        if not isinstance(shared_state, dict):
            return ""
        lines = []
        for key, label in [
            ("goal_canonical", "Objetivo"),
            ("current_step", "Passo atual"),
            ("acceptance_criteria", "Critérios de aceite"),
            ("next_step", "Próximo passo"),
            ("allowed_scope", "Escopo permitido"),
            ("non_goals", "Não escopo"),
        ]:
            value = shared_state.get(key)
            if not value:
                continue
            if isinstance(value, list):
                value = "; ".join(str(v) for v in value)
            lines.append(f"- {label}: {value}")
        return "\n".join(lines) if lines else ""

    def _build_evidence_section(self, shared_state, session_id: str) -> str:
        evidence_section = self._build_evidence_context(shared_state, session_id)
        bugs_section = self._build_bugs_context(shared_state, session_id)
        if evidence_section and bugs_section:
            return f"{evidence_section}\n\n{bugs_section}"
        return evidence_section or bugs_section

    def _build_evidence_context(self, shared_state, session_id: str) -> str:
        if not isinstance(shared_state, dict):
            return ""
        evidence_session_id = shared_state.get("session_id") or session_id
        base_dir = self.session_state.get("workspace_tmp_root") if isinstance(self.session_state, dict) else None
        if not evidence_session_id or not base_dir:
            return ""
        try:
            store = EvidenceStore(Path(base_dir), evidence_session_id)
        except Exception:
            return ""
        try:
            evidences = store.query(evidence_session_id)
        finally:
            store.close()
        if not evidences:
            return ""
        return EvidenceFormatter().format(evidences)

    def _build_bugs_context(self, shared_state, session_id: str) -> str:
        if not isinstance(shared_state, dict):
            return ""
        bug_session_id = shared_state.get("session_id") or session_id
        if not bug_session_id:
            return ""
        tmp_root = self.session_state.get("workspace_tmp_root") if isinstance(self.session_state, dict) else None
        if not tmp_root:
            return ""
        try:
            store = BugStore(Path(tmp_root) / "data" / "logs")
        except Exception:
            return ""
        try:
            reports = store.query(session_id=bug_session_id, status="open", limit=3)
        except Exception:
            return ""
        finally:
            try:
                store.close()
            except Exception:
                pass
        return format_bug_context(reports)

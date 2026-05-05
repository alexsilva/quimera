"""Componentes de `quimera.prompt`."""
import json

from . import plugins
from .config import DEFAULT_HISTORY_WINDOW, DEFAULT_USER_NAME
from .constants import EXTEND_MARKER
from .prompt_templates import prompt_template


class PromptBuilder:
    """Monta o prompt com contexto persistente e janela recente da conversa."""

    def __init__(
            self,
            context_manager,
            history_window=DEFAULT_HISTORY_WINDOW,
            session_state=None,
            user_name=None,
            active_agents=None,
            metrics_tracker=None,
    ):
        """Inicializa uma instância de PromptBuilder."""
        self.context_manager = context_manager
        self.history_window = history_window
        self.session_state = session_state or {}
        self.user_name = user_name or DEFAULT_USER_NAME
        self.active_agents = list(active_agents) if active_agents is not None else plugins.all_names()
        self.metrics_tracker = metrics_tracker

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
    ):
        """Gera o prompt final enviado ao agente da vez.

        primary=False omite session_state — adequado para agentes secundários que já
        têm o contexto da conversa e não precisam do estado de bootstrap da sessão.

        O parâmetro skip_tool_prompt é mantido por compatibilidade, mas o prompt
        não descreve mais ferramentas em texto.
        """
        if history is None:
            history = []
        elif not isinstance(history, list):
            history = list(history)
        context = self.context_manager.load()

        if handoff_only:
            route_agents = ""
            is_first_speaker_flag = False
            is_reviewer = False
        else:
            route_agents = ", ".join(self.active_agents) if self.active_agents else "nenhum"
            is_first_speaker_flag = is_first_speaker
            is_reviewer = not is_first_speaker

        other_agents = [n for n in self.active_agents if n.lower() != agent.lower()]
        agents_list = ", ".join(n.upper() for n in other_agents) if other_agents else "nenhum"
        shared = shared_state or {}
        execution_keys = {
            "goal", "goal_canonical", "decisions", "current_step",
            "acceptance_criteria", "allowed_scope", "non_goals",
            "out_of_scope_notes", "evidence", "next_step",
        }
        fallback_shared = {}
        if shared:
            fallback_shared = {
                k: v
                for k, v in self._trim_shared_state(shared).items()
                if k not in execution_keys
            }

        session_id = ""
        current_job_id = ""
        workspace_root = ""
        current_dir = ""
        os_info = ""
        if self.session_state and primary:
            session_id = self.session_state.get("session_id", "desconhecida")
            current_job_id = self.session_state.get("current_job_id", "desconhecido")
            workspace_root = self.session_state.get("workspace_root", "desconhecido")
            current_dir = self.session_state.get("current_dir", ".")
            os_info = self.session_state.get("os_info", "")
        handoff_fields = self._build_handoff_fields(handoff, from_agent)
        request_index, request = self._build_request_content(history)
        fact_indexes, facts = self._build_facts_content(history, current_agent=agent)
        shared_state_json = ""
        completed_task_results = shared.get("completed_task_results", "") or ""
        if fallback_shared:
            shared_state_json = json.dumps(fallback_shared, ensure_ascii=False, indent=2)

        metrics = ""
        if self.metrics_tracker:
            feedback = self.metrics_tracker.generate_feedback(agent)
            if feedback:
                metrics = feedback
        recent_conversation = self._build_conversation_block(
            history,
            skip_indexes={idx for idx in [request_index, *fact_indexes] if idx is not None},
            current_agent=agent,
        )
        full_prompt = prompt_template.render(
            agent=agent.upper(),
            user_name=self.user_name.upper(),
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
            recent_conversation=recent_conversation,
            metrics=metrics,
        )

        if debug:
            metrics = {
                "rules_chars": len(route_agents),
                "session_state_chars": len(session_id) + len(str(current_job_id)) + len(workspace_root) + len(current_dir),
                "persistent_chars": len(context),
                "request_chars": len(request),
                "facts_chars": len(facts),
                "shared_state_chars": len(shared_state_json) + len(completed_task_results),
                "history_chars": len(recent_conversation),
                "handoff_chars": sum(len(str(v)) for v in handoff_fields.values()),
                "total_chars": len(full_prompt),
                "history_messages": len(history[-self.history_window:]),
                "primary": primary,
            }
            return full_prompt, metrics

        return full_prompt

    @staticmethod
    def _trim_shared_state(state, decisions_tail=5):
        # Extend trimming to cover new canonical goal/step fields and avoid leaking internal planning data.
        """Executa trim shared state."""
        core_keys = {
            "goal_canonical",
            "current_step",
            "acceptance_criteria",
            "task_overview",
            "decisions",
            "working_dir",
            "workspace_root",
            "evidence",
            "next_step",
            "goal",
            "allowed_scope",
            "non_goals",
            "out_of_scope_notes",
        }
        trimmed = {}
        for k in core_keys:
            if k in state:
                trimmed[k] = state[k]
        if "decisions" in state:
            trimmed["decisions"] = state["decisions"][-decisions_tail:]
        return trimmed

    def _build_request_content(self, history):
        """Retorna o conteúdo do bloco de request."""
        window_start = max(0, len(history) - self.history_window)
        for index in range(len(history) - 1, window_start - 1, -1):
            message = history[index]
            if message.get("role") == "human":
                content = (message.get("content") or "").strip()
                if content:
                    return index, content
        return None, ""

    def _build_facts_content(self, history, max_items=4, current_agent=None):
        """Retorna o conteúdo do bloco de fatos."""
        facts = []
        fact_indexes = []
        window_start = max(0, len(history) - self.history_window)
        current_agent_lower = (current_agent or "").strip().lower()
        for index in range(len(history) - 1, window_start - 1, -1):
            message = history[index]
            role = message.get("role")
            if role == "human":
                continue
            if current_agent_lower and str(role).strip().lower() == current_agent_lower:
                continue
            content = (message.get("content") or "").strip()
            if not content:
                continue
            if self._should_skip_fact(content):
                continue
            facts.append(f"[{self._display_role(role)}] {content}")
            fact_indexes.append(index)
            if len(facts) >= max_items:
                break
        if not facts:
            return [], ""
        facts.reverse()
        fact_indexes.reverse()
        return fact_indexes, "\n".join(facts)

    @staticmethod
    def _should_skip_fact(content):
        """Evita promover meta-instruções antigas para o bloco de fatos."""
        lowered = content.lower()
        blocked_markers = (
            "goal_canonical",
            "prompt_state",
            "objetivo fixo",
            "não redefina o objetivo",
            "nao redefina o objetivo",
            "[state_update]",
            "fatos observados recentes",
            # Evita promover "saída de ferramenta"/diff não verificável a fato canônico.
            "git diff",
            "diff --git ",
            "```diff",
            "+++ b/",
            "--- a/",
            "@@ ",
            "arquivo alterado:",
        )
        return any(marker in lowered for marker in blocked_markers)

    def _build_conversation_block(self, history, skip_indexes=None, current_agent=None):
        """Monta o conteúdo residual da conversa, sem um segundo envelope interno."""
        skip_indexes = skip_indexes or set()
        window_start = max(0, len(history) - self.history_window)
        lines = []
        included_indexes = set()
        for index, message in enumerate(history[window_start:], start=window_start):
            if index in skip_indexes:
                continue
            content = (message.get("content") or "").strip()
            if not content:
                continue
            if message.get("role") != "human" and self._should_skip_fact(content):
                continue
            included_indexes.add(index)
            lines.append(
                self._format_conversation_entry(
                    role=self._display_role(message["role"]),
                    content=content,
                )
            )

        current_agent_lower = (current_agent or "").strip().lower()
        if current_agent_lower:
            for index in range(window_start - 1, -1, -1):
                message = history[index]
                if str(message.get("role") or "").strip().lower() != current_agent_lower:
                    continue
                if index in skip_indexes or index in included_indexes:
                    break
                content = (message.get("content") or "").strip()
                if not content:
                    break
                if self._should_skip_fact(content):
                    continue
                lines.insert(
                    0,
                    self._format_conversation_entry(
                        role=self._display_role(message["role"]),
                        content=content,
                    ),
                )
                break
        return "\n".join(lines) if lines else "[sem itens residuais na conversa recente]"

    @staticmethod
    def _format_conversation_entry(role, content):
        """Formata uma entrada residual da conversa dentro do bloco único."""
        return f"[{role}]: {content}"

    def _build_handoff_fields(self, handoff, from_agent=None):
        """Extrai apenas os dados dinâmicos do handoff para o template."""
        empty = {
            "handoff_present": "",
            "handoff_id": "",
            "handoff_task": "",
            "handoff_from": "",
            "handoff_context": "",
            "handoff_expected": "",
            "handoff_priority": "",
            "handoff_chain": "",
            "handoff_raw": "",
        }
        if not handoff:
            return empty
        if isinstance(handoff, dict):
            chain = handoff.get("chain", [])
            priority = handoff.get("priority", "normal")
            return {
                "handoff_present": "1",
                "handoff_id": str(handoff.get("handoff_id") or "").strip(),
                "handoff_task": (handoff.get("task") or "").strip(),
                "handoff_from": (from_agent or "").strip(),
                "handoff_context": (handoff.get("context") or "").strip(),
                "handoff_expected": (handoff.get("expected") or "").strip(),
                "handoff_priority": (
                    str(priority).strip().upper()
                    if priority and str(priority).strip().lower() != "normal"
                    else ""
                ),
                "handoff_chain": " -> ".join(chain) if chain else "",
                "handoff_raw": "",
            }
        return {
            **empty,
            "handoff_present": "1",
            "handoff_raw": str(handoff).strip(),
        }

    def _display_role(self, role):
        """Executa display role."""
        if role == "human":
            return self.user_name.upper()
        return role.upper()

"""Componentes de `quimera.prompt`."""
import json

from . import plugins
from .config import DEFAULT_HISTORY_WINDOW
from .constants import (
    EXTEND_MARKER,
    build_route_rule,
    build_tools_prompt,
)
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
        self.user_name = user_name or "Você"
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

        skip_tool_prompt=True omite PROMPT_TOOL_RULE e build_tools_prompt() — usado
        por agentes com driver de API onde as ferramentas são declaradas via schema
        OpenAI e as instruções text-based conflitariam com o protocolo da API.
        """
        context = self.context_manager.load()

        if handoff_only:
            route_rule = ""
            tool_rule = ""
            mode_rule = prompt_template.handoff_rule
        else:
            route_rule = build_route_rule(self.active_agents)
            tool_rule = prompt_template.tool_rule if not skip_tool_prompt else ""
            if is_first_speaker:
                mode_rule = prompt_template.debate_rule.format(marker=EXTEND_MARKER)
            else:
                mode_rule = prompt_template.reviewer_rule

        tools_prompt = (
            '<tools title="Ferramentas disponíveis">\n'
            f"{build_tools_prompt()}\n"
            "</tools>"
            if not skip_tool_prompt else ""
        )

        other_agents = [n for n in self.active_agents if n.lower() != agent.lower()]
        agents_list = ", ".join(n.upper() for n in other_agents) if other_agents else "nenhum"
        shared = shared_state or {}
        has_goal = "goal_canonical" in shared
        fallback_shared = {}
        if shared and not has_goal:
            execution_keys = {
                "goal", "goal_canonical", "decisions", "current_step",
                "acceptance_criteria", "allowed_scope", "non_goals",
                "out_of_scope_notes", "evidence", "next_step",
            }
            fallback_shared = {
                k: v
                for k, v in self._trim_shared_state(shared).items()
                if k not in execution_keys
            }

        state_update_rule = prompt_template.state_update_rule if fallback_shared else ""

        if self.session_state and primary:
            session_block = (
                '<session_state title="Estado da sessão">\n'
                f"- SESSÃO ATUAL: {self.session_state.get('session_id', 'desconhecida')}\n"
                f"- JOB_ID ATUAL: {self.session_state.get('current_job_id', 'desconhecido')}\n"
                f"- WORKSPACE RAIZ: {self.session_state.get('workspace_root', 'desconhecido')}\n"
                f"- DIRETÓRIO ATUAL: {self.session_state.get('current_dir', '.')}\n"
                "</session_state>"
            )
        else:
            session_block = ""
        context_block = (
            '<persistent_context title="Contexto persistente do workspace">\n'
            f"{context}\n"
            "</persistent_context>"
            if context
            else ""
        )
        handoff_block = (
            '<handoff title="Mensagem direta do outro agente">\n'
            f"{self._format_handoff(handoff, from_agent)}\n"
            "</handoff>"
            if handoff
            else ""
        )
        request_index, request_block = self._build_request_block(history)
        fact_indexes, facts_block = self._build_facts_block(history, current_agent=agent)
        shared_state_block = ""
        if shared_state and not has_goal:
            if fallback_shared:
                state_lines = json.dumps(fallback_shared, ensure_ascii=False, indent=2)
                shared_state_block = prompt_template.shared_state.format(shared_state_json=state_lines)
        elif shared_state and has_goal and "completed_task_results" in shared_state:
            results = shared_state["completed_task_results"]
            if results:
                shared_state_block = (
                    '<completed_tasks title="Tarefas concluídas">\n'
                    f"{results}\n"
                    "</completed_tasks>"
                )

        metrics_block = ""
        if self.metrics_tracker:
            feedback = self.metrics_tracker.generate_feedback(agent)
            if feedback:
                metrics_block = (
                    '<agent_metrics title="Métricas do agente atual (apenas referência)">\n'
                    f"{feedback}\n"
                    "</agent_metrics>"
                )

        conversation = self._build_conversation_block(
            history,
            skip_indexes={idx for idx in [request_index, *fact_indexes] if idx is not None},
        )
        conversation_block = (
            '<recent_conversation title="Conversa recente">\n'
            f"{conversation}\n"
            "</recent_conversation>"
        )
        rules_suffix = self._join_prompt_blocks(
            route_rule,
            tool_rule,
            mode_rule,
            state_update_rule,
        )
        rules_body = self._join_prompt_blocks(
            prompt_template.base_rules,
            rules_suffix,
        )
        body_blocks = self._join_prompt_blocks(
            tools_prompt,
            session_block,
            context_block,
            request_block,
            facts_block,
            shared_state_block,
            handoff_block,
        )
        full_prompt = prompt_template.render(
            agent=agent.upper(),
            user_name=self.user_name.upper(),
            agents=agents_list,
            rules_body=rules_body,
            body_blocks=body_blocks,
            conversation=conversation,
            metrics_block=metrics_block,
        )

        if debug:
            metrics = {
                "rules_chars": len(route_rule) + len(tool_rule) + len(mode_rule) + len(state_update_rule),
                "session_state_chars": len(session_block),
                "persistent_chars": len(context_block),
                "request_chars": len(request_block),
                "facts_chars": len(facts_block),
                "shared_state_chars": len(shared_state_block),
                "history_chars": len(conversation_block),
                "handoff_chars": len(handoff_block),
                "total_chars": len(full_prompt),
                "history_messages": len(history[-self.history_window:]),
                "primary": primary,
            }
            return full_prompt, metrics

        return full_prompt

    @staticmethod
    def _join_prompt_blocks(*blocks):
        """Junta blocos não vazios com uma única linha em branco entre eles."""
        normalized = [block.strip() for block in blocks if block and block.strip()]
        return "\n\n".join(normalized)

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

    def _build_request_block(self, history):
        """Monta request block."""
        window_start = max(0, len(history) - self.history_window)
        for index in range(len(history) - 1, window_start - 1, -1):
            message = history[index]
            if message.get("role") == "human":
                content = (message.get("content") or "").strip()
                if content:
                    return index, prompt_template.request.format(
                        user_name=self.user_name.upper(),
                        request=content,
                    )
        return None, ""

    def _build_facts_block(self, history, max_items=4, current_agent=None):
        """Monta facts block."""
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
        return fact_indexes, prompt_template.facts.format(facts="\n".join(facts))

    @staticmethod
    def _should_skip_fact(content):
        """Evita promover meta-instruções antigas para o bloco de fatos."""
        lowered = content.lower()
        blocked_markers = (
            "goal_canonical",
            "prompt_state",
            "shared_state",
            "estado compartilhado",
            "objetivo fixo",
            "não redefina o objetivo",
            "nao redefina o objetivo",
            "[state_update]",
            "fatos observados recentes",
            "contexto persistente",
        )
        return any(marker in lowered for marker in blocked_markers)

    def _build_conversation_block(self, history, skip_indexes=None):
        """Monta o conteúdo residual da conversa, sem um segundo envelope interno."""
        skip_indexes = skip_indexes or set()
        window_start = max(0, len(history) - self.history_window)
        lines = []
        for index, message in enumerate(history[window_start:], start=window_start):
            if index in skip_indexes:
                continue
            content = (message.get("content") or "").strip()
            if not content:
                continue
            if message.get("role") != "human" and self._should_skip_fact(content):
                continue
            lines.append(
                self._format_conversation_entry(
                    role=self._display_role(message["role"]),
                    content=content,
                )
            )
        return "\n".join(lines) if lines else "[sem itens residuais na conversa recente]"

    @staticmethod
    def _format_conversation_entry(role, content):
        """Formata uma entrada residual da conversa dentro do bloco único."""
        return f"[{role}]: {content}"

    def _format_handoff(self, handoff, from_agent=None):
        """Formata handoff."""
        if isinstance(handoff, dict):
            task = (handoff.get("task") or "").strip()
            context = (handoff.get("context") or "").strip()
            expected = (handoff.get("expected") or "").strip()
            handoff_id = handoff.get("handoff_id")
            priority = handoff.get("priority", "normal")
            chain = handoff.get("chain", [])

            parts = []
            if handoff_id:
                parts.append(f"HANDOFF_ID:\n{handoff_id}")
            parts.append(f"TASK:\n{task}")
            if from_agent:
                parts.append(f"FROM:\n{from_agent}")
            if context:
                parts.append(f"CONTEXT:\n{context}")
            if expected:
                parts.append(f"EXPECTED:\n{expected}")
            if priority and priority != "normal":
                parts.append(f"PRIORITY:\n{priority.upper()}")
            if chain:
                parts.append(f"CHAIN:\n{' -> '.join(chain)}")

            return "\n\n".join(parts).strip()
        return str(handoff).strip()

    def _display_role(self, role):
        """Executa display role."""
        if role == "human":
            return self.user_name.upper()
        return role.upper()

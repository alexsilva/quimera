"""Componentes de `quimera.prompt`."""
import json

from . import plugins
from .constants import (
    EXTEND_MARKER,
    PROMPT_HEADER,
    PROMPT_CONTEXT,
    PROMPT_REQUEST,
    PROMPT_FACTS,
    PROMPT_CONVERSATION,
    PROMPT_SPEAKER,
    PROMPT_BASE_RULES,
    PROMPT_DEBATE_RULE,
    PROMPT_GOAL_LOCK,
    PROMPT_STEP_LOCK,
    PROMPT_ACCEPTANCE_CRITERIA,
    PROMPT_SCOPE_CONTROL,
    PROMPT_GOAL_EXECUTION_RULES,
    build_route_rule,
    build_tools_prompt,
    PROMPT_SESSION_STATE,
    PROMPT_HANDOFF,
    PROMPT_SHARED_STATE,
    PROMPT_STATE_UPDATE_RULE,
    PROMPT_REVIEWER_RULE,
    PROMPT_HANDOFF_RULE,
    PROMPT_TOOL_RULE,
    PROMPT_AGENT_METRICS,
)
from .config import DEFAULT_HISTORY_WINDOW


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

        rules = PROMPT_BASE_RULES
        if handoff_only:
            rules += PROMPT_HANDOFF_RULE
        else:
            rules += build_route_rule(self.active_agents)
            if not skip_tool_prompt:
                rules += PROMPT_TOOL_RULE
            if is_first_speaker:
                rules += PROMPT_DEBATE_RULE.format(marker=EXTEND_MARKER)
            else:
                rules += PROMPT_REVIEWER_RULE

        tools_prompt = build_tools_prompt() if not skip_tool_prompt else ""

        other_agents = [n for n in self.active_agents if n.lower() != agent.lower()]
        agents_list = ", ".join(n.upper() for n in other_agents) if other_agents else "nenhum"
        header_block = PROMPT_HEADER.format(
            agent=agent.upper(),
            user_name=self.user_name.upper(),
            agents=agents_list,
        )
        shared = shared_state or {}
        has_goal = "goal_canonical" in shared

        execution_context = ""
        if has_goal:
            current_step = shared.get("current_step") or "(não definido)"
            if current_step.lower() == "undefined":
                current_step = "(não definido)"
            execution_context = "\n\n".join([
                PROMPT_GOAL_LOCK.format(goal_canonical=shared["goal_canonical"]),
                PROMPT_STEP_LOCK.format(current_step=current_step),
                PROMPT_ACCEPTANCE_CRITERIA.format(acceptance_criteria=shared.get("acceptance_criteria", "{unspecified}")),
                PROMPT_SCOPE_CONTROL.format(
                    allowed_scope=shared.get("allowed_scope", "unspecified"),
                    non_goals=shared.get("non_goals", "unspecified"),
                ),
            ])
            rules += PROMPT_GOAL_EXECUTION_RULES
            rules += PROMPT_STATE_UPDATE_RULE

        session_block = PROMPT_SESSION_STATE.format(**self.session_state) if (self.session_state and primary) else ""
        context_block = PROMPT_CONTEXT.format(context=context) if context else ""
        handoff_block = PROMPT_HANDOFF.format(handoff=self._format_handoff(handoff, from_agent)) if handoff else ""
        request_index, request_block = self._build_request_block(history)
        fact_indexes, facts_block = self._build_facts_block(history)
        shared_state_block = ""
        if shared_state and not has_goal:
            _legacy_keys = {"goal", "decisions"}
            trimmed = {k: v for k, v in self._trim_shared_state(shared_state).items() if k not in _legacy_keys}
            if trimmed:
                state_lines = json.dumps(trimmed, ensure_ascii=False, indent=2)
                shared_state_block = PROMPT_SHARED_STATE.format(shared_state_json=state_lines)
        elif shared_state and has_goal and "completed_task_results" in shared_state:
            results = shared_state["completed_task_results"]
            if results:
                shared_state_block = f"TAREFAS CONCLUÍDAS:\n{results}"

        metrics_block = ""
        if self.metrics_tracker:
            feedback = self.metrics_tracker.generate_feedback(agent)
            if feedback:
                metrics_block = PROMPT_AGENT_METRICS.format(metrics=feedback)

        conversation = self._build_conversation_block(
            history,
            skip_indexes={idx for idx in [request_index, *fact_indexes] if idx is not None},
        )
        conversation_block = PROMPT_CONVERSATION.format(conversation=conversation)
        speaker_block = PROMPT_SPEAKER.format(agent=agent.upper())

        parts = [p for p in [
            header_block,
            execution_context,
            rules,
            tools_prompt, session_block, context_block, request_block, facts_block,
            shared_state_block, metrics_block, handoff_block, conversation_block, speaker_block,
        ] if p]

        # Keep explicit section boundaries so prompt blocks do not collapse together.
        full_prompt = "\n\n".join(parts)

        if debug:
            metrics = {
                "rules_chars": len(rules),
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
                    return index, PROMPT_REQUEST.format(request=content)
        return None, ""

    def _build_facts_block(self, history, max_items=4):
        """Monta facts block."""
        facts = []
        fact_indexes = []
        window_start = max(0, len(history) - self.history_window)
        for index in range(len(history) - 1, window_start - 1, -1):
            message = history[index]
            role = message.get("role")
            if role == "human":
                continue
            content = (message.get("content") or "").strip()
            if not content:
                continue
            facts.append(f"[{self._display_role(role)}] {content}")
            fact_indexes.append(index)
            if len(facts) >= max_items:
                break
        if not facts:
            return [], ""
        facts.reverse()
        fact_indexes.reverse()
        return fact_indexes, PROMPT_FACTS.format(facts="\n".join(facts))

    def _build_conversation_block(self, history, skip_indexes=None):
        """Monta conversa residual sem repetir blocos destacados acima."""
        skip_indexes = skip_indexes or set()
        window_start = max(0, len(history) - self.history_window)
        lines = []
        for index, message in enumerate(history[window_start:], start=window_start):
            if index in skip_indexes:
                continue
            content = (message.get("content") or "").strip()
            if not content:
                continue
            lines.append(f"[{self._display_role(message['role'])}]: {content}")
        return "\n".join(lines) if lines else "[sem itens residuais na conversa recente]"

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

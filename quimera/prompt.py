from .constants import (
    EXTEND_MARKER,
    PROMPT_HEADER,
    PROMPT_CONTEXT,
    PROMPT_CONVERSATION,
    PROMPT_SPEAKER,
    PROMPT_BASE_RULES,
    PROMPT_DEBATE_RULE,
    PROMPT_ROUTE_RULE,
    PROMPT_PARTICIPANTS,
    PROMPT_SESSION_STATE,
    PROMPT_HANDOFF,
)


class PromptBuilder:
    """Monta o prompt com contexto persistente e janela recente da conversa."""

    def __init__(self, context_manager, history_window=12, session_state=None, user_name=None):
        self.context_manager = context_manager
        self.history_window = history_window
        self.session_state = session_state or {}
        self.user_name = user_name or "Você"

    def build(self, agent, history, is_first_speaker=False, handoff=None):
        """Gera o prompt final enviado ao agente da vez."""
        context = self.context_manager.load()

        rules = PROMPT_BASE_RULES
        rules += PROMPT_ROUTE_RULE
        if is_first_speaker:
            rules += PROMPT_DEBATE_RULE.format(marker=EXTEND_MARKER)

        participants = f"- {self.user_name.upper()}\n- CLAUDE\n- CODEX\n"
        parts = [
            PROMPT_HEADER.format(agent=agent.upper(), participants=participants),
            rules,
        ]

        if self.session_state:
            parts.append(PROMPT_SESSION_STATE.format(**self.session_state))

        if context:
            parts.append(PROMPT_CONTEXT.format(context=context))

        if handoff:
            parts.append(PROMPT_HANDOFF.format(handoff=handoff))

        conversation = "\n".join(
            f"[{self._display_role(m['role'])}]: {m['content']}"
            for m in history[-self.history_window:]
        )
        parts.append(PROMPT_CONVERSATION.format(conversation=conversation))
        parts.append(PROMPT_SPEAKER.format(agent=agent.upper()))

        return "\n\n".join(parts)

    def _display_role(self, role):
        if role == "human":
            return self.user_name.upper()
        return role.upper()

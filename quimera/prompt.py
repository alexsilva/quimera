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

    def build(self, agent, history, is_first_speaker=False, handoff=None, debug=False, primary=True):
        """Gera o prompt final enviado ao agente da vez.

        primary=False omite session_state — adequado para agentes secundários que já
        têm o contexto da conversa e não precisam do estado de bootstrap da sessão.
        """
        context = self.context_manager.load()

        rules = PROMPT_BASE_RULES
        rules += PROMPT_ROUTE_RULE
        if is_first_speaker:
            rules += PROMPT_DEBATE_RULE.format(marker=EXTEND_MARKER)

        participants = f"- {self.user_name.upper()}\n- CLAUDE\n- CODEX\n"
        header_block = PROMPT_HEADER.format(agent=agent.upper(), participants=participants)
        session_block = PROMPT_SESSION_STATE.format(**self.session_state) if (self.session_state and primary) else ""
        context_block = PROMPT_CONTEXT.format(context=context) if context else ""
        handoff_block = PROMPT_HANDOFF.format(handoff=handoff) if handoff else ""

        conversation = "\n".join(
            f"[{self._display_role(m['role'])}]: {m['content']}"
            for m in history[-self.history_window:]
        )
        conversation_block = PROMPT_CONVERSATION.format(conversation=conversation)
        speaker_block = PROMPT_SPEAKER.format(agent=agent.upper())

        parts = [p for p in [
            header_block, rules, session_block, context_block,
            handoff_block, conversation_block, speaker_block,
        ] if p]

        full_prompt = "\n\n".join(parts)

        if debug:
            metrics = {
                "rules_chars": len(rules),
                "session_state_chars": len(session_block),
                "persistent_chars": len(context_block),
                "history_chars": len(conversation_block),
                "handoff_chars": len(handoff_block),
                "total_chars": len(full_prompt),
                "history_messages": len(history[-self.history_window:]),
                "primary": primary,
            }
            return full_prompt, metrics

        return full_prompt

    def _display_role(self, role):
        if role == "human":
            return self.user_name.upper()
        return role.upper()

import json

from .constants import (
    EXTEND_MARKER,
    PROMPT_HEADER,
    PROMPT_CONTEXT,
    PROMPT_CONVERSATION,
    PROMPT_SPEAKER,
    PROMPT_BASE_RULES,
    PROMPT_DEBATE_RULE,
    PROMPT_ROUTE_RULE,
    PROMPT_SESSION_STATE,
    PROMPT_HANDOFF,
    PROMPT_SHARED_STATE,
    PROMPT_STATE_UPDATE_RULE,
    PROMPT_REVIEWER_RULE,
    PROMPT_HANDOFF_RULE,
)
from .config import DEFAULT_HISTORY_WINDOW


class PromptBuilder:
    """Monta o prompt com contexto persistente e janela recente da conversa."""

    def __init__(self, context_manager, history_window=DEFAULT_HISTORY_WINDOW, session_state=None, user_name=None):
        self.context_manager = context_manager
        self.history_window = history_window
        self.session_state = session_state or {}
        self.user_name = user_name or "Você"

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
    ):
        """Gera o prompt final enviado ao agente da vez.

        primary=False omite session_state — adequado para agentes secundários que já
        têm o contexto da conversa e não precisam do estado de bootstrap da sessão.
        """
        context = self.context_manager.load()

        rules = PROMPT_BASE_RULES
        if handoff_only:
            rules += PROMPT_HANDOFF_RULE
        else:
            rules += PROMPT_ROUTE_RULE
            rules += PROMPT_STATE_UPDATE_RULE
            if is_first_speaker:
                rules += PROMPT_DEBATE_RULE.format(marker=EXTEND_MARKER)
            else:
                rules += PROMPT_REVIEWER_RULE

        participants = f"- {self.user_name.upper()}\n- CLAUDE\n- CODEX\n"
        header_block = PROMPT_HEADER.format(agent=agent.upper(), participants=participants)
        session_block = PROMPT_SESSION_STATE.format(**self.session_state) if (self.session_state and primary) else ""
        context_block = PROMPT_CONTEXT.format(context=context) if context else ""
        handoff_block = PROMPT_HANDOFF.format(handoff=self._format_handoff(handoff)) if handoff else ""
        shared_state_block = ""
        if shared_state:
            state_lines = json.dumps(shared_state, ensure_ascii=False, indent=2)
            shared_state_block = PROMPT_SHARED_STATE.format(state=state_lines)

        conversation = "\n".join(
            f"[{self._display_role(m['role'])}]: {m['content']}"
            for m in history[-self.history_window:]
        )
        conversation_block = PROMPT_CONVERSATION.format(conversation=conversation)
        speaker_block = PROMPT_SPEAKER.format(agent=agent.upper())

        parts = [p for p in [
            header_block, rules, session_block, context_block,
            shared_state_block, handoff_block, conversation_block, speaker_block,
        ] if p]

        full_prompt = "\n\n".join(parts)

        if debug:
            metrics = {
                "rules_chars": len(rules),
                "session_state_chars": len(session_block),
                "persistent_chars": len(context_block),
                "shared_state_chars": len(shared_state_block),
                "history_chars": len(conversation_block),
                "handoff_chars": len(handoff_block),
                "total_chars": len(full_prompt),
                "history_messages": len(history[-self.history_window:]),
                "primary": primary,
            }
            return full_prompt, metrics

        return full_prompt

    def _format_handoff(self, handoff):
        if isinstance(handoff, dict):
            task = handoff.get("task", "").strip()
            context = handoff.get("context", "").strip()
            expected = handoff.get("expected", "").strip()
            return (
                f"TASK:\n{task}\n\n"
                f"CONTEXT:\n{context}\n\n"
                f"EXPECTED:\n{expected}"
            ).strip()
        return str(handoff).strip()

    def _display_role(self, role):
        if role == "human":
            return self.user_name.upper()
        return role.upper()

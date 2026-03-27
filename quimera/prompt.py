class PromptBuilder:
    """Monta o prompt com contexto persistente e janela recente da conversa."""

    def __init__(self, context_manager, history_window=12):
        self.context_manager = context_manager
        self.history_window = history_window

    def build(self, agent, history):
        """Gera o prompt final enviado ao agente da vez."""
        context = self.context_manager.load()
        base = f"""Você é {agent.upper()} em uma conversa com:
- HUMANO
- CLAUDE
- CODEX

REGRAS:
- Responda como em um chat
- Pode discordar
- Pode comentar respostas anteriores
- Seja direto
"""

        if context:
            base += f"\n\nCONTEXTO PERSISTENTE:\n{context}"

        base += "\n\nCONVERSA:"
        for message in history[-self.history_window:]:
            base += f"\n[{message['role'].upper()}]: {message['content']}"

        base += f"\n[{agent.upper()}]:"
        return base

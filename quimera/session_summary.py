"""Componentes de `quimera.session_summary`."""

def build_chain_summarizer(agent_client, agents):
    """Tenta cada agente em ordem; retorna o primeiro resultado bem-sucedido ou None."""
    def _ordered_agents(preferred_agent=None):
        if preferred_agent and preferred_agent in agents:
            return [preferred_agent, *[agent for agent in agents if agent != preferred_agent]]
        return list(agents)

    def _call(prompt, preferred_agent=None):
        for agent in _ordered_agents(preferred_agent):
            try:
                result = agent_client.call(agent, prompt)
            except Exception:
                result = None
            if result:
                return result
            agent_client.renderer.show_system(f"[memória] {agent} indisponível, tentando próximo...")
        return None
    return _call


class SessionSummarizer:
    """Consolida memória de sessão via resumidores configurados; retorna None se todos falharem."""

    def __init__(self, renderer, summarizer_call):
        """Inicializa uma instância de SessionSummarizer."""
        self.renderer = renderer
        self.summarizer_call = summarizer_call

    def summarize(self, history, existing_summary=None, preferred_agent=None):
        """Executa summarize."""
        if not history and not existing_summary:
            return None

        prompt = self._build_prompt(history, existing_summary)
        try:
            summary = self.summarizer_call(prompt, preferred_agent=preferred_agent)
        except Exception:
            summary = None
        if not summary:
            self.renderer.show_system("[memória] resumidores indisponíveis")
        return summary or None

    @staticmethod
    def _build_prompt(history, existing_summary=None):
        """Monta prompt."""
        sections = []
        if existing_summary:
            sections.append(f"RESUMO ANTERIOR:\n{existing_summary}")
        if history:
            conversation = "\n".join(
                f"[{message['role'].upper()}]: {message['content']}" for message in history
            )
            sections.append(f"NOVO TRECHO DA CONVERSA:\n{conversation}")

        return f"""Você é um assistente de memória. Consolide o material abaixo em um resumo estruturado em markdown.

O resumo deve conter:
- O que foi discutido (tópicos principais)
- Decisões tomadas (se houver)
- Pendências ou próximos passos (se houver)

Regras:
- Preserve informações relevantes do resumo anterior, se houver
- Incorpore apenas novidades importantes do novo trecho
- Entregue um resumo acumulado único, pronto para substituir o anterior

Seja conciso. Máximo 20 linhas. Não use emojis. Escreva em português.

MATERIAL:
{chr(10).join(sections)}

RESUMO:"""

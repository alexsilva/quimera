"""Componentes de `quimera.session_summary`."""
import inspect
import logging

from quimera.agents.capabilities import get_cancel_event, is_user_cancelled

logger = logging.getLogger(__name__)


def _is_cancelled(agent_client) -> bool:
    """Indica se houve cancelamento cooperativo do resumo."""
    if is_user_cancelled(agent_client):
        return True
    cancel_event = get_cancel_event(agent_client)
    return bool(cancel_event is not None and cancel_event.is_set())


def build_chain_summarizer(agent_client, agents_or_fn):
    """Tenta cada agente em ordem; retorna o primeiro resultado bem-sucedido ou None.

    ``agents_or_fn`` pode ser uma lista estática ou um callable que retorna a lista
    no momento da chamada (útil para refletir o pool atual após remoções).
    """

    def _ordered_agents(preferred_agent=None, fallback=True):
        agents = agents_or_fn() if callable(agents_or_fn) else agents_or_fn
        if preferred_agent and preferred_agent in agents:
            if not fallback:
                return [preferred_agent]
            return [preferred_agent, *[agent for agent in agents if agent != preferred_agent]]
        if not fallback:
            return list(agents[:1])
        return list(agents)

    def _call(prompt, preferred_agent=None, fallback=True):
        _call.last_outcome = "unavailable"
        for agent in _ordered_agents(preferred_agent, fallback=fallback):
            if _is_cancelled(agent_client):
                _call.last_outcome = "cancelled"
                return None
            try:
                result = agent_client.call(agent, prompt, silent=True, allow_tools=False)
            except Exception:
                result = None
            if result:
                _call.last_outcome = "success"
                return result
            if _is_cancelled(agent_client):
                _call.last_outcome = "cancelled"
                return None
        return None

    _call.last_outcome = None
    return _call


class SessionSummarizer:
    """Consolida memória de sessão via resumidores configurados; retorna None se todos falharem."""

    def __init__(self, renderer, summarizer_call):
        """Inicializa uma instância de SessionSummarizer."""
        self.renderer = renderer
        self.summarizer_call = summarizer_call

    def summarize(self, history, existing_summary=None, preferred_agent=None, fallback=True):
        """Executa summarize."""
        if not history and not existing_summary:
            return None

        if self.summarizer_call is None:
            message = "Resumo não gerado: nenhum agente disponível."
            logger.info("[memória] %s", message)
            self.renderer.show_notification(message, severity="warning")
            return None

        prompt = self._build_prompt(history, existing_summary)
        try:
            kwargs = {"preferred_agent": preferred_agent}
            try:
                signature = inspect.signature(self.summarizer_call)
                params = signature.parameters.values()
                accepts_fallback = any(
                    param.kind is inspect.Parameter.VAR_KEYWORD or param.name == "fallback"
                    for param in params
                )
            except (TypeError, ValueError):
                accepts_fallback = True
            if accepts_fallback:
                kwargs["fallback"] = fallback
            summary = self.summarizer_call(prompt, **kwargs)
        except Exception:
            summary = None
        if not summary:
            outcome = getattr(self.summarizer_call, "last_outcome", None)
            if outcome != "cancelled":
                message = "Resumo não gerado: resumidores indisponíveis."
                logger.info("[memória] %s", message)
                self.renderer.show_notification(message, severity="warning")
        return summary or None

    @staticmethod
    def _build_prompt(history, existing_summary=None):
        """Monta prompt."""
        sections = []
        if existing_summary:
            sections.append(f"RESUMO ANTERIOR:\n{existing_summary}")
        if history:
            sections.append("NOVO TRECHO DA CONVERSA:")
            sections.extend(
                f"[{message['role'].upper()}]: {message['content']}" for message in history
            )

        return f"""Você é um assistente de memória. Consolide o material abaixo em um resumo estruturado em markdown.

O resumo deve conter:
- O que foi discutido (tópicos principais)
- Decisões tomadas (se houver)
- Pendências ou próximos passos (se houver)

Regras:
- Preserve informações relevantes do resumo anterior, se houver
- Incorpore apenas novidades importantes do novo trecho
- Entregue um resumo acumulado único, pronto para substituir o anterior
- Baseie-se exclusivamente no material abaixo
- Não use ferramentas, arquivos, shell, web ou memória externa

Seja conciso. Máximo 20 linhas. Não use emojis. Escreva em português.

MATERIAL:
{chr(10).join(sections)}

RESUMO:"""

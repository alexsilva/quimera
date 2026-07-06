"""Roteador de comandos e modos de execução para QuimeraApp."""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Callable

from ..modes import get_mode
from ..constants import MSG_DOUBLE_PREFIX

if TYPE_CHECKING:
    from .interfaces import IRenderer, IAgentPool

logger = logging.getLogger(__name__)


class CommandRouter:
    """Resolve roteamento de mensagens entre agentes e ativação de modos."""

    def __init__(
        self,
        agent_pool: IAgentPool,
        renderer: IRenderer,
        get_active_agent_profiles: Callable[[], list],
        set_execution_mode: Callable[[object], None],
        normalize_agent_name: Callable[[str], str],
        selected_agents: list[str],
        get_available_profiles: Callable[[], list],
    ):
        self.agent_pool = agent_pool
        self.renderer = renderer
        self.get_active_agent_profiles = get_active_agent_profiles
        self.set_execution_mode = set_execution_mode
        self.normalize_agent_name = normalize_agent_name
        self.selected_agents = selected_agents
        self.get_available_profiles = get_available_profiles

    def parse_routing(self, user_input: str) -> tuple[str | None, str | None, bool]:
        """Extrai o agente inicial e rejeita prefixos duplicados na mesma entrada.

        Detecta comandos de modo (/planning, /analysis, etc.) e os aplica antes
        do roteamento normal. Retorna (agent, message, explicit) onde explicit=True
        indica que o usuário usou /claude ou /codex explicitamente.
        """
        stripped = user_input.lstrip()
        lowered = stripped.lower()

        # Detecta comandos de modo: /planning, /analysis, /design, /review, /execute
        first_token = lowered.split()[0] if lowered.split() else ""
        mode = get_mode(first_token)
        if mode is not None:
            self.set_execution_mode(mode)
            rest = stripped[len(first_token):].lstrip()
            mode_message = (
                f"[modo] {mode.name} ativado — restrições anteriores removidas; "
                "ferramentas bloqueadas: nenhuma"
                if mode.name == "execute"
                else f"[modo] {mode.name} ativado — ferramentas bloqueadas: "
                     f"{', '.join(mode.blocked_tools) or 'nenhuma'}"
            )
            if rest:
                self.renderer.show_system(mode_message)
                return self.parse_routing(rest)
            self.renderer.show_system(mode_message)
            if not self.agent_pool:
                self.agent_pool.set([self.normalize_agent_name(a) for a in self.selected_agents])
            return None, "", False

        active_profiles = self.get_active_agent_profiles()
        for p in active_profiles:
            prefixes = [p.prefix, *(getattr(p, "aliases", None) or [])]
            agent = p.name
            for prefix in prefixes:
                if lowered == prefix:
                    return agent, "", True
                if lowered.startswith(f"{prefix} "):
                    message = stripped[len(prefix):].lstrip()
                    lowered_message = message.lower()
                    other_prefixes = []
                    for op in active_profiles:
                        if op.name == agent:
                            continue
                        other_prefixes.extend([op.prefix, *(getattr(op, "aliases", None) or [])])
                    if any(lowered_message == op or lowered_message.startswith(f"{op} ") for op in other_prefixes):
                        self.renderer.show_warning(MSG_DOUBLE_PREFIX)
                        return None, None, False
                    return agent, message, True

        if not self.agent_pool:
            logger.warning("no active agents, resetting to default")
            logger.debug("selected_agents=%r", self.selected_agents)
            logger.debug("available=%r", self.get_available_profiles())
            self.agent_pool.set(self.selected_agents or [p.name for p in self.get_available_profiles()])
            logger.debug("after fallback active_agents=%r", self.agent_pool.agents)
            if not self.agent_pool:
                raise RuntimeError("No agents available")

        orchestrator = getattr(self.agent_pool, "orchestrator_agent", None)
        if orchestrator and orchestrator in self.agent_pool.agents:
            others = [a for a in self.agent_pool.agents if a != orchestrator]
            agent_list = '\n'.join(f'  - {a}' for a in others) if others else '  (nenhum)'
            orq_prefix = (
                f"# Modo Orquestrador\n\n"
                f"┌─ Orquestrador {'─' * 20}┐\n"
                f"│ agente  │ {orchestrator:<20s}│\n"
                f"│ ação    │ orquestrar delegações  │\n"
                f"│ agentes │ {len(others):<2d} disponíveis{' ' * 10}│\n"
                f"└{'─' * 40}┘\n\n"
                f"## Agentes disponíveis\n{agent_list}\n\n"
                f"## Fluxo obrigatório\n"
                f"1. **Analise** o pedido e decida qual(is) agente(s) melhor resolve(m) a tarefa.\n"
                f"2. Use `delegate` para atribuir a execução ao agente escolhido.\n"
                f"3. **Revise** o trabalho recebido — verifique erros ou omissões.\n"
                f"4. **Sintetize** o resultado final com sua própria redação. Não repasse resposta bruta.\n"
                f"5. Se incorreto, delegue novamente com instruções mais precisas.\n\n"
                f"## Pedido\n{user_input}"
            )
            return orchestrator, orq_prefix, True

        return self.agent_pool.primary, user_input, False

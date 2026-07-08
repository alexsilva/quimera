"""Roteador de comandos e modos de execução para QuimeraApp."""
from __future__ import annotations
from dataclasses import dataclass
import logging
from typing import Iterator
from typing import TYPE_CHECKING, Callable

from ..modes import get_mode
from ..constants import MSG_DOUBLE_PREFIX

if TYPE_CHECKING:
    from .interfaces import IRenderer, IAgentPool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoutingDecision:
    """Resultado explícito do roteamento de uma entrada humana.

    Mantém compatibilidade com o contrato antigo de tupla por meio de
    ``__iter__``: chamadas existentes ainda podem fazer
    ``agent, message, explicit = parse_routing(...)``.
    """

    agent: str | None
    message: str | None
    explicit: bool
    source: str

    def __iter__(self) -> Iterator[object]:
        yield self.agent
        yield self.message
        yield self.explicit

    def as_tuple(self) -> tuple[str | None, str | None, bool]:
        return self.agent, self.message, self.explicit

    @classmethod
    def coerce(cls, value: "RoutingDecision | tuple[str | None, str | None, bool]") -> "RoutingDecision":
        """Normaliza resultado de roteamento antigo ou novo para RoutingDecision."""
        if isinstance(value, cls):
            return value
        agent, message, explicit = value
        return cls(agent, message, bool(explicit), source="legacy_tuple")


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

    def parse_routing(self, user_input: str) -> RoutingDecision:
        """Extrai o agente inicial e rejeita prefixos duplicados na mesma entrada.

        Detecta comandos de modo (/planning, /analysis, etc.) e os aplica antes
        do roteamento normal. Retorna RoutingDecision, compatível com unpacking
        como (agent, message, explicit). ``explicit=True`` indica prefixo de
        agente usado diretamente pelo usuário, como /claude ou /codex.
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
            return RoutingDecision(None, "", False, source="mode_only")

        active_profiles = self.get_active_agent_profiles()
        for p in active_profiles:
            prefixes = [p.prefix, *(getattr(p, "aliases", None) or [])]
            agent = p.name
            for prefix in prefixes:
                if lowered == prefix:
                    return RoutingDecision(agent, "", True, source="agent_prefix")
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
                        return RoutingDecision(None, None, False, source="double_prefix")
                    return RoutingDecision(agent, message, True, source="agent_prefix")

        if not self.agent_pool:
            logger.warning("no active agents, resetting to default")
            logger.debug("selected_agents=%r", self.selected_agents)
            logger.debug("available=%r", self.get_available_profiles())
            self.agent_pool.set(self.selected_agents or [p.name for p in self.get_available_profiles()])
            logger.debug("after fallback active_agents=%r", self.agent_pool.agents)
            if not self.agent_pool:
                raise RuntimeError("No agents available")

        # Com orquestrador ativo, todo input não-prefixado passa por ele. A instrução
        # de comportamento (analisar → delegar → revisar → sintetizar) é injetada no
        # prompt pelo PromptBuilder via bloco <!-- IF:is_orchestrator -->; aqui só
        # roteamos o pedido cru, sem duplicar o contrato de orquestração.
        orchestrator = getattr(self.agent_pool, "orchestrator_agent", None)
        if orchestrator and orchestrator in self.agent_pool.agents:
            return RoutingDecision(orchestrator, user_input, False, source="orchestrator")

        return RoutingDecision(self.agent_pool.primary, user_input, False, source="primary")

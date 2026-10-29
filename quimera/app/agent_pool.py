"""Pool thread-safe para gestão dos agentes ativos."""
from __future__ import annotations

import threading
import time
from collections.abc import MutableSequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SuspendedAgent:
    """Agente do pool indisponível por prazo, com o motivo e o tempo restante."""

    name: str
    reason: str
    remaining_seconds: float


class AgentPool:
    """Encapsula a lista ordenada de agentes ativos.

    Um agente pode ficar *suspenso* por um prazo: continua membro do pool
    (prefixo explícito reconhecido, executor vivo, listado em ``/agents``), mas
    sai da rotação do chat, do failover, do roteamento de tasks e da delegação
    até o prazo vencer ou ``resume`` ser chamado. Quando todos os membros estão
    suspensos, a rotação devolve o que volta primeiro, para sondar se ele já se
    recuperou.
    """

    def __init__(self, agents: list[str], *, clock=time.monotonic):
        self._lock = threading.Lock()
        self._agents = list(agents)
        # nome -> (instante em que volta, motivo), na escala de ``clock``.
        self._suspended: dict[str, tuple[float, str]] = {}
        self._clock = clock
        self._frozen_agent: str | None = None
        self._orchestrator_agent: str | None = None
        self._routing_listener = None

    def set_routing_listener(self, callback) -> None:
        """Registra callback(frozen, orchestrator) chamado após mudanças de roteamento."""
        self._routing_listener = callback

    def _notify_routing(self) -> None:
        # Fora do lock: o listener pode fazer IO (persistência em disco).
        listener = self._routing_listener
        if not callable(listener):
            return
        with self._lock:
            frozen, orchestrator = self._frozen_agent, self._orchestrator_agent
        listener(frozen, orchestrator)

    @property
    def agents(self) -> list[str]:
        with self._lock:
            return list(self._agents)

    # ── Disponibilidade ────────────────────────────────────────────────

    def _available_locked(self) -> list[str]:
        """Membros fora de suspensão; descarta suspensões já vencidas."""
        now = self._clock()
        for name in [n for n, (until, _) in self._suspended.items() if until <= now]:
            del self._suspended[name]
        return [agent for agent in self._agents if agent not in self._suspended]

    def _pick_locked(self) -> tuple[str | None, int | None]:
        """Agente que responde agora e o índice dele para rotação (None = não rotaciona)."""
        available = self._available_locked()
        if self._frozen_agent is not None and self._frozen_agent in available:
            return self._frozen_agent, None
        if available:
            return available[0], self._agents.index(available[0])
        if not self._agents:
            return None, None
        # Todos suspensos: sonda quem volta primeiro.
        return min(self._agents, key=lambda agent: self._suspended[agent][0]), None

    def available_agents(self) -> list[str]:
        """Membros que podem receber trabalho agora, na ordem do pool."""
        with self._lock:
            return self._available_locked()

    def suspend(self, name: str, *, seconds: float, reason: str) -> None:
        """Deixa ``name`` indisponível por ``seconds``; ele continua membro do pool."""
        with self._lock:
            if name not in self._agents:
                raise ValueError(f"Agente {name} não está no pool")
            self._suspended[name] = (self._clock() + max(0.0, seconds), reason)

    def resume(self, name: str) -> bool:
        """Encerra a suspensão de ``name``; True se havia uma em vigor."""
        with self._lock:
            return self._suspended.pop(name, None) is not None

    def suspensions(self) -> list[SuspendedAgent]:
        """Suspensões em vigor, na ordem do pool."""
        with self._lock:
            self._available_locked()
            now = self._clock()
            return [
                SuspendedAgent(agent, self._suspended[agent][1], self._suspended[agent][0] - now)
                for agent in self._agents
                if agent in self._suspended
            ]

    # ── Rotação ────────────────────────────────────────────────────────

    @property
    def primary(self) -> str | None:
        with self._lock:
            return self._pick_locked()[0]

    def take_primary(self) -> str | None:
        """Retorna o agente atual e avança o round-robin de forma atômica."""
        with self._lock:
            chosen, index = self._pick_locked()
            if index is not None and len(self._agents) > 1:
                self._agents = self._agents[:index] + self._agents[index + 1:] + [self._agents[index]]
            return chosen

    def freeze(self, agent_name: str) -> None:
        """Congela a rotação: take_primary() sempre retorna este agente."""
        with self._lock:
            if agent_name not in self._agents:
                raise ValueError(f"Agente {agent_name} não está no pool")
            self._frozen_agent = agent_name
            self._orchestrator_agent = None
        self._notify_routing()

    @property
    def orchestrator_agent(self) -> str | None:
        with self._lock:
            return self._orchestrator_agent

    def list_agents(self) -> list[str]:
        with self._lock:
            return list(dict.fromkeys(self._agents))

    def get_orchestrator(self) -> str | None:
        with self._lock:
            return self._orchestrator_agent

    def set_orchestrator(self, agent_name: str) -> None:
        """Define um agente como orquestrador e congela a rotação para ele."""
        with self._lock:
            if agent_name not in self._agents:
                raise ValueError(f"Agente {agent_name} não está no pool")
            self._orchestrator_agent = agent_name
            self._frozen_agent = agent_name
        self._notify_routing()

    def unfreeze(self) -> None:
        """Descongela: take_primary() volta a rotacionar. Também limpa o orquestrador."""
        with self._lock:
            self._frozen_agent = None
            self._orchestrator_agent = None
        self._notify_routing()

    @property
    def frozen_agent(self) -> str | None:
        with self._lock:
            return self._frozen_agent

    def add(self, name: str) -> None:
        with self._lock:
            if name not in self._agents:
                self._agents.append(name)

    def remove(self, name: str) -> None:
        cleared = False
        with self._lock:
            self._suspended.pop(name, None)
            if name in self._agents:
                self._agents.remove(name)
                if self._frozen_agent == name:
                    self._frozen_agent = None
                    self._orchestrator_agent = None
                    cleared = True
        if cleared:
            self._notify_routing()

    def set(self, agents: list[str]) -> None:
        cleared = False
        with self._lock:
            self._agents = list(agents)
            self._suspended = {n: s for n, s in self._suspended.items() if n in self._agents}
            if self._frozen_agent is not None and self._frozen_agent not in self._agents:
                self._frozen_agent = None
                self._orchestrator_agent = None
                cleared = True
        if cleared:
            self._notify_routing()

    def rotate(self) -> None:
        with self._lock:
            if len(self._agents) > 1:
                self._agents = self._agents[1:] + self._agents[:1]

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._agents

    def __iter__(self):
        with self._lock:
            return iter(list(self._agents))

    def __len__(self) -> int:
        with self._lock:
            return len(self._agents)

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._agents)


class AgentPoolView(MutableSequence[str]):
    """Visão compatível com lista sobre um ``AgentPool``."""

    def __init__(self, pool: AgentPool):
        self._pool = pool

    def __getitem__(self, index):
        return self._pool.agents[index]

    def __setitem__(self, index, value) -> None:
        agents = self._pool.agents
        agents[index] = value
        self._pool.set(agents)

    def __delitem__(self, index) -> None:
        agents = self._pool.agents
        del agents[index]
        self._pool.set(agents)

    def insert(self, index: int, value: str) -> None:
        agents = self._pool.agents
        agents.insert(index, value)
        self._pool.set(agents)

    def __len__(self) -> int:
        return len(self._pool)

    def __repr__(self) -> str:
        return repr(self._pool.agents)

    def __eq__(self, other) -> bool:
        if isinstance(other, AgentPoolView):
            return self._pool.agents == other._pool.agents
        return self._pool.agents == other

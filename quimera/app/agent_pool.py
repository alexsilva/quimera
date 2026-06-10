"""Pool thread-safe para gestão dos agentes ativos."""
from __future__ import annotations

import threading
from collections.abc import MutableSequence


class AgentPool:
    """Encapsula a lista ordenada de agentes ativos."""

    def __init__(self, agents: list[str]):
        self._lock = threading.Lock()
        self._agents = list(agents)
        self._frozen_agent: str | None = None

    @property
    def agents(self) -> list[str]:
        with self._lock:
            return list(self._agents)

    @property
    def primary(self) -> str | None:
        with self._lock:
            if self._frozen_agent is not None and self._frozen_agent in self._agents:
                return self._frozen_agent
            return self._agents[0] if self._agents else None

    def take_primary(self) -> str | None:
        """Retorna o agente atual e avança o round-robin de forma atômica."""
        with self._lock:
            if not self._agents:
                return None
            if self._frozen_agent is not None and self._frozen_agent in self._agents:
                return self._frozen_agent
            primary = self._agents[0]
            if len(self._agents) > 1:
                self._agents = self._agents[1:] + self._agents[:1]
            return primary

    def freeze(self, agent_name: str) -> None:
        """Congela a rotação: take_primary() sempre retorna este agente."""
        with self._lock:
            if agent_name not in self._agents:
                raise ValueError(f"Agente {agent_name} não está no pool")
            self._frozen_agent = agent_name

    def unfreeze(self) -> None:
        """Descongela: take_primary() volta a rotacionar."""
        with self._lock:
            self._frozen_agent = None

    @property
    def frozen_agent(self) -> str | None:
        with self._lock:
            return self._frozen_agent

    def add(self, name: str) -> None:
        with self._lock:
            if name not in self._agents:
                self._agents.append(name)

    def remove(self, name: str) -> None:
        with self._lock:
            if name in self._agents:
                self._agents.remove(name)

    def set(self, agents: list[str]) -> None:
        with self._lock:
            self._agents = list(agents)

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

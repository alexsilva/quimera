"""Política de failover e elegibilidade de review para tasks."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Protocol

from .planning import can_execute_task
from .repository import TaskRepository


class _TaskProfileProto(Protocol):
    """Interface mínima de profile usada pela política de failover."""

    supports_task_execution: bool
    name: str
    prefix: str
    aliases: Sequence[str]


class _TaskRepositoryProto(Protocol):
    """Interface mínima de persistência usada pela política de failover."""

    def can_reassign_task(self, task_id: int, candidate_agents: list[str]) -> bool:
        """Retorna True quando a task pode ser reatribuída."""

    def can_review_task(self, task_id: int, candidate_agents: list[str]) -> bool:
        """Retorna True quando algum candidato ainda pode revisar a task."""


class TaskFailoverPolicy:
    """Decide elegibilidade de review e possibilidade de failover.

    Quem ainda pode executar ou revisar uma task é decidido sobre os membros
    do pool (``active_agents``), inclusive os suspensos: um agente sem cota
    volta quando o prazo vence, e contar só os disponíveis no momento fazia um
    429 de segundos falhar a task. A disponibilidade (``available_agents``)
    decide só quem reivindica agora e se quem falhou estava indisponível.
    """

    def __init__(
        self,
        active_agents: list[str] | None | Callable[[], list[str] | None],
        get_agent_profile: Callable[[str], _TaskProfileProto | None],
        repository: TaskRepository | _TaskRepositoryProto,
        available_agents: Callable[[], list[str] | None] | None = None,
    ) -> None:
        if callable(active_agents):
            self._get_active_agents = active_agents
        else:
            self._get_active_agents = lambda: active_agents or []
        self._get_available_agents = available_agents or self._get_active_agents
        self.get_agent_profile = get_agent_profile
        self.repository = repository

    @property
    def active_agents(self) -> list[str]:
        """Retorna a lista atual de agentes ativos (membros do pool)."""
        return list(self._get_active_agents() or [])

    def is_available(self, agent_name: str) -> bool:
        """Membro do pool fora de suspensão (pode reivindicar trabalho agora)."""
        return any(
            self._same_agent_identity(agent_name, available)
            for available in self._get_available_agents() or []
        )

    @staticmethod
    def _normalize_agent_name(agent_name: str | None) -> str:
        """Normaliza identificador de agente para comparações internas."""
        if agent_name is None:
            return ""
        return str(agent_name).strip().lower().lstrip("/")

    @classmethod
    def _profile_lookup_keys(cls, profile: _TaskProfileProto, fallback_agent_name: str | None = None) -> list[str]:
        """Retorna chaves aceitas para resolver um profile por nome/prefixo/alias."""
        keys: list[str] = []
        for raw in (
            getattr(profile, "name", None),
            fallback_agent_name,
            getattr(profile, "prefix", None),
            *(getattr(profile, "aliases", None) or ()),
        ):
            normalized = cls._normalize_agent_name(raw)
            if normalized and normalized not in keys:
                keys.append(normalized)
        return keys

    @classmethod
    def _profile_canonical_name(
        cls,
        profile: _TaskProfileProto | None,
        fallback_agent_name: str | None = None,
    ) -> str:
        """Resolve chave canônica de um profile, tolerando stubs mínimos em testes."""
        if profile is None:
            return cls._normalize_agent_name(fallback_agent_name)
        return cls._normalize_agent_name(getattr(profile, "name", None) or fallback_agent_name)

    def _resolve_profile_from_agent_name(self, agent_name: str) -> _TaskProfileProto | None:
        """Resolve profile por nome canônico, prefixo ou alias."""
        profile = self.get_agent_profile(agent_name)
        if profile is not None and can_execute_task(profile):
            return profile

        normalized = self._normalize_agent_name(agent_name)
        if not normalized:
            return None
        for candidate_name in self.active_agents:
            candidate = self.get_agent_profile(candidate_name)
            if candidate is None or not can_execute_task(candidate):
                continue
            if normalized in self._profile_lookup_keys(candidate, fallback_agent_name=candidate_name):
                return candidate
        return None

    def _same_agent_identity(self, left: str | None, right: str | None) -> bool:
        """Compara dois identificadores de agente levando em conta aliases."""
        left_norm = self._normalize_agent_name(left)
        right_norm = self._normalize_agent_name(right)
        if not left_norm or not right_norm:
            return False
        if left_norm == right_norm:
            return True
        left_profile = self._resolve_profile_from_agent_name(left_norm)
        right_profile = self._resolve_profile_from_agent_name(right_norm)
        return self._profile_canonical_name(left_profile, left_norm) == self._profile_canonical_name(right_profile, right_norm)

    def is_active_agent(self, agent_name: str) -> bool:
        """Retorna se o agente está no pool ativo (tolerando prefixo e aliases)."""
        return any(self._same_agent_identity(agent_name, active) for active in self.active_agents)

    def is_operational_review_agent(self, agent_name: str) -> bool:
        """Retorna se o agente está ativo e apto a executar task/review."""
        if not self.is_active_agent(agent_name):
            return False
        return self._resolve_profile_from_agent_name(agent_name) is not None

    def review_agents_for(
        self,
        executor_agent: str | None = None,
        exclude_agents: Iterable[str] | None = None,
    ) -> list[str]:
        """Lista revisores elegíveis excluindo executor e bloqueados."""
        excluded = set(exclude_agents or ())
        eligible = []
        for candidate in self.active_agents:
            if executor_agent is not None and self._same_agent_identity(candidate, executor_agent):
                continue
            if any(self._same_agent_identity(candidate, blocked) for blocked in excluded):
                continue
            if self.is_operational_review_agent(candidate):
                eligible.append(candidate)
        return eligible

    def can_failover(self, task_id: int, failed_agent: str) -> bool:
        """Verifica se ainda há candidato para assumir a task."""
        candidate_agents = [
            agent_name
            for agent_name in self.active_agents
            if not self._same_agent_identity(agent_name, failed_agent)
        ]
        return self.repository.can_reassign_task(task_id, candidate_agents)

    def can_review(
        self,
        task_id: int,
        executor_agent: str | None,
        *,
        exclude_agents: Iterable[str] = (),
    ) -> bool:
        """Algum membro do pool, fora o executor, ``exclude_agents`` e quem já falhou
        ao revisar a entrega, pode revisá-la."""
        return self.repository.can_review_task(
            task_id,
            self.review_agents_for(executor_agent=executor_agent, exclude_agents=exclude_agents),
        )

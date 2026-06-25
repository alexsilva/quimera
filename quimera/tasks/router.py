"""Roteamento de tasks com scoring e balanceamento de carga."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Protocol

from ..runtime.models import TaskRecord
from .planning import can_execute_task, choose_best_agent, score_profile_for_task
from .repository import TaskRepository


class _TaskProfileProto(Protocol):
    """Interface mínima de profile usada no roteamento de tasks."""

    name: str
    supports_task_execution: bool
    avoid_task_types: Sequence[str]
    driver: str
    prefix: str
    aliases: Sequence[str]


class _TaskRepositoryProto(Protocol):
    """Interface mínima de persistência usada pelo roteador."""

    def list_tasks(self, filt: dict | None = None) -> list[TaskRecord]:
        """Lista tasks com filtros opcionais."""


class TaskRouter:
    """Resolve seleção de agentes para execução de tasks."""

    def __init__(
        self,
        active_agents: list[str] | None,
        get_agent_profile: Callable[[str], _TaskProfileProto | None],
        get_available_profiles: Callable[[], list[_TaskProfileProto]],
        repository: TaskRepository | _TaskRepositoryProto,
    ) -> None:
        self.active_agents = list(active_agents or [])
        self.get_agent_profile = get_agent_profile
        self.get_available_profiles = get_available_profiles
        self.repository = repository

    @staticmethod
    def _normalize_agent_name(agent_name: str | None) -> str:
        """Normaliza identificador de agente para comparações internas."""
        if agent_name is None:
            return ""
        return str(agent_name).strip().lower().lstrip("/")

    @classmethod
    def _profile_lookup_keys(cls, profile: _TaskProfileProto) -> list[str]:
        """Retorna chaves aceitas para resolver um profile por nome/prefixo/alias."""
        keys: list[str] = []
        for raw in (profile.name, getattr(profile, "prefix", None), *(getattr(profile, "aliases", None) or ())):
            normalized = cls._normalize_agent_name(raw)
            if normalized and normalized not in keys:
                keys.append(normalized)
        return keys

    def _iter_routable_profiles(self) -> list[_TaskProfileProto]:
        """Lista profiles executáveis disponíveis para roteamento."""
        return [
            profile
            for profile in self.get_available_profiles()
            if profile is not None and can_execute_task(profile)
        ]

    def _resolve_profile_from_agent_name(
        self,
        agent_name: str,
        profiles: Iterable[_TaskProfileProto],
    ) -> _TaskProfileProto | None:
        """Resolve profile a partir de nome canônico, prefixo ou alias."""
        profile = self.get_agent_profile(agent_name)
        if profile is not None and can_execute_task(profile):
            return profile

        normalized = self._normalize_agent_name(agent_name)
        if not normalized:
            return None
        for candidate in profiles:
            if normalized in self._profile_lookup_keys(candidate):
                return candidate
        return None

    def get_task_routing_profiles(self) -> list[_TaskProfileProto]:
        """Retorna os profiles elegíveis para roteamento de tasks."""
        routable_profiles = self._iter_routable_profiles()

        if not self.active_agents or "*" in self.active_agents:
            return routable_profiles

        candidate_profiles: list[_TaskProfileProto] = []
        seen_profile_names: set[str] = set()
        for agent_name in self.active_agents:
            profile = self._resolve_profile_from_agent_name(agent_name, routable_profiles)
            if profile is None or profile.name in seen_profile_names:
                continue
            seen_profile_names.add(profile.name)
            candidate_profiles.append(profile)
        return candidate_profiles

    def count_agent_open_tasks(self, agent_name: str) -> int:
        """Conta quantas tasks abertas estão associadas ao agente."""
        return sum(
            len(self.repository.list_tasks({"assigned_to": agent_name, "status": status}))
            for status in ("pending", "in_progress")
        )

    def choose_agent_with_load_balance(self, task_type: str) -> str | None:
        """Seleciona o melhor agente para uma task considerando carga."""
        candidate_profiles = self.get_task_routing_profiles()
        if not candidate_profiles:
            return None

        scored = []
        for profile in candidate_profiles:
            base_score = score_profile_for_task(profile, task_type)
            load = self.count_agent_open_tasks(profile.name)
            effective_score = base_score - load
            scored.append((profile, base_score, load, effective_score))

        max_score = max(score for _, _, _, score in scored)
        if max_score <= -5:
            return choose_best_agent(task_type, candidate_profiles)

        top = [item for item in scored if item[3] == max_score]
        top.sort(key=lambda item: (item[2], -item[1], item[0].name))
        return top[0][0].name

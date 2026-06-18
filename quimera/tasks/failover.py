"""Política de failover e elegibilidade de review para tasks."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Protocol

from .planning import can_execute_task
from .repository import TaskRepository


class _TaskPluginProto(Protocol):
    """Interface mínima de plugin usada pela política de failover."""

    supports_task_execution: bool
    name: str
    prefix: str
    aliases: Sequence[str]


class _TaskRepositoryProto(Protocol):
    """Interface mínima de persistência usada pela política de failover."""

    def can_reassign_task(self, task_id: int, candidate_agents: list[str]) -> bool:
        """Retorna True quando a task pode ser reatribuída."""


class TaskFailoverPolicy:
    """Decide elegibilidade de review e possibilidade de failover."""

    def __init__(
        self,
        active_agents: list[str] | None | Callable[[], list[str] | None],
        get_agent_plugin: Callable[[str], _TaskPluginProto | None],
        repository: TaskRepository | _TaskRepositoryProto,
    ) -> None:
        if callable(active_agents):
            self._get_active_agents = active_agents
        else:
            self._get_active_agents = lambda: active_agents or []
        self.get_agent_plugin = get_agent_plugin
        self.repository = repository

    @property
    def active_agents(self) -> list[str]:
        """Retorna a lista atual de agentes ativos."""
        return list(self._get_active_agents() or [])

    @staticmethod
    def _normalize_agent_name(agent_name: str | None) -> str:
        """Normaliza identificador de agente para comparações internas."""
        if agent_name is None:
            return ""
        return str(agent_name).strip().lower().lstrip("/")

    @classmethod
    def _plugin_lookup_keys(cls, plugin: _TaskPluginProto, fallback_agent_name: str | None = None) -> list[str]:
        """Retorna chaves aceitas para resolver um plugin por nome/prefixo/alias."""
        keys: list[str] = []
        for raw in (
            getattr(plugin, "name", None),
            fallback_agent_name,
            getattr(plugin, "prefix", None),
            *(getattr(plugin, "aliases", None) or ()),
        ):
            normalized = cls._normalize_agent_name(raw)
            if normalized and normalized not in keys:
                keys.append(normalized)
        return keys

    @classmethod
    def _plugin_canonical_name(
        cls,
        plugin: _TaskPluginProto | None,
        fallback_agent_name: str | None = None,
    ) -> str:
        """Resolve chave canônica de um plugin, tolerando stubs mínimos em testes."""
        if plugin is None:
            return cls._normalize_agent_name(fallback_agent_name)
        return cls._normalize_agent_name(getattr(plugin, "name", None) or fallback_agent_name)

    def _resolve_plugin_from_agent_name(self, agent_name: str) -> _TaskPluginProto | None:
        """Resolve plugin por nome canônico, prefixo ou alias."""
        plugin = self.get_agent_plugin(agent_name)
        if plugin is not None and can_execute_task(plugin):
            return plugin

        normalized = self._normalize_agent_name(agent_name)
        if not normalized:
            return None
        for candidate_name in self.active_agents:
            candidate = self.get_agent_plugin(candidate_name)
            if candidate is None or not can_execute_task(candidate):
                continue
            if normalized in self._plugin_lookup_keys(candidate, fallback_agent_name=candidate_name):
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
        left_plugin = self._resolve_plugin_from_agent_name(left_norm)
        right_plugin = self._resolve_plugin_from_agent_name(right_norm)
        return self._plugin_canonical_name(left_plugin, left_norm) == self._plugin_canonical_name(right_plugin, right_norm)

    def is_operational_review_agent(self, agent_name: str) -> bool:
        """Retorna se o agente está ativo e apto a executar task/review."""
        if not any(self._same_agent_identity(agent_name, active) for active in self.active_agents):
            return False
        return self._resolve_plugin_from_agent_name(agent_name) is not None

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

    def has_review_failover(self, executor_agent: str | None, failed_reviewer: str) -> bool:
        """Indica se restam revisores válidos após falha."""
        return bool(self.review_agents_for(executor_agent=executor_agent, exclude_agents={failed_reviewer}))

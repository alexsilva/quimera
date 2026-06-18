"""Roteamento de tasks com scoring e balanceamento de carga."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Protocol

from ..runtime.models import TaskRecord
from .planning import can_execute_task, choose_best_agent, score_plugin_for_task
from .repository import TaskRepository


class _TaskPluginProto(Protocol):
    """Interface mínima de plugin usada no roteamento de tasks."""

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
        get_agent_plugin: Callable[[str], _TaskPluginProto | None],
        get_available_plugins: Callable[[], list[_TaskPluginProto]],
        repository: TaskRepository | _TaskRepositoryProto,
    ) -> None:
        self.active_agents = list(active_agents or [])
        self.get_agent_plugin = get_agent_plugin
        self.get_available_plugins = get_available_plugins
        self.repository = repository

    @staticmethod
    def _normalize_agent_name(agent_name: str | None) -> str:
        """Normaliza identificador de agente para comparações internas."""
        if agent_name is None:
            return ""
        return str(agent_name).strip().lower().lstrip("/")

    @classmethod
    def _plugin_lookup_keys(cls, plugin: _TaskPluginProto) -> list[str]:
        """Retorna chaves aceitas para resolver um plugin por nome/prefixo/alias."""
        keys: list[str] = []
        for raw in (plugin.name, getattr(plugin, "prefix", None), *(getattr(plugin, "aliases", None) or ())):
            normalized = cls._normalize_agent_name(raw)
            if normalized and normalized not in keys:
                keys.append(normalized)
        return keys

    def _iter_routable_plugins(self) -> list[_TaskPluginProto]:
        """Lista plugins executáveis disponíveis para roteamento."""
        return [
            plugin
            for plugin in self.get_available_plugins()
            if plugin is not None and can_execute_task(plugin)
        ]

    def _resolve_plugin_from_agent_name(
        self,
        agent_name: str,
        plugins: Iterable[_TaskPluginProto],
    ) -> _TaskPluginProto | None:
        """Resolve plugin a partir de nome canônico, prefixo ou alias."""
        plugin = self.get_agent_plugin(agent_name)
        if plugin is not None and can_execute_task(plugin):
            return plugin

        normalized = self._normalize_agent_name(agent_name)
        if not normalized:
            return None
        for candidate in plugins:
            if normalized in self._plugin_lookup_keys(candidate):
                return candidate
        return None

    def get_task_routing_plugins(self) -> list[_TaskPluginProto]:
        """Retorna os plugins elegíveis para roteamento de tasks."""
        routable_plugins = self._iter_routable_plugins()

        if not self.active_agents or "*" in self.active_agents:
            return routable_plugins

        candidate_plugins: list[_TaskPluginProto] = []
        seen_plugin_names: set[str] = set()
        for agent_name in self.active_agents:
            plugin = self._resolve_plugin_from_agent_name(agent_name, routable_plugins)
            if plugin is None or plugin.name in seen_plugin_names:
                continue
            seen_plugin_names.add(plugin.name)
            candidate_plugins.append(plugin)
        return candidate_plugins

    def count_agent_open_tasks(self, agent_name: str) -> int:
        """Conta quantas tasks abertas estão associadas ao agente."""
        return sum(
            len(self.repository.list_tasks({"assigned_to": agent_name, "status": status}))
            for status in ("pending", "in_progress")
        )

    def choose_agent_with_load_balance(self, task_type: str) -> str | None:
        """Seleciona o melhor agente para uma task considerando carga."""
        candidate_plugins = self.get_task_routing_plugins()
        if not candidate_plugins:
            return None

        scored = []
        for plugin in candidate_plugins:
            base_score = score_plugin_for_task(plugin, task_type)
            load = self.count_agent_open_tasks(plugin.name)
            effective_score = base_score - load
            scored.append((plugin, base_score, load, effective_score))

        max_score = max(score for _, _, _, score in scored)
        if max_score <= -5:
            return choose_best_agent(task_type, candidate_plugins)

        top = [item for item in scored if item[3] == max_score]
        top.sort(key=lambda item: (item[2], -item[1], item[0].name))
        return top[0][0].name

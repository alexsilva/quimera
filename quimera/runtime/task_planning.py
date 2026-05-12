"""Componentes de `quimera.runtime.task_planning`."""
from __future__ import annotations

import re
from typing import Iterable, Protocol, Sequence, runtime_checkable

from ..constants import TaskType


@runtime_checkable
class _TaskAgentProto(Protocol):
    """Interface mínima de plugin usada pelo planejamento de tasks."""

    name: str
    base_tier: int
    preferred_task_types: Sequence[str]
    avoid_task_types: Sequence[str]
    supports_code_editing: bool
    supports_long_context: bool
    supports_tools: bool
    capabilities: Sequence[str]
    supports_task_execution: bool
    tool_use_reliability: str


_TASK_PATTERNS: tuple[tuple[TaskType, tuple[str, ...]], ...] = (
    (TaskType.TEST_EXECUTION,
     ("execute os testes", "executar testes", "rode pytest", "rodar testes", "run tests", "pytest", "testes")),
    (TaskType.CODE_REVIEW,
     ("revise", "review", "analise esse arquivo", "code review", "revisar arquivo", "inspecione")),
    (TaskType.CODE_EDIT,
     ("corrija", "implemente", "edite", "refatore", "refatoração", "refactor", "ajuste", "altere", "modifique")),
    (TaskType.BUG_INVESTIGATION,
     ("investigue", "descubra por que", "erro", "falha", "bug", "quebrou", "não funciona")),
    (TaskType.ARCHITECTURE, ("arquitetura", "design", "protocolo", "estratégia", "modelagem")),
    (TaskType.DOCUMENTATION, ("documente", "readme", "explicar", "documentação", "docs")),
)


def normalize_task_description(text: str) -> str:
    """Normaliza task description."""
    return re.sub(r"\s+", " ", str(text or "").strip())


def classify_task_type(description: str) -> str:
    """Classifica task type."""
    normalized = normalize_task_description(description).lower()
    if not normalized:
        return TaskType.GENERAL
    for task_type, keywords in _TASK_PATTERNS:
        if any(keyword in normalized for keyword in keywords):
            return task_type
    return TaskType.GENERAL


CAPABILITY_BOOST = {
    "code_editing": {TaskType.CODE_EDIT: 4, TaskType.BUG_INVESTIGATION: 1},
    "bug_investigation": {TaskType.BUG_INVESTIGATION: 4, TaskType.CODE_EDIT: 1},
    "documentation": {TaskType.DOCUMENTATION: 4},
    "code_review": {TaskType.CODE_REVIEW: 4, TaskType.ARCHITECTURE: 1},
    "architecture": {TaskType.ARCHITECTURE: 4, TaskType.CODE_REVIEW: 1},
    "general_coding": {TaskType.CODE_EDIT: 3, TaskType.BUG_INVESTIGATION: 1},
    "planning": {TaskType.ARCHITECTURE: 2, TaskType.CODE_REVIEW: 1},
}

TOOL_RELIABILITY_SCORES = {
    "low": -4,
    "medium": 0,
    "high": 3,
}


def can_execute_task(plugin: _TaskAgentProto) -> bool:
    """Indica se pode execute task."""
    return getattr(plugin, "supports_task_execution", True)


def tool_reliability(plugin: _TaskAgentProto) -> str:
    """Retorna a confiabilidade declarada do agente para uso de ferramentas."""
    return str(getattr(plugin, "tool_use_reliability", "medium") or "medium").lower()


def score_plugin_for_task(plugin: _TaskAgentProto, task_type: str) -> int:
    """Executa score plugin for task."""
    score = 0
    score += (plugin.base_tier - 1) * 2

    if task_type in plugin.preferred_task_types:
        score += 5
    if task_type in plugin.avoid_task_types:
        score -= 5
    if task_type in {TaskType.CODE_EDIT, TaskType.BUG_INVESTIGATION,
                     TaskType.CODE_REVIEW} and plugin.supports_code_editing:
        score += 2
    if task_type in {TaskType.ARCHITECTURE, TaskType.CODE_REVIEW,
                     TaskType.DOCUMENTATION} and plugin.supports_long_context:
        score += 2
    if plugin.supports_tools and task_type in {TaskType.TEST_EXECUTION, TaskType.BUG_INVESTIGATION}:
        score += 1

    if task_type in {TaskType.TEST_EXECUTION, TaskType.BUG_INVESTIGATION}:
        score += TOOL_RELIABILITY_SCORES.get(tool_reliability(plugin), 0)

    # Penalty: for bug investigation tasks, penalize plugins without tooling
    if task_type == TaskType.BUG_INVESTIGATION and not plugin.supports_tools:
        score -= 3

    for cap in plugin.capabilities:
        cap_boost = CAPABILITY_BOOST.get(cap, {})
        score += cap_boost.get(task_type, 0)

    return score


def choose_best_agent(task_type: str, active_plugins: Iterable[_TaskAgentProto]) -> str | None:
    """Seleciona best agent."""
    plugins = [plugin for plugin in active_plugins if plugin is not None and can_execute_task(plugin)]
    if not plugins:
        return None

    best_plugin = None
    best_score = None
    for plugin in plugins:
        score = score_plugin_for_task(plugin, task_type)
        if best_plugin is None or score > best_score:
            best_plugin = plugin
            best_score = score

    if best_plugin is not None and best_score is not None and best_score > -5:
        return best_plugin.name

    compatible = [plugin for plugin in plugins if task_type not in plugin.avoid_task_types]
    if compatible:
        return compatible[0].name
    return plugins[0].name

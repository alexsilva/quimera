from __future__ import annotations

import re
from typing import Iterable

from ..plugins.base import AgentPlugin

TASK_TYPE_TEST_EXECUTION = "test_execution"
TASK_TYPE_CODE_REVIEW = "code_review"
TASK_TYPE_CODE_EDIT = "code_edit"
TASK_TYPE_BUG_INVESTIGATION = "bug_investigation"
TASK_TYPE_ARCHITECTURE = "architecture"
TASK_TYPE_DOCUMENTATION = "documentation"
TASK_TYPE_GENERAL = "general"

TASK_TYPES = (
    TASK_TYPE_TEST_EXECUTION,
    TASK_TYPE_CODE_REVIEW,
    TASK_TYPE_CODE_EDIT,
    TASK_TYPE_BUG_INVESTIGATION,
    TASK_TYPE_ARCHITECTURE,
    TASK_TYPE_DOCUMENTATION,
    TASK_TYPE_GENERAL,
)

_TASK_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (TASK_TYPE_TEST_EXECUTION, ("execute os testes", "executar testes", "rode pytest", "rodar testes", "run tests", "pytest", "testes")),
    (TASK_TYPE_CODE_REVIEW, ("revise", "review", "analise esse arquivo", "code review", "revisar arquivo", "inspecione")),
    (TASK_TYPE_CODE_EDIT, ("corrija", "implemente", "edite", "refatore", "ajuste", "altere", "modifique")),
    (TASK_TYPE_BUG_INVESTIGATION, ("investigue", "descubra por que", "erro", "falha", "bug", "quebrou", "não funciona")),
    (TASK_TYPE_ARCHITECTURE, ("arquitetura", "design", "protocolo", "estratégia", "modelagem")),
    (TASK_TYPE_DOCUMENTATION, ("documente", "readme", "explicar", "documentação", "docs")),
)


def normalize_task_description(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def classify_task_type(description: str) -> str:
    normalized = normalize_task_description(description).lower()
    if not normalized:
        return TASK_TYPE_GENERAL
    for task_type, keywords in _TASK_PATTERNS:
        if any(keyword in normalized for keyword in keywords):
            return task_type
    return TASK_TYPE_GENERAL


def score_plugin_for_task(plugin: AgentPlugin, task_type: str) -> int:
    score = 0
    if task_type in plugin.preferred_task_types:
        score += 5
    if task_type in plugin.avoid_task_types:
        score -= 5
    if task_type in {TASK_TYPE_CODE_EDIT, TASK_TYPE_TEST_EXECUTION, TASK_TYPE_BUG_INVESTIGATION} and plugin.supports_code_editing:
        score += 2
    if task_type in {TASK_TYPE_ARCHITECTURE, TASK_TYPE_CODE_REVIEW, TASK_TYPE_DOCUMENTATION} and plugin.supports_long_context:
        score += 2
    if plugin.supports_tools and task_type in {TASK_TYPE_TEST_EXECUTION, TASK_TYPE_BUG_INVESTIGATION}:
        score += 1
    return score


def choose_best_agent(task_type: str, active_plugins: Iterable[AgentPlugin]) -> str | None:
    plugins = list(active_plugins)
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

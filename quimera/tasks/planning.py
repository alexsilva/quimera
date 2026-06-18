"""Componentes de `quimera.tasks.planning`."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Literal, Protocol, Sequence, runtime_checkable

from ..constants import TaskType


ComplexityLevel = Literal["low", "medium", "high"]
RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class TaskClassification:
    """Classificação enriquecida para roteamento e políticas de execução."""

    task_type: TaskType
    complexity: ComplexityLevel
    requires_tools: bool
    requires_code_editing: bool
    risk_level: RiskLevel


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


@runtime_checkable
class TaskClassifier(Protocol):
    """Interface para classificadores de task plugáveis."""

    def classify(self, description: str) -> TaskClassification:
        ...


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
_TASK_PRECEDENCE: tuple[TaskType, ...] = (
    TaskType.CODE_EDIT,
    TaskType.BUG_INVESTIGATION,
    TaskType.TEST_EXECUTION,
    TaskType.CODE_REVIEW,
    TaskType.ARCHITECTURE,
    TaskType.DOCUMENTATION,
)

_COMPLEXITY_HIGH_HINTS: tuple[str, ...] = (
    "arquitetura",
    "redesen",
    "migr",
    "protocolo",
    "sistema inteiro",
)
_COMPLEXITY_MEDIUM_HINTS: tuple[str, ...] = (
    "integra",
    "fluxo",
    "pipeline",
    "regress",
    "cobertura",
)
_RISK_HIGH_HINTS: tuple[str, ...] = (
    "produção",
    "prod",
    "deploy",
    "segurança",
    "credencial",
    "token",
    "database",
    "sqlite",
)
_TOOLS_HINTS: tuple[str, ...] = (
    "pytest",
    "teste",
    "rodar",
    "executar",
    "comando",
    "shell",
    "terminal",
    "log",
    "traceback",
)
_CODE_EDIT_HINTS: tuple[str, ...] = (
    "corrija",
    "implemente",
    "edite",
    "refatore",
    "altere",
    "modifique",
    "patch",
)
_MULTI_SCOPE_HINTS: tuple[str, ...] = (
    " e ",
    " além de ",
    "também",
    "depois",
    "em seguida",
)


def normalize_task_description(text: str) -> str:
    """Normaliza task description."""
    return re.sub(r"\s+", " ", str(text or "").strip())


def _classify_task_type_from_text(normalized: str) -> TaskType:
    """Classifica o tipo principal da task com heurística de palavras-chave."""
    if not normalized:
        return TaskType.GENERAL
    matched: list[TaskType] = []
    for task_type, keywords in _TASK_PATTERNS:
        if any(keyword in normalized for keyword in keywords):
            matched.append(task_type)
    if not matched:
        return TaskType.GENERAL
    if len(matched) == 1:
        return matched[0]
    for preferred in _TASK_PRECEDENCE:
        if preferred in matched:
            return preferred
    return TaskType.GENERAL


def _infer_complexity(normalized: str, task_type: TaskType) -> ComplexityLevel:
    word_count = len(normalized.split())
    has_multi_scope = any(token in normalized for token in _MULTI_SCOPE_HINTS)
    if any(token in normalized for token in _COMPLEXITY_HIGH_HINTS) or word_count >= 22:
        return "high"
    if task_type in {TaskType.ARCHITECTURE, TaskType.BUG_INVESTIGATION} and word_count >= 12 and has_multi_scope:
        return "high"
    if task_type in {TaskType.TEST_EXECUTION, TaskType.BUG_INVESTIGATION, TaskType.CODE_REVIEW}:
        return "medium"
    if has_multi_scope or word_count >= 10 or any(token in normalized for token in _COMPLEXITY_MEDIUM_HINTS):
        return "medium"
    return "low"


def _infer_requires_tools(normalized: str, task_type: TaskType) -> bool:
    if task_type in {TaskType.TEST_EXECUTION, TaskType.BUG_INVESTIGATION}:
        return True
    return any(token in normalized for token in _TOOLS_HINTS)


def _infer_requires_code_editing(normalized: str, task_type: TaskType) -> bool:
    if task_type == TaskType.CODE_EDIT:
        return True
    return any(token in normalized for token in _CODE_EDIT_HINTS)


def _infer_risk_level(normalized: str, task_type: TaskType) -> RiskLevel:
    if any(token in normalized for token in _RISK_HIGH_HINTS):
        return "high"
    if task_type in {
        TaskType.BUG_INVESTIGATION,
        TaskType.TEST_EXECUTION,
        TaskType.CODE_EDIT,
        TaskType.CODE_REVIEW,
        TaskType.ARCHITECTURE,
    }:
        return "medium"
    return "low"


class KeywordTaskClassifier:
    """Classificador padrão baseado em palavras-chave."""

    def classify(self, description: str) -> TaskClassification:
        normalized = normalize_task_description(description).lower()
        task_type = _classify_task_type_from_text(normalized)
        return TaskClassification(
            task_type=task_type,
            complexity=_infer_complexity(normalized, task_type),
            requires_tools=_infer_requires_tools(normalized, task_type),
            requires_code_editing=_infer_requires_code_editing(normalized, task_type),
            risk_level=_infer_risk_level(normalized, task_type),
        )


DEFAULT_TASK_CLASSIFIER = KeywordTaskClassifier()


def classify_task(description: str, classifier: TaskClassifier | None = None) -> TaskClassification:
    """Retorna classificação enriquecida da task."""
    selected = classifier or DEFAULT_TASK_CLASSIFIER
    classification = selected.classify(description)
    if not isinstance(classification, TaskClassification):
        raise TypeError("task classifier must return TaskClassification")
    return classification


def classify_task_type(description: str, classifier: TaskClassifier | None = None) -> str:
    """Classifica task type mantendo compatibilidade com a API anterior."""
    return classify_task(description, classifier=classifier).task_type


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

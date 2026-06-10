import pytest

from quimera.plugins.base import AgentPlugin
from quimera.constants import TaskType
from quimera.runtime.task_planning import (
    TaskClassification,
    classify_task,
    classify_task_type,
    score_plugin_for_task,
    choose_best_agent,
)


class MockPlugin(AgentPlugin):
    @property
    def name(self) -> str: return self._name

    @property
    def cmd(self) -> list[str]: return ["mock"]

    def __init__(
            self,
            name,
            tier=1,
            preferred=None,
            avoid=None,
            code=False,
            long=False,
            tools=False,
            caps=None,
            task_execution=True,
            tool_reliability="medium",
    ):
        self._name = name
        self.base_tier = tier
        self.preferred_task_types = preferred or []
        self.avoid_task_types = avoid or []
        self.supports_code_editing = code
        self.supports_long_context = long
        self.supports_tools = tools
        self.tool_use_reliability = tool_reliability
        self.supports_task_execution = task_execution
        self.capabilities = caps or []


def test_classify_task_type():
    """Verifica a classificação do tipo de task."""
    assert classify_task_type("") == TaskType.GENERAL
    assert classify_task_type("something random") == TaskType.GENERAL
    assert classify_task_type("corrija o bug") == TaskType.CODE_EDIT
    assert classify_task_type("execute os testes") == TaskType.TEST_EXECUTION


def test_classify_task_type_with_realistic_descriptions():
    """Verifica a classificação com descrições realistas."""
    assert classify_task_type("Execute os testes de integração e reporte o traceback do task runner") == TaskType.TEST_EXECUTION
    assert classify_task_type("Revise o módulo quimera/app/task.py e aponte regressões") == TaskType.CODE_REVIEW
    assert classify_task_type("Implemente retry para falha transitória no executor de tasks") == TaskType.CODE_EDIT
    assert classify_task_type("Investigue por que o roteador escolhe agente errado com carga alta") == TaskType.BUG_INVESTIGATION
    assert classify_task_type("Documente o comando /task no README") == TaskType.DOCUMENTATION


def test_classify_task_returns_richer_dimensions():
    """Verifica que classify_task retorna dimensões enriquecidas."""
    classification = classify_task("Investigue por que o /task falha em produção, rode pytest e traga traceback.")

    assert classification == TaskClassification(
        task_type=TaskType.BUG_INVESTIGATION,
        complexity="high",
        requires_tools=True,
        requires_code_editing=False,
        risk_level="high",
    )


def test_classify_task_allows_pluggable_classifier():
    """Verifica que classify_task aceita classificador plugável."""
    class CustomClassifier:
        def classify(self, _description: str) -> TaskClassification:
            return TaskClassification(
                task_type=TaskType.CODE_REVIEW,
                complexity="low",
                requires_tools=False,
                requires_code_editing=False,
                risk_level="low",
            )

    classification = classify_task("qualquer descrição", classifier=CustomClassifier())

    assert classification.task_type == TaskType.CODE_REVIEW
    assert classify_task_type("qualquer descrição", classifier=CustomClassifier()) == TaskType.CODE_REVIEW


def test_classify_task_rejects_invalid_classifier_return_type():
    """Verifica que classify_task rejeita tipo de retorno inválido do classificador."""
    class InvalidClassifier:
        def classify(self, _description: str):
            return {"task_type": TaskType.CODE_EDIT}

    with pytest.raises(TypeError, match="task classifier must return TaskClassification"):
        classify_task("qualquer descrição", classifier=InvalidClassifier())


def test_score_plugin_for_task():
    """Verifica o score de um plugin para um tipo de task."""
    p = MockPlugin("p1", tier=3, preferred=[TaskType.CODE_EDIT], code=True, long=True, tools=True)
    # Tier 3 -> (3-1)*2 = 4
    # Preferred CODE_EDIT -> +5
    # Supports code editing -> +2
    # Total = 11
    assert score_plugin_for_task(p, TaskType.CODE_EDIT) == 11

    # Avoid
    p2 = MockPlugin("p2", avoid=[TaskType.TEST_EXECUTION])
    # Tier 1 -> 0
    # Avoid -> -5
    assert score_plugin_for_task(p2, TaskType.TEST_EXECUTION) == -5


def test_choose_best_agent():
    """Verifica a escolha do melhor agente."""
    assert choose_best_agent("any", []) is None

    p1 = MockPlugin("p1", tier=1)
    p2 = MockPlugin("p2", tier=3)
    assert choose_best_agent("any", [p1, p2]) == "p2"


def test_choose_best_agent_fallback():
    """Verifica o fallback na escolha do melhor agente."""
    # p1 avoids the task
    p1 = MockPlugin("p1", avoid=[TaskType.CODE_EDIT])
    # score will be -5
    assert choose_best_agent(TaskType.CODE_EDIT, [p1]) == "p1"

    p2 = MockPlugin("p2", avoid=[TaskType.CODE_EDIT])
    p3 = MockPlugin("p3", preferred=[TaskType.GENERAL])
    # p2 score -5, p3 score 5
    assert choose_best_agent(TaskType.CODE_EDIT, [p2, p3]) == "p3"

    # Test compatible fallback
    p4 = MockPlugin("p4", avoid=[TaskType.CODE_EDIT])  # score -5
    p5 = MockPlugin("p5")  # score 0
    assert choose_best_agent(TaskType.CODE_EDIT, [p4, p5]) == "p5"


def test_code_editing_agents_can_review():
    """Verifica que agentes com code_editing podem fazer review."""
    p = MockPlugin("editor", tier=2, code=True)
    score_review = score_plugin_for_task(p, TaskType.CODE_REVIEW)
    score_edit = score_plugin_for_task(p, TaskType.CODE_EDIT)
    assert score_review >= score_edit - 2, "Agentes com code_editing devem ter bônus para code_review"
    assert score_review > 0, "Agentes com code_editing devem poder ser elegíveis para code_review"

    p2 = MockPlugin("non_editor", tier=2, code=False)
    assert score_plugin_for_task(p2,
                                 TaskType.CODE_REVIEW) < score_review, "Editor deve ter vantagem sobre não-editor para code_review"


def test_bug_investigation_penalizes_plugins_without_tools():
    """Verifica que plugins sem ferramentas são penalizados para bug_investigation."""
    with_tools = MockPlugin("with_tools", tier=2, code=True, tools=True, caps=["general_coding"])
    without_tools = MockPlugin("without_tools", tier=2, code=True, tools=False, caps=["general_coding"])

    score_with = score_plugin_for_task(with_tools, TaskType.BUG_INVESTIGATION)
    score_without = score_plugin_for_task(without_tools, TaskType.BUG_INVESTIGATION)

    assert score_without < score_with, "Plugin sem tools deve ter score menor para bug_investigation"

    plugin_with_tools = MockPlugin("tool_user", tier=1, tools=True)
    plugin_without_tools = MockPlugin("no_tools", tier=1, tools=False)
    score_tools = score_plugin_for_task(plugin_with_tools, TaskType.BUG_INVESTIGATION)
    score_no_tools = score_plugin_for_task(plugin_without_tools, TaskType.BUG_INVESTIGATION)
    assert score_no_tools < score_tools, "Agent sem tools deve ter score menor que agent com tools para bug_investigation"


def test_choose_best_agent_penalizes_no_tools_for_bug_investigation():
    """Verifica que agente sem ferramentas é preterido para bug_investigation."""
    agent_with_tools = MockPlugin("with_tools", tier=2, tools=True)
    agent_without_tools = MockPlugin("without_tools", tier=2, tools=False)

    selected = choose_best_agent(TaskType.BUG_INVESTIGATION, [agent_with_tools, agent_without_tools])
    assert selected == "with_tools", "Agente com tools deve ser preferido para bug_investigation"


def test_test_execution_prefers_high_tool_reliability():
    """Verifica que alta confiabilidade de ferramentas é preferida para test_execution."""
    low = MockPlugin("low", tier=2, tools=True, tool_reliability="low")
    high = MockPlugin("high", tier=1, tools=True, tool_reliability="high")
    assert choose_best_agent(TaskType.TEST_EXECUTION, [low, high]) == "high"


def test_bug_investigation_prefers_high_tool_reliability():
    """Verifica que alta confiabilidade de ferramentas é preferida para bug_investigation."""
    low = MockPlugin("low", tier=2, tools=True, code=True, caps=["general_coding"], tool_reliability="low")
    high = MockPlugin("high", tier=1, tools=True, tool_reliability="high")
    assert choose_best_agent(TaskType.BUG_INVESTIGATION, [low, high]) == "high"


def test_choose_best_agent_ignores_agents_without_task_execution():
    """Verifica que agentes sem execução de tasks são ignorados."""
    non_executor = MockPlugin("qwen-like", tier=3, preferred=[TaskType.CODE_REVIEW], task_execution=False)
    executor = MockPlugin("executor", tier=1)

    selected = choose_best_agent(TaskType.CODE_REVIEW, [non_executor, executor])

    assert selected == "executor"


def test_choose_best_agent_returns_none_when_only_non_executors_are_available():
    """Verifica que retorna None quando só há não-executores disponíveis."""
    non_executor = MockPlugin("qwen-like", tier=3, preferred=[TaskType.CODE_REVIEW], task_execution=False)

    assert choose_best_agent(TaskType.CODE_REVIEW, [non_executor]) is None

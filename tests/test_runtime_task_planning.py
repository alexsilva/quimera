import pytest
from quimera.runtime.task_planning import (
    classify_task_type,
    score_plugin_for_task,
    choose_best_agent,
    TASK_TYPE_GENERAL,
    TASK_TYPE_CODE_EDIT,
    TASK_TYPE_CODE_REVIEW,
    TASK_TYPE_TEST_EXECUTION,
    TASK_TYPE_BUG_INVESTIGATION,
)
from quimera.plugins.base import AgentPlugin

class MockPlugin(AgentPlugin):
    @property
    def name(self) -> str: return self._name
    @property
    def cmd(self) -> list[str]: return ["mock"]
    def __init__(self, name, tier=1, preferred=None, avoid=None, code=False, long=False, tools=False, caps=None, task_execution=True):
        self._name = name
        self.base_tier = tier
        self.preferred_task_types = preferred or []
        self.avoid_task_types = avoid or []
        self.supports_code_editing = code
        self.supports_long_context = long
        self.supports_tools = tools
        self.supports_task_execution = task_execution
        self.capabilities = caps or []

def test_classify_task_type():
    assert classify_task_type("") == TASK_TYPE_GENERAL # Line 43
    assert classify_task_type("something random") == TASK_TYPE_GENERAL # Line 47
    assert classify_task_type("corrija o bug") == "code_edit"
    assert classify_task_type("execute os testes") == "test_execution"

def test_score_plugin_for_task():
    p = MockPlugin("p1", tier=3, preferred=[TASK_TYPE_CODE_EDIT], code=True, long=True, tools=True)
    # Tier 3 -> (3-1)*2 = 4
    # Preferred CODE_EDIT -> +5
    # Supports code editing -> +2
    # Total = 11
    assert score_plugin_for_task(p, TASK_TYPE_CODE_EDIT) == 11
    
    # Avoid
    p2 = MockPlugin("p2", avoid=[TASK_TYPE_TEST_EXECUTION])
    # Tier 1 -> 0
    # Avoid -> -5
    assert score_plugin_for_task(p2, TASK_TYPE_TEST_EXECUTION) == -5

def test_choose_best_agent():
    assert choose_best_agent("any", []) is None # Line 71
    
    p1 = MockPlugin("p1", tier=1)
    p2 = MockPlugin("p2", tier=3)
    assert choose_best_agent("any", [p1, p2]) == "p2"

def test_choose_best_agent_fallback():
    # Line 84-87 coverage
    # p1 avoids the task
    p1 = MockPlugin("p1", avoid=[TASK_TYPE_CODE_EDIT])
    # score will be -5
    assert choose_best_agent(TASK_TYPE_CODE_EDIT, [p1]) == "p1"
    
    p2 = MockPlugin("p2", avoid=[TASK_TYPE_CODE_EDIT])
    p3 = MockPlugin("p3", preferred=[TASK_TYPE_GENERAL])
    # p2 score -5, p3 score 5
    assert choose_best_agent(TASK_TYPE_CODE_EDIT, [p2, p3]) == "p3"
    
    # Test compatible fallback
    p4 = MockPlugin("p4", avoid=[TASK_TYPE_CODE_EDIT]) # score -5
    p5 = MockPlugin("p5") # score 0
    assert choose_best_agent(TASK_TYPE_CODE_EDIT, [p4, p5]) == "p5"

def test_code_editing_agents_can_review():
    p = MockPlugin("editor", tier=2, code=True)
    score_review = score_plugin_for_task(p, TASK_TYPE_CODE_REVIEW)
    score_edit = score_plugin_for_task(p, TASK_TYPE_CODE_EDIT)
    assert score_review >= score_edit - 2, "Agentes com code_editing devem ter bônus para code_review"
    assert score_review > 0, "Agentes com code_editing devem poder ser elegíveis para code_review"

    p2 = MockPlugin("non_editor", tier=2, code=False)
    assert score_plugin_for_task(p2, TASK_TYPE_CODE_REVIEW) < score_review, "Editor deve ter vantagem sobre não-editor para code_review"

def test_bug_investigation_penalizes_plugins_without_tools():
    with_tools = MockPlugin("with_tools", tier=2, code=True, tools=True, caps=["general_coding"])
    without_tools = MockPlugin("without_tools", tier=2, code=True, tools=False, caps=["general_coding"])

    score_with = score_plugin_for_task(with_tools, TASK_TYPE_BUG_INVESTIGATION)
    score_without = score_plugin_for_task(without_tools, TASK_TYPE_BUG_INVESTIGATION)

    assert score_without < score_with, "Plugin sem tools deve ter score menor para bug_investigation"

    plugin_with_tools = MockPlugin("tool_user", tier=1, tools=True)
    plugin_without_tools = MockPlugin("no_tools", tier=1, tools=False)
    score_tools = score_plugin_for_task(plugin_with_tools, TASK_TYPE_BUG_INVESTIGATION)
    score_no_tools = score_plugin_for_task(plugin_without_tools, TASK_TYPE_BUG_INVESTIGATION)
    assert score_no_tools < score_tools, "Agent sem tools deve ter score menor que agent com tools para bug_investigation"

def test_choose_best_agent_penalizes_no_tools_for_bug_investigation():
    agent_with_tools = MockPlugin("with_tools", tier=2, tools=True)
    agent_without_tools = MockPlugin("without_tools", tier=2, tools=False)

    selected = choose_best_agent(TASK_TYPE_BUG_INVESTIGATION, [agent_with_tools, agent_without_tools])
    assert selected == "with_tools", "Agente com tools deve ser preferido para bug_investigation"

def test_choose_best_agent_ignores_agents_without_task_execution():
    non_executor = MockPlugin("qwen-like", tier=3, preferred=[TASK_TYPE_CODE_REVIEW], task_execution=False)
    executor = MockPlugin("executor", tier=1)

    selected = choose_best_agent(TASK_TYPE_CODE_REVIEW, [non_executor, executor])

    assert selected == "executor"

def test_choose_best_agent_returns_none_when_only_non_executors_are_available():
    non_executor = MockPlugin("qwen-like", tier=3, preferred=[TASK_TYPE_CODE_REVIEW], task_execution=False)

    assert choose_best_agent(TASK_TYPE_CODE_REVIEW, [non_executor]) is None

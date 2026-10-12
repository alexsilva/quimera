"""Contratos da família ``tasks_*``/``jobs_*`` (um verbo por tool, sem nome legado)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from quimera.modes import DEBATE_MODE
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.drivers.tool_schemas import TOOL_SCHEMAS
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicyError
from quimera.runtime.tool_metadata import TOOL_METADATA, ApprovalMode, ToolRisk
from quimera.runtime.tools import delegate as delegate_tools
from quimera.runtime.tools import tasks as tasks_tools
from quimera.runtime.tools.tasks import JobTools, TaskTools, TaskToolsValidator
from quimera.workspace import Workspace

LEGACY_NAMES = frozenset({"tasks", "list_tasks", "delegate", "list_jobs", "get_job"})
TASK_FAMILY = ("tasks_create", "tasks_delegate", "tasks_list", "tasks_cancel")
JOB_FAMILY = ("jobs_list", "jobs_get")


@pytest.fixture
def executor(tmp_path):
    config = ToolRuntimeConfig(
        workspace=Workspace(tmp_path),
        require_approval_for_mutations=True,
        require_approval_for_task_creation=True,
    )
    return ToolExecutor(config, approval_handler=None)


def test_legacy_names_are_gone_from_registry_catalog_and_metadata(executor):
    """Sem shim: os nomes antigos não existem em nenhuma superfície do runtime."""
    registry = set(executor.registry.names())
    catalog = {schema["function"]["name"] for schema in TOOL_SCHEMAS}

    assert LEGACY_NAMES.isdisjoint(registry)
    assert LEGACY_NAMES.isdisjoint(catalog)
    assert LEGACY_NAMES.isdisjoint(TOOL_METADATA)
    for name in (*TASK_FAMILY, *JOB_FAMILY):
        assert name in registry, name
        assert name in catalog, name
        assert name in TOOL_METADATA, name


def test_families_follow_the_prefix_convention():
    """Os handlers registrados seguem ``<prefixo>_<verbo>``, como git/memory/browser."""
    assert TaskTools._tool_prefix == "tasks"
    assert JobTools._tool_prefix == "jobs"
    assert all(name.startswith("tasks_") for name in tasks_tools._TASK_TOOL_NAMES)
    assert all(name.startswith("jobs_") for name in tasks_tools._JOB_TOOL_NAMES)
    assert "tasks_delegate" in delegate_tools._DELEGATE_TOOL_NAMES
    assert set(tasks_tools._TASK_TOOL_NAMES) | {"tasks_delegate"} == set(TASK_FAMILY)
    assert set(tasks_tools._JOB_TOOL_NAMES) == set(JOB_FAMILY)


@pytest.mark.parametrize(
    ("name", "risk", "mutates", "parallel_safe", "approval", "profiles"),
    [
        ("tasks_create", ToolRisk.WRITE, True, False, ApprovalMode.TASK_CREATION, {"agent"}),
        ("tasks_delegate", ToolRisk.DELEGATION, True, False, ApprovalMode.MUTATION, {"agent"}),
        ("tasks_list", ToolRisk.READ, False, True, ApprovalMode.NONE, {"read-local", "read", "agent"}),
        ("tasks_cancel", ToolRisk.WRITE, True, False, ApprovalMode.NONE, {"agent"}),
        ("jobs_list", ToolRisk.READ, False, True, ApprovalMode.NONE, {"read-local", "read", "agent"}),
        ("jobs_get", ToolRisk.READ, False, True, ApprovalMode.NONE, {"read-local", "read", "agent"}),
    ],
)
def test_each_verb_carries_its_own_metadata(name, risk, mutates, parallel_safe, approval, profiles):
    """Com um nome por verbo, nenhuma dimensão da policy depende de ``action``."""
    meta = TOOL_METADATA[name]
    assert meta.risk is risk
    assert meta.mutates is mutates
    assert meta.parallel_safe is parallel_safe
    assert meta.approval is approval
    assert set(meta.http_profiles) == profiles


def test_cancel_no_longer_inherits_task_creation_approval(executor):
    """Cancelar deixa de pedir ao humano para aprovar uma "criação"; a gate é a checagem de dono."""
    policy = executor.policy

    assert policy.requires_approval(ToolCall(name="tasks_create", arguments={})) is True
    assert policy.requires_approval(ToolCall(name="tasks_delegate", arguments={})) is True
    assert policy.requires_approval(ToolCall(name="tasks_cancel", arguments={"task_id": 1})) is False
    for name in ("tasks_list", "jobs_list", "jobs_get"):
        assert policy.requires_approval(ToolCall(name=name, arguments={})) is False, name


def test_validators_are_split_per_verb(tmp_path):
    """Cada verbo valida só os próprios argumentos; não há ``action`` nem padrão silencioso."""
    validator = TaskToolsValidator(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    with pytest.raises(ToolPolicyError, match="tasks_create requer 'description'"):
        validator.validate(ToolCall(name="tasks_create", arguments={"task_id": 3}))
    with pytest.raises(ToolPolicyError, match="tasks_cancel requer 'task_id' inteiro"):
        validator.validate(ToolCall(name="tasks_cancel", arguments={"description": "x"}))
    with pytest.raises(ToolPolicyError, match="tasks_cancel requer 'task_id' inteiro"):
        validator.validate(ToolCall(name="tasks_cancel", arguments={"task_id": True}))
    with pytest.raises(ToolPolicyError, match="tasks_list exige ao menos um filtro"):
        validator.validate(ToolCall(name="tasks_list", arguments={}))

    validator.validate(ToolCall(name="tasks_create", arguments={"description": "rodar testes"}))
    validator.validate(ToolCall(name="tasks_cancel", arguments={"task_id": 3}))
    validator.validate(ToolCall(name="tasks_list", arguments={"id": 3}))
    validator.validate(ToolCall(name="jobs_list", arguments={}))
    validator.validate(ToolCall(name="jobs_get", arguments={}))


def test_cancel_reaches_the_handler_through_the_executor(executor):
    """``tasks_cancel`` é roteada pelo registry e responde com erro limpo sem delegação ativa."""
    result = executor.execute(ToolCall(name="tasks_cancel", arguments={"task_id": 424242}))

    assert result.ok is False
    assert "não é uma delegação em execução" in str(result.error)


def test_debate_mode_blocks_every_mutating_task_verb():
    for name in ("tasks_create", "tasks_delegate", "tasks_cancel"):
        assert name in DEBATE_MODE.blocked_tools, name
    for name in ("tasks_list", "jobs_list", "jobs_get"):
        assert name not in DEBATE_MODE.blocked_tools, name


def test_catalog_descriptions_point_to_the_new_names():
    """As descrições públicas orientam pelos nomes novos e explicam a diferença create/delegate."""
    descriptions = {
        schema["function"]["name"]: schema["function"]["description"] for schema in TOOL_SCHEMAS
    }
    assert "tasks_delegate" in descriptions["tasks_create"]
    assert "tasks_list" in descriptions["tasks_create"]
    assert "tasks_create" in descriptions["tasks_delegate"]
    assert "tasks_list" in descriptions["tasks_delegate"]
    assert "tasks_cancel" in descriptions["tasks_delegate"]
    for name, text in descriptions.items():
        for legacy in ("list_tasks", "list_jobs", "get_job"):
            assert legacy not in text, (name, legacy)


_DOCS_WITH_TOOL_NAMES = (
    "README.md",
    "ARCHITECTURE.md",
    "AGENTS.md",
    "quimera/prompt.md",
    "quimera/delegate_prompt.md",
    "docs/guia/mcp-e-ferramentas.md",
    "docs/guia/tasks.md",
    "docs/desenvolvimento/aprovacao.md",
    "docs/desenvolvimento/testes.md",
)
_LEGACY_IN_PROSE = re.compile(
    r"`(tasks|list_tasks|delegate|list_jobs|get_job)`|\"name\": \"(tasks|list_tasks|delegate|list_jobs|get_job)\""
)


def test_prose_surfaces_do_not_cite_legacy_tool_names():
    """Catálogo, prompts e docs não citam os nomes antigos (o CHANGELOG é histórico e fica fora)."""
    root = Path(__file__).resolve().parents[1]
    for schema in TOOL_SCHEMAS:
        text = json_text = str(schema["function"])
        assert not re.search(r"(?<![\w])(list_tasks|list_jobs|get_job)(?![\w])", json_text), schema["function"]["name"]
        assert "'tasks'" not in text and "'delegate'" not in text, schema["function"]["name"]
    for relative in _DOCS_WITH_TOOL_NAMES:
        text = (root / relative).read_text(encoding="utf-8")
        hits = [m.group(0) for m in _LEGACY_IN_PROSE.finditer(text)]
        assert not hits, (relative, hits)

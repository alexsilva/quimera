"""Contratos da família ``tasks_*``/``jobs_*`` (um verbo por tool, sem nome legado)."""
from __future__ import annotations

import json
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


def test_validators_are_split_per_verb(tmp_path, monkeypatch):
    """Cada verbo valida só os próprios argumentos; não há ``action`` nem padrão silencioso."""
    monkeypatch.delenv("QUIMERA_CURRENT_JOB_ID", raising=False)
    validator = TaskToolsValidator(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    with pytest.raises(ToolPolicyError, match="tasks_create requer 'description'"):
        validator.validate(ToolCall(name="tasks_create", arguments={"task_id": 3}))
    with pytest.raises(ToolPolicyError, match="tasks_cancel requer 'task_id' inteiro"):
        validator.validate(ToolCall(name="tasks_cancel", arguments={"description": "x"}))
    with pytest.raises(ToolPolicyError, match="tasks_cancel requer 'task_id' inteiro"):
        validator.validate(ToolCall(name="tasks_cancel", arguments={"task_id": True}))
    with pytest.raises(ToolPolicyError, match="tasks_list sem filtro lista o job atual"):
        validator.validate(ToolCall(name="tasks_list", arguments={}))
    monkeypatch.setenv("QUIMERA_CURRENT_JOB_ID", "366")
    validator.validate(ToolCall(name="tasks_list", arguments={}))
    monkeypatch.delenv("QUIMERA_CURRENT_JOB_ID")

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


def test_tasks_list_without_filters_defaults_to_current_job(tmp_path, monkeypatch):
    """Sem filtro, ``tasks_list`` lista o job atual; fora de um job, recusa com dica (live test 2026-10-12)."""
    tools = TaskTools(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    seen: dict[str, dict] = {}

    def fake_list_tasks(filt, db_path=None):
        seen["filt"] = dict(filt)
        return []

    monkeypatch.setattr(tasks_tools, "_list_tasks", fake_list_tasks)
    monkeypatch.setenv("QUIMERA_CURRENT_JOB_ID", "366")

    result = tools.tasks_list(ToolCall(name="tasks_list", arguments={}))

    assert result.ok is True
    assert seen["filt"] == {"job_id": 366}

    result = tools.tasks_list(ToolCall(name="tasks_list", arguments={"status": "completed"}))
    assert seen["filt"] == {"status": "completed"}, "filtro explícito não ganha job_id implícito"

    monkeypatch.delenv("QUIMERA_CURRENT_JOB_ID")
    seen.clear()
    result = tools.tasks_list(ToolCall(name="tasks_list", arguments={}))

    assert result.ok is False
    assert "nenhum job atual está definido" in result.error
    assert not seen, "sem job atual e sem filtro o banco não é consultado"


def _page(result):
    """Decodifica o envelope paginado do ``content``."""
    assert result.ok is True, result.error
    payload = json.loads(result.content)
    assert set(payload) == {"items", "total", "offset", "limit", "next_offset"}
    return payload


def test_jobs_list_paginates_newest_first_with_limit_and_offset(tmp_path, monkeypatch):
    """``jobs_list`` pagina por ``limit``/``offset`` do mais recente ao mais antigo (pedido de ALEX, 2026-10-12)."""
    job_tools = JobTools(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    rows = [{"id": i} for i in range(1, 31)]  # repositório: updated_at ASC
    monkeypatch.setattr(tasks_tools, "_list_jobs", lambda filt, db_path=None: list(rows))

    first = _page(job_tools.jobs_list(ToolCall(name="jobs_list", arguments={})))
    assert [job["id"] for job in first["items"]] == list(range(30, 10, -1))
    assert (first["total"], first["offset"], first["limit"], first["next_offset"]) == (30, 0, 20, 20)

    second = _page(job_tools.jobs_list(ToolCall(name="jobs_list", arguments={"offset": first["next_offset"]})))
    assert [job["id"] for job in second["items"]] == list(range(10, 0, -1))
    assert second["next_offset"] is None

    full = _page(job_tools.jobs_list(ToolCall(name="jobs_list", arguments={"limit": 50})))
    assert len(full["items"]) == 30 and full["next_offset"] is None

    single = _page(job_tools.jobs_list(ToolCall(name="jobs_list", arguments={"limit": 1, "offset": 3})))
    assert [job["id"] for job in single["items"]] == [27]
    assert single["next_offset"] == 4


def test_list_pages_chain_without_gaps_and_truncated_mirrors_next_offset(tmp_path, monkeypatch):
    """Seguindo ``next_offset`` até ``null`` percorre tudo sem repetir nem pular; ``truncated``/``data`` acompanham."""
    job_tools = JobTools(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    rows = [{"id": i} for i in range(1, 8)]
    monkeypatch.setattr(tasks_tools, "_list_jobs", lambda filt, db_path=None: list(rows))

    seen: list[int] = []
    offset, pages = 0, 0
    while offset is not None:
        result = job_tools.jobs_list(ToolCall(name="jobs_list", arguments={"limit": 3, "offset": offset}))
        page = _page(result)
        assert result.truncated is (page["next_offset"] is not None)
        assert result.data == {k: page[k] for k in ("total", "offset", "limit", "next_offset")}
        seen.extend(job["id"] for job in page["items"])
        offset = page["next_offset"]
        pages += 1

    assert seen == [7, 6, 5, 4, 3, 2, 1]
    assert pages == 3

    beyond = job_tools.jobs_list(ToolCall(name="jobs_list", arguments={"offset": 99}))
    page = _page(beyond)
    assert page["items"] == [] and page["total"] == 7 and page["next_offset"] is None
    assert beyond.truncated is False


def test_tasks_list_paginates_newest_first_and_enriches_only_the_page(tmp_path, monkeypatch):
    """``tasks_list`` usa a mesma mecânica e só anexa ``live`` às tasks da página devolvida."""
    tools = TaskTools(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    rows = [{"id": i, "status": "completed", "origin": "delegate", "body": ""} for i in range(1, 26)]
    monkeypatch.setattr(tasks_tools, "_list_tasks", lambda filt, db_path=None: list(rows))
    enriched: list[int] = []

    def attach(task):
        enriched.append(task["id"])
        return task

    monkeypatch.setattr(tools, "_attach_live_delegation", attach)

    first = _page(tools.tasks_list(ToolCall(name="tasks_list", arguments={"job_id": 3, "limit": 10})))
    assert [task["id"] for task in first["items"]] == list(range(25, 15, -1))
    assert first["next_offset"] == 10 and first["total"] == 25
    assert enriched == list(range(25, 15, -1)), "live só é consultado para a página devolvida"

    last = _page(tools.tasks_list(ToolCall(name="tasks_list", arguments={"job_id": 3, "limit": 10, "offset": 20})))
    assert [task["id"] for task in last["items"]] == [5, 4, 3, 2, 1]
    assert last["next_offset"] is None


def test_page_metadata_precedes_items_and_survives_content_truncation(tmp_path, monkeypatch):
    """Metadados vêm antes de ``items``: o prefixo que sobrevive ao corte do harness ainda traz next_offset (revisão task 102)."""
    job_tools = JobTools(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    rows = [{"id": i, "description": "x" * 4_000} for i in range(1, 31)]
    monkeypatch.setattr(tasks_tools, "_list_jobs", lambda filt, db_path=None: list(rows))

    result = job_tools.jobs_list(ToolCall(name="jobs_list", arguments={"limit": 10}))

    assert result.content.startswith('{"total": 30, "offset": 0, "limit": 10, "next_offset": 10, "items": [')
    clipped = result.to_prompt_payload(2_000)
    assert clipped["truncated"] is True
    assert '"next_offset": 10' in clipped["content"][:120]


def test_page_bounds_report_non_integer_arguments_clearly(tmp_path, monkeypatch):
    """Fora da policy, ``limit``/``offset`` não inteiros viram erro legível, não ``invalid literal``."""
    job_tools = JobTools(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    monkeypatch.setattr(tasks_tools, "_list_jobs", lambda filt, db_path=None: [])

    result = job_tools.jobs_list(ToolCall(name="jobs_list", arguments={"offset": "abc"}))

    assert result.ok is False
    assert result.error == "'offset' deve ser inteiro, recebido 'abc'"


def test_list_limit_is_clamped_by_runtime_ceiling(tmp_path, monkeypatch):
    """O teto ``max_list_results`` vale nas duas listagens mesmo sem passar pela policy."""
    config = ToolRuntimeConfig(workspace=Workspace(tmp_path), max_list_results=5)
    rows = [{"id": i, "status": "completed", "origin": "human", "body": ""} for i in range(1, 13)]
    monkeypatch.setattr(tasks_tools, "_list_jobs", lambda filt, db_path=None: list(rows))
    monkeypatch.setattr(tasks_tools, "_list_tasks", lambda filt, db_path=None: list(rows))

    jobs = _page(JobTools(config).jobs_list(ToolCall(name="jobs_list", arguments={"limit": 500})))
    tasks = _page(TaskTools(config).tasks_list(ToolCall(name="tasks_list", arguments={"job_id": 1, "limit": 0, "offset": -4})))

    assert (len(jobs["items"]), jobs["limit"], jobs["next_offset"]) == (5, 5, 5), "acima do teto cai no teto"
    assert (len(tasks["items"]), tasks["limit"], tasks["offset"]) == (1, 1, 0), "abaixo do piso cai em 1/0"


def test_list_validators_reject_bad_limit_and_offset(tmp_path, monkeypatch):
    """``limit`` inteiro >= 1 e ``offset`` inteiro >= 0 nas duas tools; ausentes são aceitos."""
    monkeypatch.setenv("QUIMERA_CURRENT_JOB_ID", "1")
    validator = TaskToolsValidator(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    for tool in ("jobs_list", "tasks_list"):
        for bad in (0, -1, True, "5", 2.5):
            with pytest.raises(ToolPolicyError, match=f"{tool} requer 'limit' inteiro positivo"):
                validator.validate(ToolCall(name=tool, arguments={"limit": bad}))
        for bad in (-1, False, "0", 1.5):
            with pytest.raises(ToolPolicyError, match=f"{tool} requer 'offset' inteiro não negativo"):
                validator.validate(ToolCall(name=tool, arguments={"offset": bad}))
        validator.validate(ToolCall(name=tool, arguments={"limit": 5, "offset": 0}))
        validator.validate(ToolCall(name=tool, arguments={}))


def test_list_schemas_publish_pagination_contract():
    """Catálogo expõe ``limit``/``offset`` e descreve o envelope nas duas listagens."""
    by_name = {schema["function"]["name"]: schema["function"] for schema in TOOL_SCHEMAS}
    for tool in ("tasks_list", "jobs_list"):
        params = by_name[tool]["parameters"]["properties"]
        assert (params["limit"]["type"], params["limit"]["minimum"]) == ("integer", 1)
        assert (params["offset"]["type"], params["offset"]["minimum"]) == ("integer", 0)
        assert "next_offset" in by_name[tool]["description"]

"""Tools workflows_*: contrato, dono, aprovação, descoberta e bloqueio por modo."""
import json
from types import SimpleNamespace

import pytest

from quimera.modes import MODES
from quimera.runtime.approval import TrustedToolExecutionContext
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.drivers.tool_schemas import resolve_tool_schemas
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicyError
from quimera.runtime.tool_deferral import ESSENTIAL_TOOLS
from quimera.runtime.tool_metadata import ApprovalMode, get_tool_metadata
from quimera.runtime.tools.workflows import WorkflowTools, WorkflowToolsValidator
from quimera.workspace import Workspace
from tests.workflow_harness import Harness, task_spec


@pytest.fixture
def env(tmp_path):
    harness = Harness(tmp_path, ["a1", "a2"])
    config = ToolRuntimeConfig(workspace=Workspace(tmp_path))
    tools = WorkflowTools(config)
    tools.set_workflow_service(SimpleNamespace(coordinator=harness.coordinator))
    return harness, tools


def _call(name, agent="claude", **arguments):
    metadata = {"trusted_context": TrustedToolExecutionContext(agent_name=agent)} if agent else {}
    return ToolCall(name=name, arguments=arguments, metadata=metadata)


def _plan(**extra):
    return {
        "objective": "Corrigir sincronizacao",
        "work_items": [task_spec("T1"), task_spec("T2", deps=["T1"])],
        **extra,
    }


def test_create_start_status_roundtrip(env):
    harness, tools = env
    created = tools.workflows_create(_call("workflows_create", **_plan()))
    assert created.ok and created.data["status"] == "ready" and created.data["created"] is True
    workflow_id = created.data["workflow_id"]
    assert created.data["next"]["arguments"] == {"workflow_id": workflow_id}
    again = tools.workflows_create(_call("workflows_create", **_plan()))
    assert again.data["workflow_id"] == workflow_id and again.data["created"] is False

    started = tools.workflows_start(_call("workflows_start", workflow_id=workflow_id))
    assert started.ok and started.data["status"] == "running"
    status = tools.workflows_status(_call("workflows_status", agent="a1", workflow_id=workflow_id))
    assert status.ok and status.data["counts"]["total"] == 2
    assert [i["item"] for i in status.data["work_items"]] == ["T1", "T2"]
    assert status.data["work_items"][1]["depends_on"] == ["T1"]
    listing = tools.workflows_status(_call("workflows_status", agent="a1"))
    assert [w["workflow_id"] for w in listing.data["workflows"]] == [workflow_id]


def test_dry_run_validates_without_persisting(env):
    harness, tools = env
    result = tools.workflows_create(_call("workflows_create", dry_run=True, **_plan()))
    assert result.ok and result.data["valid"] is True
    assert harness.workflows.list_workflows() == []
    bad = tools.workflows_create(
        _call("workflows_create", dry_run=True, objective="x",
              work_items=[task_spec("A", deps=["B"]), task_spec("B", deps=["A"])])
    )
    assert not bad.ok and "ciclo" in bad.error


def test_only_the_coordinator_can_mutate(env):
    harness, tools = env
    workflow_id = tools.workflows_create(_call("workflows_create", **_plan())).data["workflow_id"]
    for name, extra in (
        ("workflows_start", {}),
        ("workflows_cancel", {}),
        ("workflows_control", {"action": "pause"}),
        ("workflows_finish", {"outcome": "failed", "summary": "x"}),
    ):
        denied = getattr(tools, name)(_call(name, agent="intruso", workflow_id=workflow_id, **extra))
        assert not denied.ok and "coordenador" in denied.error, name
    anonymous = tools.workflows_start(_call("workflows_start", agent=None, workflow_id=workflow_id))
    assert not anonymous.ok and "identidade" in anonymous.error
    assert harness.workflows.get(workflow_id).status.value == "ready"
    no_identity = tools.workflows_create(_call("workflows_create", agent=None, **_plan()))
    assert not no_identity.ok


def test_replan_pause_resume_cancel_and_finish_flow(env):
    harness, tools = env
    wf = tools.workflows_create(_call("workflows_create", **_plan())).data["workflow_id"]
    tools.workflows_start(_call("workflows_start", workflow_id=wf))
    paused = tools.workflows_control(_call("workflows_control", workflow_id=wf, action="pause"))
    assert paused.data["status"] == "paused"
    resumed = tools.workflows_control(_call("workflows_control", workflow_id=wf, action="resume"))
    assert resumed.data["status"] == "running"

    replanned = tools.workflows_replan(
        _call("workflows_replan", workflow_id=wf, base_version=1,
              objective="Corrigir sincronizacao",
              work_items=[task_spec("T1"), task_spec("T2", deps=["T1"]), task_spec("T3", deps=["T2"])])
    )
    assert replanned.ok and replanned.data["plan_version"] == 2
    assert replanned.data["replan"]["applied"] is True
    stale = tools.workflows_replan(
        _call("workflows_replan", workflow_id=wf, base_version=1, objective="x",
              work_items=[task_spec("Z")])
    )
    assert not stale.ok and "versao" in stale.error

    premature = tools.workflows_finish(
        _call("workflows_finish", workflow_id=wf, outcome="completed", summary="pronto")
    )
    assert not premature.ok and "sem aceite" in premature.error  # nao declara sem evidencia
    blocked = tools.workflows_finish(
        _call("workflows_finish", workflow_id=wf, outcome="blocked", summary="falta acesso ao banco")
    )
    assert blocked.ok and blocked.data["status"] == "blocked"
    assert blocked.data["result"] == "falta acesso ao banco"
    cancelled = tools.workflows_cancel(_call("workflows_cancel", workflow_id=wf, reason="desistiu"))
    assert cancelled.ok and cancelled.data["status"] == "cancelled"


def test_unavailable_without_session_binding(tmp_path):
    tools = WorkflowTools(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    result = tools.workflows_status(_call("workflows_status"))
    assert not result.ok and "unavailable" in result.error


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("workflows_create", {"objective": "x"}),
        ("workflows_create", {"objective": "x", "work_items": [], "dry_run": "sim"}),
        ("workflows_start", {}),
        ("workflows_start", {"workflow_id": True}),
        ("workflows_control", {"workflow_id": 1, "action": "stop"}),
        ("workflows_replan", {"workflow_id": 1, "base_version": 0, "work_items": []}),
        ("workflows_replan", {"workflow_id": 1, "base_version": 1}),
        ("workflows_finish", {"workflow_id": 1, "outcome": "done", "summary": "x"}),
        ("workflows_finish", {"workflow_id": 1, "outcome": "failed", "summary": " "}),
    ],
)
def test_validator_rejects_malformed_arguments(tmp_path, name, arguments):
    validator = WorkflowToolsValidator(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    with pytest.raises(ToolPolicyError):
        validator.validate(ToolCall(name=name, arguments=arguments))


def test_registration_metadata_approval_and_discovery(tmp_path):
    names = [
        "workflows_create", "workflows_start", "workflows_status", "workflows_control",
        "workflows_replan", "workflows_cancel", "workflows_finish",
    ]
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), None)
    unbound = {s["function"]["name"] for s in resolve_tool_schemas(executor)}
    assert not unbound & set(names)  # sem serviço de sessão, as tools não são anunciadas
    executor.set_workflow_service(SimpleNamespace(coordinator=None))
    schemas = {s["function"]["name"] for s in resolve_tool_schemas(executor)}
    for name in names:
        assert executor.registry.get(name) is not None, name
        assert name in schemas, name
        assert get_tool_metadata(name) is not None, name
        assert name not in ESSENTIAL_TOOLS  # descobertas por tool_search, sem inflar o prompt
    # iniciar/replanejar disparam trabalho com auto-aprovacao: mesma aprovacao de tasks_create
    assert get_tool_metadata("workflows_start").approval == ApprovalMode.TASK_CREATION
    assert get_tool_metadata("workflows_replan").approval == ApprovalMode.TASK_CREATION
    assert get_tool_metadata("workflows_status").mutates is False
    for mode in ("planning", "analysis", "review", "design"):
        blocked = next(m for m in MODES.values() if m.name == mode).blocked_tools
        assert "workflows_start" in blocked and "workflows_create" in blocked, mode
    assert json.dumps(sorted(names)) == json.dumps(sorted(set(names)))


@pytest.mark.parametrize(
    "run_ids",
    [
        {"run_id": "agentrun:wf3-t9-a0-codex"},
        {"run_id": "agentrun:wf3-t9-r1-claude"},
        {"run_id": "agentrun:sub", "parent_run_id": "agentrun:wf3-t9-a0-codex"},
    ],
)
def test_workflow_task_runs_cannot_spawn_workflows(env, run_ids):
    harness, tools = env
    created = tools.workflows_create(_call("workflows_create", **_plan()))
    workflow_id = created.data["workflow_id"]
    trusted = TrustedToolExecutionContext(agent_name="claude", **run_ids)

    def inside(name, **arguments):
        return ToolCall(name=name, arguments=arguments, metadata={"trusted_context": trusted})

    for call in (
        inside("workflows_create", **_plan(idempotency_key="nested")),
        inside("workflows_start", workflow_id=workflow_id),
        inside("workflows_replan", workflow_id=workflow_id, base_version=1,
               **_plan(objective="outro")),
    ):
        result = getattr(tools, call.name)(call)
        assert not result.ok and "dentro de uma task de workflow" in result.error
    assert len(harness.coordinator.list_workflows()) == 1
    # Consultar continua permitido dentro da task.
    assert tools.workflows_status(inside("workflows_status", workflow_id=workflow_id)).ok
    # Fora de uma task de workflow (chat normal / decisão do coordenador) segue permitido.
    assert tools.workflows_start(_call("workflows_start", workflow_id=workflow_id)).ok


def test_orchestrated_subagent_cannot_spawn_workflows(env):
    harness, tools = env
    tools.set_orchestrator_provider(lambda: "claude")
    sub = TrustedToolExecutionContext(agent_name="a1", parent_agent="claude")
    refused = tools.workflows_create(
        ToolCall(name="workflows_create", arguments=_plan(), metadata={"trusted_context": sub})
    )
    assert not refused.ok and "orquestrador" in refused.error
    assert harness.coordinator.list_workflows() == []
    # O orquestrador e agentes chamados direto pelo humano continuam podendo.
    assert tools.workflows_create(_call("workflows_create", **_plan())).ok
    assert tools.workflows_create(_call("workflows_create", agent="a1", **_plan())).ok


def test_delegation_from_a_workflow_task_inherits_the_task_run_prefix(tmp_path):
    from unittest.mock import MagicMock

    from quimera.runtime.tools.delegate import DelegateTools
    from quimera.workflows.execution import is_workflow_task_run

    delegate_tools = DelegateTools(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    dispatch = MagicMock(return_value="feito")
    delegate_tools.set_delegate_fn(dispatch)

    def delegated_run(run_id):
        dispatch.reset_mock()
        trusted = TrustedToolExecutionContext(agent_name="codex", run_id=run_id)
        call = ToolCall(
            name="tasks_delegate",
            arguments={"target_agent": "b", "request": "investigue"},
            metadata={"trusted_context": trusted},
        )
        assert delegate_tools.tasks_delegate(call).ok
        return dispatch.call_args.kwargs["delegation"]

    nested = delegated_run("agentrun:wf3-t9-a0-codex")
    assert nested["parent_run_id"] == "agentrun:wf3-t9-a0-codex"
    assert nested["run_id"].startswith("agentrun:wf3-t9-a0-codex-d")
    assert is_workflow_task_run(nested["run_id"])  # a guarda vale no run filho (e nos netos)
    plain = delegated_run("agentrun:chat-1")
    assert "run_id" not in plain and "parent_run_id" not in plain  # delegacao comum inalterada

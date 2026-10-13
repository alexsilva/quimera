"""Família ``workflows_*`` — orquestração distribuída de tarefas com dependências.

Camada fina que adapta o ``WorkflowCoordinator`` às tools: valida argumentos,
confere o dono (o agente coordenador que criou o workflow) e traduz erros de
domínio em ``ToolResult``. Nenhuma regra de negócio vive aqui.

- ``workflows_create``: valida e publica um plano (``dry_run`` só valida);
- ``workflows_start``: inicia a execução (mesma aprovação de ``tasks_create``);
- ``workflows_status``: estado e evidências lidos do banco;
- ``workflows_control``: pausa ou retoma;
- ``workflows_replan``: nova versão do plano (mesma aprovação de ``tasks_create``);
- ``workflows_cancel``: cancela o workflow e suas execuções em curso;
- ``workflows_finish``: conclusão explícita (completed, blocked ou failed).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from ..approval import TrustedToolExecutionContext
from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError
from ...workflows.execution import is_workflow_task_run
from ...workflows.models import OPEN_WORKFLOW_STATES, WorkflowError, WorkflowRecord
from ...workflows.presentation import snapshot_payload
from .base import ToolBase, ValidatableTool

_WORKFLOW_TOOL_NAMES = [
    "workflows_create",
    "workflows_start",
    "workflows_status",
    "workflows_control",
    "workflows_replan",
    "workflows_cancel",
    "workflows_finish",
]
# Tools que colocam trabalho novo em movimento: proibidas dentro de uma task de
# workflow, cujo executor roda com auto-aprovação de tasks (sem cascatas).
_SPAWNING_TOOLS = frozenset({"workflows_create", "workflows_start", "workflows_replan"})
_CONTROL_ACTIONS = frozenset({"pause", "resume"})
_FINISH_OUTCOMES = frozenset({"completed", "blocked", "failed"})
_PLAN_FIELDS = (
    "objective",
    "work_items",
    "global_criteria",
    "constraints",
    "idempotency_key",
)

class _WorkflowService(Protocol):
    """Contrato mínimo do serviço injetado pelo bootstrap."""

    @property
    def coordinator(self) -> Any:
        ...


def _normalize(name: str | None) -> str:
    return str(name or "").strip().lower().lstrip("/")


class WorkflowTools(
    ToolBase,
    tool_prefix="workflows",
    tool_public_methods=(
        "set_workflow_service",
        "set_orchestrator_provider",
        "is_workflows_available",
    ),
):
    """Ferramentas ``workflows_*``."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        super().__init__(config)
        self._service: _WorkflowService | None = None
        self._orchestrator_provider: Callable[[], str | None] | None = None

    def set_workflow_service(self, service: _WorkflowService | None) -> None:
        """Injeta o serviço de workflows da sessão."""
        self._service = service

    def set_orchestrator_provider(self, fn: Callable[[], str | None] | None) -> None:
        """Injeta o provider do orquestrador ativo (mesma regra de ``tasks_delegate``)."""
        self._orchestrator_provider = fn

    def is_workflows_available(self) -> bool:
        return self._service is not None

    # ── Identidade ─────────────────────────────────────────────────────

    @staticmethod
    def _caller(call: ToolCall) -> str:
        trusted = call.metadata.get("trusted_context")
        if isinstance(trusted, TrustedToolExecutionContext):
            return _normalize(trusted.agent_name)
        if isinstance(trusted, dict):
            return _normalize(trusted.get("agent_name"))
        return ""

    def _owned(self, call: ToolCall, workflow: WorkflowRecord) -> str | None:
        """Erro quando o chamador não é o coordenador do workflow."""
        caller = self._caller(call)
        if not caller:
            return f"{call.name} requer identidade de agente confiavel"
        if caller != _normalize(workflow.coordinator):
            return (
                f"apenas o coordenador ({workflow.coordinator}) pode usar "
                f"{call.name} no workflow {workflow.id}"
            )
        return None

    # ── Execução comum ─────────────────────────────────────────────────

    @staticmethod
    def _inside_workflow_task(call: ToolCall) -> bool:
        trusted = TrustedToolExecutionContext.from_trusted_metadata(call.metadata)
        return is_workflow_task_run(trusted.run_id, trusted.parent_run_id)

    def _orchestrated_subagent(self, call: ToolCall) -> bool:
        """True para agente delegado pelo orquestrador ativo: só o orquestrador distribui trabalho."""
        orchestrator = _normalize(self._orchestrator_provider() if self._orchestrator_provider else None)
        if not orchestrator:
            return False
        trusted = TrustedToolExecutionContext.from_trusted_metadata(call.metadata)
        caller = _normalize(trusted.agent_name)
        return bool(caller) and caller != orchestrator and _normalize(trusted.parent_agent) == orchestrator

    def _run(self, call: ToolCall, action) -> ToolResult:
        if self._service is None:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=f"{call.name} is unavailable outside an active Quimera session",
            )
        if call.name in _SPAWNING_TOOLS and self._inside_workflow_task(call):
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=(
                    f"{call.name} nao pode ser usada dentro de uma task de workflow: "
                    "reporte a necessidade no handoff para o coordenador replanejar"
                ),
            )
        if call.name in _SPAWNING_TOOLS and self._orchestrated_subagent(call):
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=(
                    f"{call.name} nao pode ser usada por agente delegado com o modo orquestrador "
                    "ativo: so o orquestrador distribui trabalho"
                ),
            )
        try:
            data = action(self._service.coordinator)
        except WorkflowError as exc:
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))
        return ToolResult(
            ok=True, tool_name=call.name, content=json.dumps(data, ensure_ascii=False), data=data
        )

    def _owned_action(self, call: ToolCall, action) -> ToolResult:
        """Executa ``action(coordinator, workflow)`` só para o coordenador do workflow."""
        workflow_id = call.arguments["workflow_id"]

        def guarded(coordinator):
            workflow = coordinator.snapshot(workflow_id).workflow
            denied = self._owned(call, workflow)
            if denied:
                raise WorkflowError(denied)
            return action(coordinator, workflow)

        return self._run(call, guarded)

    @staticmethod
    def _plan_payload(arguments: dict) -> dict:
        return {key: arguments[key] for key in _PLAN_FIELDS if key in arguments}

    # ── Tools ──────────────────────────────────────────────────────────

    def workflows_create(self, call: ToolCall) -> ToolResult:
        """Valida e publica um plano estruturado; fica ``ready`` até ``workflows_start``."""
        caller = self._caller(call)
        if not caller:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="workflows_create requires a trusted agent identity",
            )
        payload = self._plan_payload(call.arguments)

        def action(coordinator):
            if call.arguments.get("dry_run") is True:
                plan = coordinator.validate(payload, requested_by=caller)
                return {
                    "valid": True,
                    "work_items": [spec.id for spec in plan.tasks],
                    "write_tasks": [spec.id for spec in plan.tasks if spec.writes],
                }
            result = coordinator.create(payload, requested_by=caller)
            data = snapshot_payload(coordinator.snapshot(result.workflow.id))
            data["created"] = result.created
            data["next"] = {"tool": "workflows_start", "arguments": {"workflow_id": data["workflow_id"]}}
            return data

        return self._run(call, action)

    def workflows_start(self, call: ToolCall) -> ToolResult:
        """Inicia a execução distribuída; repetir a chamada é idempotente."""
        return self._owned_action(
            call,
            lambda coordinator, workflow: snapshot_payload(
                coordinator.snapshot(coordinator.start(workflow.id).id)
            ),
        )

    def workflows_status(self, call: ToolCall) -> ToolResult:
        """Estado persistido; sem ``workflow_id`` resume os workflows abertos."""
        include = call.arguments.get("include_results") is True
        workflow_id = call.arguments.get("workflow_id")

        def action(coordinator):
            if workflow_id is None:
                return {
                    "workflows": [
                        snapshot_payload(coordinator.snapshot(w.id))
                        for w in coordinator.list_workflows(OPEN_WORKFLOW_STATES)
                    ]
                }
            return snapshot_payload(coordinator.snapshot(workflow_id), include_results=include)

        return self._run(call, action)

    def workflows_control(self, call: ToolCall) -> ToolResult:
        """Pausa (sem novas reivindicações) ou retoma um workflow."""
        action_name = call.arguments["action"]

        def action(coordinator, workflow):
            changed = (
                coordinator.pause(workflow.id)
                if action_name == "pause"
                else coordinator.resume(workflow.id)
            )
            return snapshot_payload(coordinator.snapshot(changed.id))

        return self._owned_action(call, action)

    def workflows_replan(self, call: ToolCall) -> ToolResult:
        """Aplica uma nova versão do plano (lista completa), preservando o que segue válido."""
        payload = self._plan_payload(call.arguments)

        def action(coordinator, workflow):
            result = coordinator.replan(
                workflow.id, payload, base_version=call.arguments["base_version"]
            )
            data = snapshot_payload(coordinator.snapshot(workflow.id))
            data["replan"] = {
                "applied": result.applied,
                "kept_tasks": list(result.kept_task_ids),
                "created_tasks": list(result.created_task_ids),
                "retired_tasks": list(result.retired_task_ids),
            }
            return data

        return self._owned_action(call, action)

    def workflows_cancel(self, call: ToolCall) -> ToolResult:
        """Cancela o workflow e interrompe só as execuções dele."""
        reason = call.arguments.get("reason")

        def action(coordinator, workflow):
            changed = coordinator.cancel(
                workflow.id, reason=str(reason).strip() if isinstance(reason, str) else None
            )
            return snapshot_payload(coordinator.snapshot(changed.id))

        return self._owned_action(call, action)

    def workflows_finish(self, call: ToolCall) -> ToolResult:
        """Conclusão explícita: ``completed`` exige todas as tasks aceitas."""

        def action(coordinator, workflow):
            changed = coordinator.finish(
                workflow.id,
                outcome=call.arguments["outcome"],
                summary=call.arguments["summary"],
            )
            return snapshot_payload(coordinator.snapshot(changed.id), include_results=True)

        return self._owned_action(call, action)


class WorkflowToolsValidator(ValidatableTool):
    """Validação de argumentos das tools ``workflows_*`` (a do plano é do domínio)."""

    @staticmethod
    def _require_id(call: ToolCall, *, required: bool = True) -> None:
        value = call.arguments.get("workflow_id")
        if value is None and not required:
            return
        if not isinstance(value, int) or isinstance(value, bool):
            raise ToolPolicyError(f"{call.name} requer 'workflow_id' inteiro")

    def _validate_workflows_create(self, call: ToolCall) -> None:
        if not isinstance(call.arguments.get("work_items"), list):
            raise ToolPolicyError("workflows_create requer 'work_items' como lista")
        dry_run = call.arguments.get("dry_run")
        if dry_run is not None and not isinstance(dry_run, bool):
            raise ToolPolicyError("workflows_create requer 'dry_run' booleano")

    def _validate_workflows_start(self, call: ToolCall) -> None:
        self._require_id(call)

    def _validate_workflows_status(self, call: ToolCall) -> None:
        self._require_id(call, required=False)

    def _validate_workflows_control(self, call: ToolCall) -> None:
        self._require_id(call)
        if call.arguments.get("action") not in _CONTROL_ACTIONS:
            raise ToolPolicyError("workflows_control requer 'action' pause ou resume")

    def _validate_workflows_replan(self, call: ToolCall) -> None:
        self._require_id(call)
        base = call.arguments.get("base_version")
        if not isinstance(base, int) or isinstance(base, bool) or base < 1:
            raise ToolPolicyError("workflows_replan requer 'base_version' inteiro positivo")
        if not isinstance(call.arguments.get("work_items"), list):
            raise ToolPolicyError("workflows_replan requer 'work_items' como lista completa")

    def _validate_workflows_cancel(self, call: ToolCall) -> None:
        self._require_id(call)

    def _validate_workflows_finish(self, call: ToolCall) -> None:
        self._require_id(call)
        if call.arguments.get("outcome") not in _FINISH_OUTCOMES:
            raise ToolPolicyError("workflows_finish requer 'outcome' completed, blocked ou failed")
        summary = call.arguments.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ToolPolicyError("workflows_finish requer 'summary' nao vazio")


def register(registry, policy, config) -> WorkflowTools:
    """Registra a família ``workflows_*`` e sua validação; devolve a instância para injeção."""
    tools = WorkflowTools(config)
    validator = WorkflowToolsValidator(config)
    for name in _WORKFLOW_TOOL_NAMES:
        registry.register(name, getattr(tools, name))
    policy.register_tool_validator(_WORKFLOW_TOOL_NAMES, validator)
    return tools

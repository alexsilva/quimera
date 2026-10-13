"""Parsing e validação determinística de planos de workflow (sem chamada de IA)."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from ..domain.work_items import (
    WorkItem,
    WorkItemError,
    parse_work_item,
    text_field,
    text_list,
    validate_work_items,
)
from .models import WorkflowError, WorkflowLimits, WorkflowPlan, WorkflowTaskSpec

# Tipos cujo resultado normal é alterar arquivos do workspace: ficam serializados.
_WRITING_TASK_TYPES = frozenset({"code_edit", "documentation"})


class PlanValidationError(WorkflowError):
    """Plano estruturalmente inválido ou acima dos limites operacionais."""


def parse_plan(
    payload: Any,
    *,
    coordinator: str,
    limits: WorkflowLimits,
    version: int = 1,
) -> WorkflowPlan:
    """Converte o JSON informado pelo agente em ``WorkflowPlan`` validado."""
    if not isinstance(payload, dict):
        raise PlanValidationError("plano deve ser um objeto")
    try:
        objective = text_field(payload.get("objective"), "objective", 4_000)
        if not objective:
            raise PlanValidationError("campo obrigatorio ausente: objective")
        raw_tasks = payload.get("work_items")
        if not isinstance(raw_tasks, (list, tuple)) or not raw_tasks:
            raise PlanValidationError("work_items deve ser uma lista nao vazia")
        if len(raw_tasks) > limits.max_tasks:
            raise PlanValidationError(f"work_items excede o limite de {limits.max_tasks} itens")
        specs = tuple(
            _parse_spec(raw, default_id=f"T{index}")
            for index, raw in enumerate(raw_tasks, start=1)
        )
        plan = WorkflowPlan(
            objective=objective,
            coordinator=coordinator,
            tasks=specs,
            global_criteria=text_list(payload.get("global_criteria"), "global_criteria"),
            constraints=text_list(payload.get("constraints"), "constraints"),
            version=version,
        )
        validate_plan(plan)
    except WorkItemError as exc:
        raise PlanValidationError(str(exc)) from exc
    return plan


def validate_plan(plan: WorkflowPlan) -> None:
    """Valida IDs, dependências, ciclos e critérios do plano."""
    try:
        validate_work_items(spec.item for spec in plan.tasks)
    except WorkItemError as exc:
        raise PlanValidationError(str(exc)) from exc
    for spec in plan.tasks:
        if not spec.item.acceptance_criteria:
            raise PlanValidationError(
                f"task {spec.id} exige ao menos um criterio de aceite verificavel"
            )


def _parse_spec(raw: Any, *, default_id: str) -> WorkflowTaskSpec:
    item = parse_work_item(raw, default_id=default_id)
    writes = raw.get("writes")
    if writes is None:
        writes = item.task_type in _WRITING_TASK_TYPES
    elif not isinstance(writes, bool):
        raise WorkItemError(f"writes deve ser booleano no work_item {item.id}")
    return WorkflowTaskSpec(
        item=item,
        evidence_expected=text_list(raw.get("evidence_expected"), "evidence_expected"),
        writes=writes,
    )


def resolve_assignees(
    plan: WorkflowPlan, active_agents: Iterable[str]
) -> WorkflowPlan:
    """Mantém ``assigned_to`` só para agentes ativos; os demais caem no roteador."""
    active = {str(name).strip().lstrip("/").lower(): str(name) for name in active_agents}
    resolved = []
    for spec in plan.tasks:
        suggested = (spec.item.assigned_to or "").strip().lstrip("/").lower()
        owner = active.get(suggested)
        resolved.append(replace(spec, item=replace(spec.item, assigned_to=owner)))
    return replace(plan, tasks=tuple(resolved))


def spec_to_dict(spec: WorkflowTaskSpec) -> dict[str, Any]:
    """Forma canônica (e serializável) de uma tarefa do plano."""
    data = spec.item.as_dict()
    data["evidence_expected"] = list(spec.evidence_expected)
    data["writes"] = spec.writes
    return data


def spec_from_dict(data: dict[str, Any]) -> WorkflowTaskSpec:
    """Reconstrói uma tarefa persistida; o conteúdo já foi validado na publicação."""
    return WorkflowTaskSpec(
        item=WorkItem(
            id=data["id"],
            title=data["title"],
            description=data["description"],
            task_type=data["task_type"],
            assigned_to=data.get("assigned_to"),
            dependencies=tuple(data.get("dependencies", ())),
            acceptance_criteria=tuple(data.get("acceptance_criteria", ())),
            priority=data.get("priority", "medium"),
        ),
        evidence_expected=tuple(data.get("evidence_expected", ())),
        writes=bool(data.get("writes", False)),
    )


def plan_to_dict(plan: WorkflowPlan) -> dict[str, Any]:
    """Serialização canônica do plano (usada no histórico de versões e no hash)."""
    return {
        "objective": plan.objective,
        "coordinator": plan.coordinator,
        "global_criteria": list(plan.global_criteria),
        "constraints": list(plan.constraints),
        "tasks": [spec_to_dict(spec) for spec in plan.tasks],
    }


def plan_from_dict(data: dict[str, Any], *, version: int) -> WorkflowPlan:
    """Reconstrói uma versão persistida do plano."""
    return WorkflowPlan(
        objective=data["objective"],
        coordinator=data["coordinator"],
        tasks=tuple(spec_from_dict(raw) for raw in data["tasks"]),
        global_criteria=tuple(data.get("global_criteria", ())),
        constraints=tuple(data.get("constraints", ())),
        version=version,
    )


def plan_hash(plan: WorkflowPlan) -> str:
    """Hash estável do conteúdo do plano (independe da versão)."""
    canonical = json.dumps(plan_to_dict(plan), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

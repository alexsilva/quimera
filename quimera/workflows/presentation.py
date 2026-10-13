"""Apresentação de workflows: dicionários para tools e texto simples para a UI.

Toda informação vem do snapshot persistido; nenhuma mensagem é gerada por modelo.
"""
from __future__ import annotations

from typing import Any

from .handoff import Handoff
from .models import TaskView, WorkflowSnapshot

_STATUS_LABELS = {
    "in_progress": "executando",
    "pending_review": "aguardando review",
    "reviewing": "em review",
    "pending": "pendente",
    "completed": "concluida",
    "failed": "falhou",
    "rejected": "rejeitada",
    "cancelled": "cancelada",
}
_SUMMARY_CHARS = 240


def task_payload(view: TaskView, *, include_result: bool) -> dict[str, Any]:
    """Visão compacta de uma task; o resultado completo só quando pedido."""
    data: dict[str, Any] = {
        "item": view.link.item_id,
        "task_id": view.link.task_id,
        "title": view.title,
        "status": view.status,
        "assigned_to": view.assigned_to,
        "depends_on": list(view.dependencies),
        "attempts": view.attempt_count,
        "writes": view.link.writes,
    }
    if view.notes and view.status != "completed":
        data["notes"] = view.notes[:_SUMMARY_CHARS]
    handoff = Handoff.from_json(view.result)
    if view.status == "completed" and handoff is not None:
        data["summary"] = handoff.summary[:_SUMMARY_CHARS]
        if include_result:
            data["handoff"] = {
                "agent": handoff.agent,
                "files_changed": list(handoff.files_changed),
                "checks": [
                    {"command": c.command, "outcome": c.outcome} for c in handoff.checks
                ],
                "evidence_refs": list(handoff.evidence_refs),
                "limitations": list(handoff.limitations),
                "structured": handoff.structured,
            }
    return data


def snapshot_payload(snapshot: WorkflowSnapshot, *, include_results: bool = False) -> dict[str, Any]:
    """Estado do workflow para a tool ``workflows_status``."""
    workflow = snapshot.workflow
    return {
        "workflow_id": workflow.id,
        "objective": workflow.objective,
        "coordinator": workflow.coordinator,
        "status": workflow.status.value,
        "status_reason": workflow.status_reason,
        "plan_version": workflow.plan_version,
        "replans": workflow.replans,
        "attention": workflow.attention,
        "global_criteria": list(workflow.global_criteria),
        "counts": snapshot.counts(),
        "work_items": [task_payload(view, include_result=include_results) for view in snapshot.tasks],
        "result": workflow.result,
    }


def render_text(snapshot: WorkflowSnapshot) -> str:
    """Resumo legível (UI/terminal) com objetivo, contagens, responsáveis e dependências."""
    workflow = snapshot.workflow
    counts = snapshot.counts()
    lines = [
        f"workflow {workflow.id} [{workflow.status.value}] plano v{workflow.plan_version} "
        f"· coordenador {workflow.coordinator}",
        f"objetivo: {workflow.objective}",
        "tasks: {total} | concluidas {completed} | em execucao {running} | pendentes {pending} "
        "| bloqueadas {blocked} | falhas {failed} | canceladas {cancelled}".format(**counts),
    ]
    if workflow.status_reason:
        lines.append(f"motivo: {workflow.status_reason}")
    for view in snapshot.tasks:
        deps = f" <- {','.join(view.dependencies)}" if view.dependencies else ""
        retries = f" (tentativa {view.attempt_count + 1})" if view.attempt_count else ""
        lines.append(
            f"  [{view.link.item_id}] task {view.link.task_id} "
            f"{_STATUS_LABELS.get(view.status, view.status)}"
            f" · {view.assigned_to or 'roteamento automatico'}{deps}{retries} · {view.title}"
        )
    if workflow.result:
        lines.append(f"resultado: {workflow.result}")
    return "\n".join(lines)


def overview(snapshots: list[WorkflowSnapshot]) -> list[dict[str, Any]]:
    """Resumo mínimo dos workflows abertos para o shared_state (contexto, não fonte de verdade)."""
    return [
        {
            "workflow_id": snap.workflow.id,
            "status": snap.workflow.status.value,
            "objective": snap.workflow.objective[:160],
            "plan_version": snap.workflow.plan_version,
            "counts": snap.counts(),
        }
        for snap in snapshots
    ]

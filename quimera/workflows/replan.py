"""Regra determinística de preservação de tasks entre versões de um plano."""
from __future__ import annotations

from .models import WorkflowPlan, WorkflowTaskSpec
from .plan import spec_to_dict

_DEAD_STATUSES = frozenset({"failed", "rejected", "cancelled"})


def decide_kept(
    current: dict[str, tuple[WorkflowTaskSpec, str]], new_plan: WorkflowPlan
) -> set[str]:
    """Itens cujas tasks continuam válidas na nova versão.

    Uma task é preservada quando existe viva (nem falha nem cancelada), tem
    especificação idêntica (o responsável sugerido não conta: é só roteamento)
    e todas as suas dependências também são preservadas;
    qualquer mudança invalida a task e, em cascata, as que dependem dela.
    """
    by_id = {spec.id: spec for spec in new_plan.tasks}
    kept: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visited:
            return
        visited.add(item_id)
        spec = by_id[item_id]
        for dependency in spec.item.dependencies:
            visit(dependency)
        previous = current.get(item_id)
        if previous is None:
            return
        old_spec, status = previous
        if status in _DEAD_STATUSES or _identity(old_spec) != _identity(spec):
            return
        if all(dependency in kept for dependency in spec.item.dependencies):
            kept.add(item_id)

    for item_id in by_id:
        visit(item_id)
    return kept


def _identity(spec: WorkflowTaskSpec) -> dict:
    """Conteúdo que define a task; o responsável sugerido não a invalida."""
    data = spec_to_dict(spec)
    data.pop("assigned_to", None)
    return data

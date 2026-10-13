"""Contrato neutro de work items: estrutura, parsing estrito e validação do grafo.

Compartilhado por ``/debate`` (modo workflow) e pelos workflows distribuídos,
sem que um dependa do outro. Os erros são ``WorkItemError``; cada consumidor
os traduz para o seu próprio tipo de erro de protocolo quando preciso.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

MAX_TEXT_CHARS = 20_000
MAX_LIST_ITEMS = 32
WORK_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
WORK_ITEM_PRIORITIES = frozenset({"low", "medium", "high", "critical"})
WORK_ITEM_TASK_TYPES = frozenset(
    {
        "architecture",
        "bug_investigation",
        "code_edit",
        "code_review",
        "documentation",
        "general",
        "test_execution",
    }
)


class WorkItemError(ValueError):
    """Work item ou grafo de work items inválido."""


@dataclass(frozen=True, slots=True)
class WorkItem:
    id: str
    title: str
    description: str
    task_type: str = "general"
    assigned_to: str | None = None
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    priority: str = "medium"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_work_items(items: Iterable[WorkItem]) -> None:
    """Valida IDs, referências e aciclicidade de um plano de work items."""
    work_items = tuple(items)
    by_id = {item.id: item for item in work_items}
    if len(by_id) != len(work_items):
        raise WorkItemError("work_items contem IDs duplicados")
    for item in work_items:
        unknown = set(item.dependencies) - set(by_id)
        if unknown:
            raise WorkItemError(
                f"work item {item.id} depende de IDs desconhecidos: {', '.join(sorted(unknown))}"
            )
        if item.id in item.dependencies:
            raise WorkItemError(f"work item {item.id} depende de si mesmo")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            raise WorkItemError("workflow contem ciclo de dependencias")
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in by_id[item_id].dependencies:
            visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in by_id:
        visit(item_id)


def parse_work_items(value: Any, *, max_items: int) -> tuple[WorkItem, ...]:
    """Converte uma lista JSON em work items, com limites e tipos estritos."""
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        raise WorkItemError("work_items deve ser uma lista")
    if len(value) > max_items:
        raise WorkItemError(f"work_items excede o limite de {max_items} itens")
    return tuple(
        parse_work_item(raw, default_id=f"T{index}")
        for index, raw in enumerate(value, start=1)
    )


def parse_work_item(raw: Any, *, default_id: str) -> WorkItem:
    """Converte um objeto JSON em ``WorkItem``."""
    if not isinstance(raw, dict):
        raise WorkItemError("cada work_item deve ser um objeto")
    item_id = text_field(raw.get("id") or default_id, "work_item.id", 64)
    if not WORK_ITEM_ID_RE.fullmatch(item_id):
        raise WorkItemError(f"work_item possui id invalido: {item_id}")
    title = text_field(raw.get("title"), "title", 500)
    if not title:
        raise WorkItemError("campo obrigatorio ausente: title")
    priority = text_field(raw.get("priority") or "medium", "work_item.priority", 20).lower()
    if priority not in WORK_ITEM_PRIORITIES:
        raise WorkItemError(f"prioridade invalida no work_item {item_id}: {priority}")
    task_type = text_field(raw.get("task_type") or "general", "work_item.task_type", 40).lower()
    if task_type not in WORK_ITEM_TASK_TYPES:
        raise WorkItemError(f"task_type invalido no work_item {item_id}: {task_type}")
    return WorkItem(
        id=item_id,
        title=title,
        description=text_field(raw.get("description") or title, "work_item.description", 8_000),
        task_type=task_type,
        assigned_to=text_field(raw.get("assigned_to"), "work_item.assigned_to", 200) or None,
        dependencies=text_list(raw.get("dependencies"), "work_item.dependencies"),
        acceptance_criteria=text_list(
            raw.get("acceptance_criteria"), "work_item.acceptance_criteria"
        ),
        priority=priority,
    )


def text_field(value: Any, field_name: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Texto opcional e aparado; ``None`` vira vazio."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise WorkItemError(f"{field_name} deve ser texto")
    text = value.strip()
    if len(text) > max_chars:
        raise WorkItemError(f"{field_name} excede o limite de {max_chars} caracteres")
    return text


def text_list(value: Any, field_name: str) -> tuple[str, ...]:
    """Lista de textos não vazios, com limite de itens."""
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        raise WorkItemError(f"{field_name} possui tipo invalido")
    if len(value) > MAX_LIST_ITEMS:
        raise WorkItemError(f"{field_name} excede o limite de {MAX_LIST_ITEMS} itens")
    return tuple(
        text
        for item in value
        if (text := text_field(item, f"{field_name}[]", 4_000))
    )

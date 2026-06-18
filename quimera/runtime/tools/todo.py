"""Componentes de `quimera.runtime.tools.todo`."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, asdict

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError
from ._helpers import resolve_current_job_id
from .base import ToolBase, ValidatableTool


@dataclass
class TodoItem:
    id: int
    job_id: int
    agent: str
    content: str
    status: str = "pending"
    priority: str = "medium"


class TodoRegistry:
    _todos: dict[int, list[TodoItem]] = {}
    _counter: dict[int, int] = {}
    _lock = threading.Lock()

    @classmethod
    def get_active(cls, job_id: int) -> list[TodoItem]:
        """Retorna apenas os itens com status 'in_progress'."""
        with cls._lock:
            return [t for t in cls._todos.get(job_id, []) if t.status == "in_progress"]

    @classmethod
    def get_active_as_dicts(cls, job_id: int) -> list[dict]:
        """Retorna itens 'in_progress' serializados como dicts (deep copy via asdict)."""
        with cls._lock:
            return [asdict(t) for t in cls._todos.get(job_id, []) if t.status == "in_progress"]

    @classmethod
    def cleanup(cls, job_id: int) -> None:
        """Remove todos os registros do job da memória."""
        with cls._lock:
            cls._todos.pop(job_id, None)
            cls._counter.pop(job_id, None)

    @classmethod
    def write(cls, job_id: int, agent: str, items_data: list[dict]) -> list[TodoItem]:
        """Cria ou atualiza itens do job."""
        with cls._lock:
            if job_id not in cls._todos:
                cls._todos[job_id] = []
                cls._counter[job_id] = 1
            results = []
            for data in items_data:
                existing_id = data.get("id")
                if existing_id is not None:
                    existing = next(
                        (t for t in cls._todos[job_id] if t.id == existing_id), None
                    )
                    if existing:
                        new_status = data.get("status", existing.status)
                        existing.content = data.get("content", existing.content)
                        existing.status = new_status
                        existing.priority = data.get("priority", existing.priority)
                        existing.agent = agent
                        if new_status == "in_progress":
                            for t in cls._todos[job_id]:
                                if t is not existing and t.status == "in_progress":
                                    t.status = "pending"
                        results.append(existing)
                        continue
                todo = TodoItem(
                    id=cls._counter[job_id],
                    job_id=job_id,
                    agent=agent,
                    content=data.get("content", ""),
                    status=data.get("status", "pending"),
                    priority=data.get("priority", "medium"),
                )
                cls._counter[job_id] += 1
                cls._todos[job_id].append(todo)
                if todo.status == "in_progress":
                    for t in cls._todos[job_id]:
                        if t is not todo and t.status == "in_progress":
                            t.status = "pending"
                results.append(todo)
        return results

    @classmethod
    def list(cls, job_id: int) -> list[TodoItem]:
        """Lista todos os itens do job."""
        with cls._lock:
            return list(cls._todos.get(job_id, []))


class TodoTools(ToolBase, tool_prefix="todo"):
    """Ferramentas de gerenciamento de todo list por job."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de TodoTools."""
        super().__init__(config)

    def _resolve_job_id(self) -> int | None:
        """Resolve o job_id corrente via variável de ambiente."""
        return resolve_current_job_id()

    def todo_write(self, call: ToolCall) -> ToolResult:
        """Cria ou atualiza itens na todo list do job corrente."""
        job_id = self._resolve_job_id()
        if job_id is None:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="QUIMERA_CURRENT_JOB_ID não definido",
            )
        items = call.arguments.get("todos", [])
        if not isinstance(items, list):
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="'todos' deve ser uma lista",
            )
        if not items:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="'todos' deve ser uma lista não vazia",
            )
        agent = call.arguments.get("agent", os.environ.get("CALLER_AGENT", "unknown"))
        try:
            results = TodoRegistry.write(job_id, agent, items)
            return ToolResult(
                ok=True,
                tool_name=call.name,
                content=json.dumps([asdict(t) for t in results], ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

    def todo_list(self, call: ToolCall) -> ToolResult:
        """Lista todos os itens do job corrente."""
        job_id = self._resolve_job_id()
        if job_id is None:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="QUIMERA_CURRENT_JOB_ID não definido",
            )
        try:
            todos = TodoRegistry.list(job_id)
            return ToolResult(
                ok=True,
                tool_name=call.name,
                content=json.dumps([asdict(t) for t in todos], ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))


class TodoToolsValidator(ValidatableTool):
    """Validação de policy para as ferramentas de todo."""

    _VALID_STATUSES = frozenset({"pending", "in_progress", "done", "cancelled"})
    _VALID_PRIORITIES = frozenset({"high", "medium", "low"})

    def _validate_todo_write(self, call: ToolCall) -> None:
        """Valida todo_write: lista não vazia com content e campos opcionais válidos."""
        items = call.arguments.get("todos")
        if not isinstance(items, list) or not items:
            raise ToolPolicyError("todo_write requer 'todos' como lista não vazia")
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ToolPolicyError(f"todo_write: item {i} deve ser um dicionário")
            if not item.get("content"):
                raise ToolPolicyError(f"todo_write: item {i} requer 'content' não vazio")
            status = item.get("status")
            if status and status not in self._VALID_STATUSES:
                raise ToolPolicyError(f"todo_write: status inválido '{status}' em item {i}")
            priority = item.get("priority")
            if priority and priority not in self._VALID_PRIORITIES:
                raise ToolPolicyError(f"todo_write: priority inválida '{priority}' em item {i}")

    def _validate_todo_list(self, call: ToolCall) -> None:
        """todo_list não exige argumentos."""


def register(registry, policy, config) -> None:
    """Registra todas as tools de todo no registry e a validação na policy."""
    todo_tools = TodoTools(config)
    todo_validator = TodoToolsValidator(config)
    tool_names = [name for name in dir(TodoTools) if name.startswith("todo_")]
    for name in tool_names:
        registry.register(name, getattr(todo_tools, name))
    policy.register_tool_validator(tool_names, todo_validator)

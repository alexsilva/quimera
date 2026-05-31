"""Componentes de `quimera.runtime.tools.todo`."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, asdict

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ._helpers import resolve_current_job_id


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
        with cls._lock:
            return list(cls._todos.get(job_id, []))


class TodoTools:
    def __init__(self, config: ToolRuntimeConfig) -> None:
        self.config = config

    def _resolve_job_id(self) -> int | None:
        return resolve_current_job_id()

    def todo_write(self, call: ToolCall) -> ToolResult:
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

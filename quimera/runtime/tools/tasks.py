"""Componentes de `quimera.runtime.tools.tasks`."""
from __future__ import annotations

import json
import re
from typing import Protocol

from ..approval import TrustedToolExecutionContext
from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError
from ...tasks.api import (
    list_tasks as _list_tasks,
    list_jobs as _list_jobs,
    get_job as _get_job,
)
from ._helpers import resolve_current_job_id
from .base import ToolBase, ValidatableTool

_TASK_TOOL_NAMES = ["tasks", "list_tasks", "list_jobs", "get_job"]


class _TaskCreationReceipt(Protocol):
    """Contrato mínimo do recibo retornado pelo domínio de tasks."""

    def as_dict(self) -> dict:
        """Serializa o recibo."""
        ...


class _CreateTaskFn(Protocol):
    """Contrato do serviço de criação injetado pelo bootstrap."""

    def __call__(
        self,
        description: str,
        *,
        requested_by: str,
    ) -> _TaskCreationReceipt:
        """Cria uma task e retorna seu recibo."""
        ...


class TaskTools(ToolBase):
    """Ferramentas de criação e consulta a tasks e jobs."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de TaskTools."""
        super().__init__(config)
        self._create_task_fn: _CreateTaskFn | None = None

    def set_create_task_fn(self, fn: _CreateTaskFn | None) -> None:
        """Injeta o serviço canônico que cria tasks da sessão."""
        self._create_task_fn = fn

    def is_tasks_available(self) -> bool:
        """Indica se a criação de tasks está ligada ao serviço da aplicação."""
        return callable(self._create_task_fn)

    @staticmethod
    def _get_calling_agent(call: ToolCall) -> str | None:
        """Obtém a identidade confiável do agente solicitante."""
        trusted = call.metadata.get("trusted_context")
        if isinstance(trusted, TrustedToolExecutionContext):
            return trusted.agent_name
        if isinstance(trusted, dict):
            name = trusted.get("agent_name")
            return str(name).strip() if name else None
        return None

    def tasks(self, call: ToolCall) -> ToolResult:
        """Cria uma task pelo mesmo protocolo usado pelo comando /task."""
        if not callable(self._create_task_fn):
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="tasks is unavailable outside an active Quimera session",
            )
        requested_by = self._get_calling_agent(call)
        if not requested_by:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="tasks requires a trusted agent identity",
            )
        description = str(call.arguments.get("description") or "").strip()
        try:
            receipt = self._create_task_fn(
                description,
                requested_by=requested_by,
            )
            data = receipt.as_dict()
            data["monitor_with"] = {
                "tool": "list_tasks",
                "arguments": {"id": data["task_id"]},
            }
            return ToolResult(
                ok=True,
                tool_name=call.name,
                content=json.dumps(data, ensure_ascii=False),
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

    def _resolve_job_id(self, raw_job_id, *, allow_recent_fallback: bool = False) -> int | None:
        """Resolve job id."""
        job_id = raw_job_id
        if job_id is None:
            job_id = resolve_current_job_id()
        if job_id is None and allow_recent_fallback:
            try:
                recent_jobs = _list_jobs({"status": "planning"}, db_path=self.config.db_path)
                if not recent_jobs:
                    recent_jobs = _list_jobs({"status": "active"}, db_path=self.config.db_path)
                if recent_jobs:
                    job_id = recent_jobs[-1]["id"]
            except Exception:
                return None
        return job_id

    @staticmethod
    def _normalize_text(value: str) -> str:
        """Normaliza text."""
        return re.sub(r"\s+", " ", value.strip().lower())

    def _build_filters(self, arguments: dict) -> dict:
        """Monta filters."""
        filt = dict(arguments.get("filters", {}) or {})
        for key in ("job_id", "status", "assigned_to", "id"):
            value = arguments.get(key)
            if value is not None:
                filt[key] = value
        return filt

    def _find_duplicate_task(self, job_id: int, description: str) -> dict | None:
        """Executa find duplicate task."""
        normalized_description = self._normalize_text(description)
        if not normalized_description:
            return None
        open_statuses = ("proposed", "approved", "in_progress")
        for status in open_statuses:
            tasks = _list_tasks({"job_id": job_id, "status": status}, db_path=self.config.db_path)
            for task in tasks:
                if self._normalize_text(task["description"]) == normalized_description:
                    return task
        return None

    def list_tasks(self, call: ToolCall) -> ToolResult:
        """Lista tasks."""
        filt = self._build_filters(call.arguments)
        try:
            tasks = _list_tasks(filt, db_path=self.config.db_path)
            max_results = int(call.arguments.get("max_results", self.config.max_task_results))
            truncated = len(tasks) > max_results
            tasks = tasks[:max_results]
            return ToolResult(
                ok=True,
                tool_name=call.name,
                content=json.dumps(tasks),
                truncated=truncated,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

    def list_jobs(self, call: ToolCall) -> ToolResult:
        """Lista jobs."""
        filt = dict(call.arguments.get("filters", {}) or {})
        for key in ("status", "created_by"):
            value = call.arguments.get(key)
            if value is not None:
                filt[key] = value
        try:
            jobs = _list_jobs(filt, db_path=self.config.db_path)
            return ToolResult(ok=True, tool_name=call.name, content=json.dumps(jobs))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

    def get_job(self, call: ToolCall) -> ToolResult:
        """Retorna job."""
        job_id = self._resolve_job_id(call.arguments.get("job_id"), allow_recent_fallback=True)
        if job_id is None:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="job_id is required (set QUIMERA_CURRENT_JOB_ID or create a job first)",
            )
        try:
            job = _get_job(job_id, db_path=self.config.db_path)
            return ToolResult(
                ok=True,
                tool_name=call.name,
                content=json.dumps(job) if job is not None else "null",
                data={"job": job},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))


class TaskToolsValidator(ValidatableTool):
    """Validação de policy para as ferramentas de tasks."""

    def _validate_tasks(self, call: ToolCall) -> None:
        """Exige uma descrição textual não vazia para criar a task."""
        description = call.arguments.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ToolPolicyError("tasks requer 'description' não vazia")

    def _validate_list_tasks(self, call: ToolCall) -> None:
        """Exige ao menos um filtro para evitar DoS por listagem sem limites."""
        filt = call.arguments.get("filters") or {}
        has_top_level_filter = any(
            call.arguments.get(k) is not None
            for k in ("job_id", "status", "assigned_to", "id")
        )
        has_dict_filter = isinstance(filt, dict) and bool(filt)
        if not has_top_level_filter and not has_dict_filter:
            raise ToolPolicyError(
                "list_tasks exige ao menos um filtro (job_id, status, assigned_to, id ou filters)"
            )

    def _validate_list_jobs(self, call: ToolCall) -> None:
        """list_jobs não exige filtros obrigatórios."""

    def _validate_get_job(self, call: ToolCall) -> None:
        """get_job não exige job_id obrigatório (usa fallback)."""


def register(registry, policy, config) -> TaskTools:
    """Registra todas as tools de tasks no registry e a validação na policy."""
    task_tools = TaskTools(config)
    task_validator = TaskToolsValidator(config)
    for name in _TASK_TOOL_NAMES:
        registry.register(name, getattr(task_tools, name))
    policy.register_tool_validator(_TASK_TOOL_NAMES, task_validator)
    return task_tools

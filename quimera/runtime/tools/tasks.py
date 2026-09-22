"""Componentes de `quimera.runtime.tools.tasks`."""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Protocol

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

logger = logging.getLogger(__name__)

_TASK_TOOL_NAMES = ["tasks", "list_tasks", "list_jobs", "get_job"]
_LIVE_THINKING_MAX_CHARS = 400


def _live_excerpt(value) -> str:
    """Mantém a cauda do texto ao vivo dentro do limite publicado pela tool."""
    text = str(value or "")
    if len(text) > _LIVE_THINKING_MAX_CHARS:
        return "…" + text[-_LIVE_THINKING_MAX_CHARS:]
    return text


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
        self._delegation_run_lookup: Callable[[str], dict | None] | None = None

    def set_create_task_fn(self, fn: _CreateTaskFn | None) -> None:
        """Injeta o serviço canônico que cria tasks da sessão."""
        self._create_task_fn = fn

    def set_delegation_run_lookup(self, fn: Callable[[str], dict | None] | None) -> None:
        """Injeta lookup do run ao vivo de uma delegação por delegation_id.

        Assinatura esperada: fn(delegation_id) -> {agent, status, last_thinking,
        last_activity?, updated_seconds_ago} | None. O lookup consulta o registry
        de runs em memória do processo — visibilidade cross-processo fica fora
        do escopo.
        """
        self._delegation_run_lookup = fn

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
                recent_jobs = _list_jobs({"status": "planning"}, db_path=self.workspace.tasks_db)
                if not recent_jobs:
                    recent_jobs = _list_jobs({"status": "active"}, db_path=self.workspace.tasks_db)
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
            tasks = _list_tasks({"job_id": job_id, "status": status}, db_path=self.workspace.tasks_db)
            for task in tasks:
                if self._normalize_text(task["description"]) == normalized_description:
                    return task
        return None

    def _attach_live_delegation(self, task: dict) -> dict:
        """Anexa o estado ao vivo (thinking) a tasks de delegação em execução.

        Best-effort: qualquer falha no lookup deixa a task intocada — a
        listagem nunca quebra por causa do enriquecimento.
        """
        if not callable(self._delegation_run_lookup):
            return task
        if task.get("origin") != "delegate" or task.get("status") != "in_progress":
            return task
        try:
            steps = json.loads(task.get("body") or "[]")
        except (TypeError, ValueError):
            return task
        if not isinstance(steps, list):
            return task
        live: list[dict] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            delegation_id = str(step.get("delegation_id") or "")
            if not delegation_id:
                continue
            try:
                view = self._delegation_run_lookup(delegation_id)
            except Exception:
                logger.warning(
                    "list_tasks: delegation run lookup failed for %s", delegation_id, exc_info=True,
                )
                continue
            if not isinstance(view, dict):
                continue
            thinking = _live_excerpt(view.get("last_thinking"))
            item = {
                "delegation_id": delegation_id,
                "agent": str(view.get("agent") or step.get("target_agent") or ""),
                "status": str(view.get("status") or ""),
                "last_thinking": thinking,
                "updated_seconds_ago": view.get("updated_seconds_ago"),
            }
            activity = _live_excerpt(view.get("last_activity"))
            if activity:
                item["last_activity"] = activity
            live.append(item)
        if not live:
            return task
        enriched = dict(task)
        enriched["live"] = live
        return enriched

    def list_tasks(self, call: ToolCall) -> ToolResult:
        """Lista tasks."""
        filt = self._build_filters(call.arguments)
        try:
            tasks = _list_tasks(filt, db_path=self.workspace.tasks_db)
            max_results = int(call.arguments.get("max_results", self.config.max_task_results))
            truncated = len(tasks) > max_results
            tasks = [self._attach_live_delegation(task) for task in tasks[:max_results]]
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
            jobs = _list_jobs(filt, db_path=self.workspace.tasks_db)
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
            job = _get_job(job_id, db_path=self.workspace.tasks_db)
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

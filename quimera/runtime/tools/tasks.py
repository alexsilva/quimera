"""Componentes de `quimera.runtime.tools.tasks`.

Família ``tasks_*`` — verbos sobre o recurso task:

- ``tasks_create``: cria uma task pelo mesmo fluxo do comando ``/task`` (o
  roteador escolhe o executor e outro agente revisa);
- ``tasks_delegate``: delegação a um alvo explícito, sem fila nem revisão
  (implementada em ``delegate.py``);
- ``tasks_list``: lista tasks com filtros;
- ``tasks_cancel``: cancela uma delegação em execução.

Jobs são outro substantivo e têm família própria ``jobs_*`` (``jobs_list``,
``jobs_get``), implementada aqui por ``JobTools``.
"""
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

_TASK_TOOL_NAMES = ["tasks_create", "tasks_list", "tasks_cancel"]
_JOB_TOOL_NAMES = ["jobs_list", "jobs_get"]
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


class TaskTools(
    ToolBase,
    tool_prefix="tasks",
    tool_public_methods=(
        "set_create_task_fn",
        "set_delegation_run_lookup",
        "is_tasks_available",
    ),
):
    """Ferramentas ``tasks_create``, ``tasks_list`` e ``tasks_cancel``."""

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

    @staticmethod
    def _get_transport(call: ToolCall) -> str:
        """Extrai o transporte do contexto confiável no metadata do ToolCall."""
        ctx = call.metadata.get("trusted_context", {})
        if isinstance(ctx, TrustedToolExecutionContext):
            return ctx.transport
        if isinstance(ctx, dict):
            return str(ctx.get("transport", "native_tool_call"))
        return "native_tool_call"

    def tasks_create(self, call: ToolCall) -> ToolResult:
        """Cria uma task pelo mesmo fluxo do comando /task.

        O roteador escolhe o executor pelo effective_score e outro agente
        revisa o resultado. Retorna o recibo com task_id para acompanhamento
        via ``tasks_list``.
        """
        if not callable(self._create_task_fn):
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="tasks_create is unavailable outside an active Quimera session",
            )
        requested_by = self._get_calling_agent(call)
        if not requested_by:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="tasks_create requires a trusted agent identity",
            )
        description = str(call.arguments.get("description") or "").strip()
        try:
            receipt = self._create_task_fn(
                description,
                requested_by=requested_by,
            )
            data = receipt.as_dict()
            data["monitor_with"] = {
                "tool": "tasks_list",
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

    @staticmethod
    def _normalize_agent_identity(agent_name: str | None) -> str:
        """Normaliza o nome de um agente para comparação (mesma regra do delegate)."""
        if agent_name is None:
            return ""
        return str(agent_name).strip().lower().lstrip("/")

    def tasks_cancel(self, call: ToolCall) -> ToolResult:
        """Cancela uma delegação em execução.

        Permitido ao agente que delegou (``source_agent``) ou ao humano. O alvo
        é terminado e a task transiciona para o status ``cancelled`` com recibo
        do que aconteceu. A checagem de dono é proteção contra acidente e bug,
        não barreira contra agente hostil: o token MCP é um por sessão.
        """
        from .delegate import get_active_delegation

        task_id = call.arguments.get("task_id")
        if not isinstance(task_id, int) or isinstance(task_id, bool):
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="tasks_cancel requer 'task_id' inteiro",
            )
        requested_by = self._normalize_agent_identity(self._get_calling_agent(call))
        entry = get_active_delegation(task_id)
        if entry is None:
            status_hint = ""
            try:
                rows = _list_tasks({"id": task_id}, db_path=self.workspace.tasks_db)
                if rows:
                    status_hint = f" (status atual: {rows[0].get('status')})"
            except Exception:  # noqa: BLE001 - hint é best-effort
                pass
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=(
                    f"task {task_id} não é uma delegação em execução neste "
                    f"processo{status_hint}"
                ),
            )
        # Quem pode cancelar: o agente que delegou, ou o humano. Agente
        # identificado precisa ser exatamente o source_agent — inclusive
        # quando a delegação não tem dono registrado (nesse caso só o humano
        # cancela). Ausência de identidade só conta como humano fora do HTTP:
        # no transporte HTTP o nome vem de header do cliente, e omiti-lo não
        # pode conceder privilégio.
        if requested_by:
            if requested_by != entry.source_agent:
                owner = entry.source_agent or "o humano"
                return ToolResult(
                    ok=False,
                    tool_name=call.name,
                    error=(
                        f"apenas quem delegou ({owner}) pode cancelar a "
                        f"task {task_id}"
                    ),
                )
        elif self._get_transport(call) == "http_mcp":
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=(
                    "cancelamento via HTTP MCP requer identidade de agente "
                    "(trusted_context.agent_name)"
                ),
            )
        reason = call.arguments.get("reason")
        reason = str(reason).strip() if isinstance(reason, str) else None
        outcome = entry.request_cancel(requested_by or "humano", reason)
        if outcome == "finalized":
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=(
                    f"task {task_id} já foi finalizada; nada a cancelar "
                    "(consulte tasks_list para o desfecho)"
                ),
            )
        payload = {
            "task_id": task_id,
            "status": "cancelling" if outcome == "cancelled" else "already_cancelling",
            "cancelled_by": entry.cancelled_by,
            "targets": list(entry.targets),
            "hint": (
                f"o cancelamento é assíncrono; acompanhe com tasks_list "
                f"{{\"id\": {task_id}}} até o status 'cancelled'"
            ),
        }
        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=json.dumps(payload, ensure_ascii=False),
            data=payload,
        )

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
                    "tasks_list: delegation run lookup failed for %s", delegation_id, exc_info=True,
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

    def tasks_list(self, call: ToolCall) -> ToolResult:
        """Lista tasks com filtros (job_id, status, assigned_to, id ou filters)."""
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


class JobTools(ToolBase, tool_prefix="jobs"):
    """Ferramentas ``jobs_list`` e ``jobs_get``."""

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

    def jobs_list(self, call: ToolCall) -> ToolResult:
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

    def jobs_get(self, call: ToolCall) -> ToolResult:
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
    """Validação de policy para as famílias ``tasks_*`` e ``jobs_*``."""

    def _validate_tasks_create(self, call: ToolCall) -> None:
        """Exige description não vazia."""
        description = call.arguments.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ToolPolicyError("tasks_create requer 'description' não vazia")

    def _validate_tasks_cancel(self, call: ToolCall) -> None:
        """Exige task_id inteiro."""
        task_id = call.arguments.get("task_id")
        if not isinstance(task_id, int) or isinstance(task_id, bool):
            raise ToolPolicyError("tasks_cancel requer 'task_id' inteiro")

    def _validate_tasks_list(self, call: ToolCall) -> None:
        """Exige ao menos um filtro para evitar DoS por listagem sem limites."""
        filt = call.arguments.get("filters") or {}
        has_top_level_filter = any(
            call.arguments.get(k) is not None
            for k in ("job_id", "status", "assigned_to", "id")
        )
        has_dict_filter = isinstance(filt, dict) and bool(filt)
        if not has_top_level_filter and not has_dict_filter:
            raise ToolPolicyError(
                "tasks_list exige ao menos um filtro (job_id, status, assigned_to, id ou filters)"
            )

    def _validate_jobs_list(self, call: ToolCall) -> None:
        """jobs_list não exige filtros obrigatórios."""

    def _validate_jobs_get(self, call: ToolCall) -> None:
        """jobs_get não exige job_id obrigatório (usa fallback)."""


def register(registry, policy, config) -> TaskTools:
    """Registra as famílias tasks_*/jobs_* no registry e a validação na policy.

    Retorna a instância de TaskTools para o executor injetar o serviço de
    criação e o lookup de runs ao vivo.
    """
    task_tools = TaskTools(config)
    job_tools = JobTools(config)
    validator = TaskToolsValidator(config)
    for name in _TASK_TOOL_NAMES:
        registry.register(name, getattr(task_tools, name))
    for name in _JOB_TOOL_NAMES:
        registry.register(name, getattr(job_tools, name))
    policy.register_tool_validator([*_TASK_TOOL_NAMES, *_JOB_TOOL_NAMES], validator)
    return task_tools

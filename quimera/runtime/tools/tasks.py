"""Componentes de `quimera.runtime.tools.tasks`."""
from __future__ import annotations

import json
import os
import re

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..tasks import (
    list_tasks as _list_tasks,
    list_jobs as _list_jobs,
    get_job as _get_job,
)


class TaskTools:
    """Implementa `TaskTools`."""
    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de TaskTools."""
        self.config = config

    def _resolve_job_id(self, raw_job_id, *, allow_recent_fallback: bool = False) -> int | None:
        """Resolve job id."""
        job_id = raw_job_id
        if job_id is None:
            env_val = os.environ.get("QUIMERA_CURRENT_JOB_ID")
            if env_val is not None:
                try:
                    job_id = int(env_val)
                except ValueError:
                    job_id = None
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
            return ToolResult(ok=True, tool_name=call.name, content=json.dumps(tasks))
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
            return ToolResult(ok=False, tool_name=call.name, error="job_id is required (set QUIMERA_CURRENT_JOB_ID or create a job first)")
        try:
            job = _get_job(job_id, db_path=self.config.db_path)
            return ToolResult(ok=True, tool_name=call.name, content=json.dumps(job) if job is not None else "null", data={"job": job})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

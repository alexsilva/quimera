from __future__ import annotations

import json
from typing import Any

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..tasks import (
    propose_task as _propose_task,
    list_tasks as _list_tasks,
    list_jobs as _list_jobs,
    get_job as _get_job,
    complete_task as _complete_task,
    fail_task as _fail_task,
)


class TaskTools:
    def __init__(self, config: ToolRuntimeConfig) -> None:
        self.config = config

    def propose_task(self, call: ToolCall) -> ToolResult:
        job_id = call.arguments["job_id"]
        description = call.arguments["description"]
        priority = call.arguments.get("priority", "medium")
        created_by = call.arguments.get("created_by")
        notes = call.arguments.get("notes")
        source_context = call.arguments.get("source_context")
        body = call.arguments.get("body")
        try:
            tid = _propose_task(job_id, description, priority=priority, created_by=created_by, notes=notes, source_context=source_context, body=body, db_path=None)
            return ToolResult(ok=True, tool_name=call.name, content=str(tid), data={"task_id": tid})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

    def list_tasks(self, call: ToolCall) -> ToolResult:
        filt = call.arguments.get("filters", {})
        try:
            tasks = _list_tasks(filt, db_path=None)
            return ToolResult(ok=True, tool_name=call.name, content=json.dumps(tasks))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

    def list_jobs(self, call: ToolCall) -> ToolResult:
        filt = call.arguments.get("filters", {})
        try:
            jobs = _list_jobs(filt, db_path=None)
            return ToolResult(ok=True, tool_name=call.name, content=json.dumps(jobs))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

    def get_job(self, call: ToolCall) -> ToolResult:
        job_id = call.arguments["job_id"]
        try:
            job = _get_job(job_id, db_path=None)
            return ToolResult(ok=True, tool_name=call.name, content=json.dumps(job) if job is not None else "null", data={"job": job})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

    def complete_task(self, call: ToolCall) -> ToolResult:
        task_id = call.arguments["task_id"]
        result = call.arguments.get("result")
        try:
            ok = _complete_task(task_id, result=result, db_path=None)
            return ToolResult(ok=bool(ok), tool_name=call.name, content="completed" if ok else "failed to complete")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

    def fail_task(self, call: ToolCall) -> ToolResult:
        task_id = call.arguments["task_id"]
        reason = call.arguments.get("reason")
        try:
            ok = _fail_task(task_id, reason=reason, db_path=None)
            return ToolResult(ok=bool(ok), tool_name=call.name, content="failed" if ok else "could not fail task")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

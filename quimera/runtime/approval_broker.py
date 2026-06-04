"""Approval Broker central para governar ferramentas MCP e chamadas paralelas."""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator

from .approve_summary import ApproveSummary
from .models import ToolCall
from .policy import PathPermissionError, is_path_inside


class RiskLevel(str, Enum):
    """Classificação de risco usada pelo broker."""

    READ = "read"
    NETWORK = "network"
    DELEGATION = "delegation"
    WRITE = "write"
    SHELL = "shell"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Metadados rastreáveis de uma chamada de ferramenta."""

    agent_name: str | None = None
    parent_agent: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    job_id: str | int | None = None
    task_id: str | int | None = None
    transport: str = "native_tool_call"
    approval_scope_id: str | None = None

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> "ToolExecutionContext":
        meta = metadata or {}
        run_id = meta.get("run_id") or meta.get("task_id") or f"thread:{threading.get_ident()}"
        return cls(
            agent_name=_clean(meta.get("agent_name")),
            parent_agent=_clean(meta.get("parent_agent")),
            run_id=str(run_id) if run_id is not None else None,
            parent_run_id=str(meta.get("parent_run_id")) if meta.get("parent_run_id") is not None else None,
            job_id=meta.get("job_id"),
            task_id=meta.get("task_id"),
            transport=str(meta.get("transport") or "native_tool_call"),
            approval_scope_id=_clean(meta.get("approval_scope_id")),
        )

    @property
    def scope_key(self) -> str:
        return self.approval_scope_id or (f"run:{self.run_id}" if self.run_id else f"thread:{threading.get_ident()}")


@dataclass(slots=True)
class ApprovalRequest:
    """Pedido de aprovação auditable."""

    id: str
    tool_name: str
    arguments: dict[str, Any]
    risk: RiskLevel
    context: ToolExecutionContext
    summary: str
    path: str | None = None
    command: str | None = None
    reason: str | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def equivalence_key(self) -> tuple[Any, ...]:
        return (
            self.context.scope_key,
            self.tool_name,
            self.risk.value,
            self.path or "",
            self.command or "",
            self.context.agent_name or "",
        )

    @property
    def route(self) -> str:
        parts = [p for p in (self.context.parent_agent, self.context.agent_name) if p]
        prefix = " → ".join(parts)
        target = self.arguments.get("agent_name") if self.tool_name == "call_agent" else None
        op = f"{self.tool_name}({target})" if target else self.tool_name
        tail = self.path or self.command or ""
        rendered = f"{op} {tail}".strip()
        return f"{prefix} → {rendered}" if prefix else rendered


@dataclass(slots=True)
class ApprovalScope:
    """Escopo temporário de aprovação controlada."""

    id: str
    run_id: str | None = None
    tool_name: str | None = None
    agent_name: str | None = None
    path: str | None = None
    risk: RiskLevel | None = None
    expires_at: float | None = None
    remaining_uses: int | None = None
    approve_all_in_run: bool = False

    def matches(self, request: ApprovalRequest) -> bool:
        now = time.time()
        if self.expires_at is not None and self.expires_at <= now:
            return False
        if self.run_id is not None and self.run_id != request.context.run_id:
            return False
        if self.tool_name is not None and self.tool_name != request.tool_name:
            return False
        if self.agent_name is not None and self.agent_name != request.context.agent_name:
            return False
        if self.path is not None and self.path != request.path:
            return False
        if self.risk is not None and self.risk != request.risk:
            return False
        if self.approve_all_in_run and self.run_id is None:
            return False
        return True

    def consume(self) -> None:
        if self.remaining_uses is not None:
            self.remaining_uses -= 1

    @property
    def exhausted(self) -> bool:
        return self.remaining_uses is not None and self.remaining_uses <= 0


class ApprovalBroker:
    """Governança central de approval, auditoria e serialização de ferramentas."""

    _READ_TOOLS = {"list_files", "read_file", "grep_search", "list_tasks", "list_jobs", "get_job", "todo_list"}
    _NETWORK_TOOLS = {"web_search", "web_fetch"}
    _WRITE_TOOLS = {"write_file", "apply_patch", "todo_write", "write_stdin", "close_command_session"}
    _SHELL_TOOLS = {"run_shell", "run_shell_command", "exec_command"}
    _DESTRUCTIVE_TOOLS = {"remove_file"}
    _PATH_TOOLS = {"read_file", "list_files", "grep_search", "write_file", "remove_file"}
    _DELEGATION_BUDGET_DEFAULT = 8

    def __init__(self, config, approval_handler) -> None:
        self.config = config
        self._approval_handler = approval_handler
        self._prompt_lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._scope_lock = threading.Lock()
        self._lock_table_lock = threading.Lock()
        self._scopes: list[ApprovalScope] = []
        self.audit_log: list[dict[str, Any]] = []
        self._delegation_budget: dict[str, int] = {}
        self._locks: dict[str, threading.RLock] = {}

    def build_context(self, call: ToolCall) -> ToolExecutionContext:
        return ToolExecutionContext.from_metadata(call.metadata)

    def classify(self, call: ToolCall) -> RiskLevel:
        if call.name == "call_agent":
            return RiskLevel.DELEGATION
        if call.name in self._DESTRUCTIVE_TOOLS:
            return RiskLevel.DESTRUCTIVE
        if call.name in self._SHELL_TOOLS:
            return RiskLevel.SHELL
        if call.name in self._WRITE_TOOLS:
            return RiskLevel.WRITE
        if call.name in self._NETWORK_TOOLS:
            return RiskLevel.NETWORK
        return RiskLevel.READ

    def create_request(
        self,
        call: ToolCall,
        *,
        permission_error: PathPermissionError | None = None,
        reason: str | None = None,
    ) -> ApprovalRequest:
        context = self.build_context(call)
        risk = self.classify(call)
        path = self._extract_path(call, permission_error=permission_error)
        command = _extract_command(call)
        effective_reason = reason or ("path_permission" if permission_error is not None else None)
        summary = self._summary(call, risk, context, path=path, command=command, permission_error=permission_error, reason=effective_reason)
        return ApprovalRequest(
            id=str(uuid.uuid4()),
            tool_name=call.name,
            arguments=dict(call.arguments),
            risk=risk,
            context=context,
            summary=summary,
            path=path,
            command=command,
            reason=effective_reason,
        )

    def should_request_approval(
        self,
        call: ToolCall,
        *,
        needs_policy_approval: bool,
        permission_error: PathPermissionError | None = None,
    ) -> bool:
        request = self.create_request(call, permission_error=permission_error)
        return not self._can_auto_approve(request, needs_policy_approval=needs_policy_approval, consume_budget=False)

    def approve(
        self,
        call: ToolCall,
        *,
        needs_policy_approval: bool,
        permission_error: PathPermissionError | None = None,
    ) -> bool:
        request = self.create_request(call, permission_error=permission_error)
        self._record("request", request, decision=None)

        if self._can_auto_approve(request, needs_policy_approval=needs_policy_approval, consume_budget=True):
            self._record("auto_approved", request, decision=True)
            return True

        with self._prompt_lock:
            approved = bool(self._approval_handler.approve(tool_name=call.name, summary=request.summary))
        self._record("approved" if approved else "denied", request, decision=approved)
        if approved:
            self._maybe_import_legacy_scope(request)
        return approved

    @contextmanager
    def execution_guard(self, call: ToolCall) -> Iterator[None]:
        key = self._serialization_key(call)
        if key is None:
            yield
            return
        with self._lock_table_lock:
            lock = self._locks.setdefault(key, threading.RLock())
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def approve_scope(self, scope: ApprovalScope) -> None:
        with self._scope_lock:
            self._scopes.append(scope)

    def approve_equivalent(self, request: ApprovalRequest, *, ttl_seconds: float = 300, uses: int | None = None) -> ApprovalScope:
        scope = ApprovalScope(
            id=f"equiv:{request.id}",
            run_id=request.context.run_id,
            tool_name=request.tool_name,
            agent_name=request.context.agent_name,
            path=request.path,
            risk=request.risk,
            expires_at=time.time() + ttl_seconds,
            remaining_uses=uses,
        )
        self.approve_scope(scope)
        return scope

    def _can_auto_approve(self, request: ApprovalRequest, *, needs_policy_approval: bool, consume_budget: bool) -> bool:
        scope = self._matching_scope(request)
        if scope is not None:
            if consume_budget:
                scope.consume()
                self._prune_scopes()
            return True

        if request.risk == RiskLevel.READ and request.reason is None:
            return True
        if request.risk == RiskLevel.NETWORK and request.reason is None:
            return True
        if request.risk == RiskLevel.DELEGATION:
            if request.context.transport == "http_mcp" and not request.arguments.get("allowlisted"):
                return False
            if self._delegation_within_budget(request, consume=consume_budget):
                return True
            return False
        return not needs_policy_approval and request.reason is None

    def _delegation_within_budget(self, request: ApprovalRequest, *, consume: bool) -> bool:
        key = request.context.scope_key
        used = self._delegation_budget.get(key, 0)
        budget = _as_int(request.arguments.get("approval_budget"), self._DELEGATION_BUDGET_DEFAULT)
        if used >= budget:
            return False
        if consume:
            self._delegation_budget[key] = used + 1
        return True

    def _matching_scope(self, request: ApprovalRequest) -> ApprovalScope | None:
        with self._scope_lock:
            self._prune_scopes_locked()
            for scope in list(self._scopes):
                if scope.matches(request):
                    return scope
        return None

    def _prune_scopes(self) -> None:
        with self._scope_lock:
            self._prune_scopes_locked()

    def _prune_scopes_locked(self) -> None:
        now = time.time()
        self._scopes = [s for s in self._scopes if not s.exhausted and (s.expires_at is None or s.expires_at > now)]

    def _maybe_import_legacy_scope(self, request: ApprovalRequest) -> None:
        getter = getattr(self._approval_handler, "get_thread_approval_scope", None)
        if not callable(getter):
            return
        try:
            key = getter()
        except Exception:
            return
        if not key:
            return
        self.approve_scope(ApprovalScope(id=str(key), run_id=request.context.run_id, approve_all_in_run=True))

    def _serialization_key(self, call: ToolCall) -> str | None:
        if call.name in {"run_shell", "run_shell_command", "exec_command"}:
            return f"workspace:{self.config.workspace_root}"
        if call.name in {"write_file", "remove_file"}:
            path = self._extract_path(call)
            return f"path:{path}" if path else f"workspace:{self.config.workspace_root}"
        if call.name == "apply_patch":
            paths = self._extract_patch_paths(str(call.arguments.get("patch", "")))
            if len(paths) == 1:
                return f"path:{paths[0]}"
            if paths:
                return "paths:" + "|".join(sorted(paths))
            return f"workspace:{self.config.workspace_root}"
        return None

    def _extract_path(self, call: ToolCall, *, permission_error: PathPermissionError | None = None) -> str | None:
        if permission_error is not None:
            return str(permission_error.resolved_path)
        if call.name == "apply_patch":
            paths = self._extract_patch_paths(str(call.arguments.get("patch", "")))
            return paths[0] if len(paths) == 1 else (", ".join(paths) if paths else None)
        if call.name not in self._PATH_TOOLS:
            return None
        raw = call.arguments.get("path", ".")
        try:
            normalized = str(raw).lstrip("/") or "."
            path = (self.config.workspace_root / normalized).resolve()
        except Exception:
            return str(raw)
        return str(path)

    def _extract_patch_paths(self, patch: str) -> list[str]:
        paths: list[str] = []
        for line in patch.splitlines():
            if not (line.startswith("+++ ") or line.startswith("--- ")):
                continue
            raw = line[4:].strip().split("\t", 1)[0]
            if raw == "/dev/null":
                continue
            if raw.startswith("a/") or raw.startswith("b/"):
                raw = raw[2:]
            try:
                path = (self.config.workspace_root / raw.lstrip("/")).resolve()
            except Exception:
                continue
            if is_path_inside(path, self.config.workspace_root):
                text = str(path)
                if text not in paths:
                    paths.append(text)
        return paths

    def _summary(self, call: ToolCall, risk: RiskLevel, context: ToolExecutionContext, *, path: str | None, command: str | None, permission_error: PathPermissionError | None, reason: str | None) -> str:
        details = [f"origem: {self.create_route(call, context, path=path, command=command)}", f"risco: {risk.value}"]
        if path:
            details.append(f"path: {path}")
        if command:
            details.append(f"comando: {command}")
        if reason:
            details.append(f"justificativa: {reason}")
        body = f"Permissão necessária para acessar: {permission_error.resolved_path}" if permission_error else ApproveSummary.build(call.name, call.arguments)
        details.append(body)
        return "\n".join(details)

    def create_route(self, call: ToolCall, context: ToolExecutionContext, *, path: str | None = None, command: str | None = None) -> str:
        req = ApprovalRequest("route", call.name, call.arguments, self.classify(call), context, "", path=path, command=command)
        return req.route

    def _record(self, event: str, request: ApprovalRequest, decision: bool | None) -> None:
        with self._audit_lock:
            self.audit_log.append({
                "event": event,
                "request_id": request.id,
                "tool_name": request.tool_name,
                "risk": request.risk.value,
                "agent_name": request.context.agent_name,
                "parent_agent": request.context.parent_agent,
                "run_id": request.context.run_id,
                "parent_run_id": request.context.parent_run_id,
                "transport": request.context.transport,
                "path": request.path,
                "command": request.command,
                "decision": decision,
                "created_at": request.created_at,
                "recorded_at": time.time(),
            })


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_command(call: ToolCall) -> str | None:
    if call.name in {"run_shell", "run_shell_command"}:
        return str(call.arguments.get("command", "")).strip() or None
    if call.name == "exec_command":
        return str(call.arguments.get("cmd") or call.arguments.get("command") or "").strip() or None
    return None

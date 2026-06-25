"""Approval Broker central para governar ferramentas MCP e chamadas paralelas."""
from __future__ import annotations

import threading
import time
import uuid
import inspect
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator

from .models import ToolCall
from .policy import PathPermissionError, is_path_inside
from .tool_preview import ToolPreview


class RiskLevel(str, Enum):
    """Classificação de risco usada pelo broker."""

    READ = "read"
    NETWORK = "network"
    DELEGATION = "delegation"
    WRITE = "write"
    SHELL = "shell"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class TrustedToolExecutionContext:
    """Contexto confiável definido pelo runtime/servidor, nunca pelo cliente MCP."""

    agent_name: str | None = None
    parent_agent: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    job_id: str | int | None = None
    task_id: str | int | None = None
    transport: str = "native_tool_call"
    session_id: str | None = None
    server_origin: str = "tool_executor"
    http_profile: str | None = None
    approval_scope_id: str | None = None
    delegation_budget: int | None = None
    http_delegate_auto_approve: bool = False

    @classmethod
    def native(cls) -> "TrustedToolExecutionContext":
        return cls(
            run_id=f"native:{uuid.uuid4()}",
            transport="native_tool_call",
            server_origin="tool_executor",
        )

    @classmethod
    def from_trusted_metadata(
        cls,
        metadata: dict[str, Any] | None,
    ) -> "TrustedToolExecutionContext":
        """Extrai apenas contexto marcado como confiável pelo servidor.

        Campos comuns de ``metadata`` são deliberadamente ignorados: esse dict
        também pode conter `_meta`/dados emitidos pelo modelo ou cliente MCP.
        """
        meta = metadata or {}
        raw = meta.get("trusted_context") or meta.get("_trusted_context")
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, dict):
            return cls(
                agent_name=_clean(raw.get("agent_name")),
                parent_agent=_clean(raw.get("parent_agent")),
                run_id=_clean(raw.get("run_id")) or f"native:{uuid.uuid4()}",
                parent_run_id=_clean(raw.get("parent_run_id")),
                job_id=raw.get("job_id"),
                task_id=raw.get("task_id"),
                transport=_clean(raw.get("transport")) or "native_tool_call",
                session_id=_clean(raw.get("session_id")),
                server_origin=_clean(raw.get("server_origin")) or "tool_executor",
                http_profile=_clean(raw.get("http_profile")),
                approval_scope_id=_clean(raw.get("approval_scope_id")),
                delegation_budget=_positive_int_or_none(
                    raw.get("delegation_budget")
                ),
                http_delegate_auto_approve=bool(
                    raw.get("http_delegate_auto_approve", False)
                ),
            )
        return cls.native()

    @property
    def scope_key(self) -> str:
        if self.approval_scope_id:
            return f"scope:{self.approval_scope_id}"
        if self.run_id:
            return f"run:{self.run_id}"
        return f"thread:{threading.get_ident()}"


@dataclass(slots=True)
class ApprovalRequest:
    """Pedido de aprovação auditable."""

    id: str
    tool_name: str
    arguments: dict[str, Any]
    risk: RiskLevel
    context: TrustedToolExecutionContext
    summary: str
    path: str | None = None
    command: str | None = None
    reason: str | None = None
    target_agent_name: str | None = None
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
            self.target_agent_name or "",
        )

    @property
    def route(self) -> str:
        parts = [
            p for p in (self.context.parent_agent, self.context.agent_name) if p
        ]
        prefix = " → ".join(parts)
        target = (
            self.arguments.get("target_agent")
            if self.tool_name == "delegate"
            else None
        )
        op = f"{self.tool_name}({target})" if target else self.tool_name
        tail = self.path or self.command or ""
        rendered = f"{op} {tail}".strip()
        return f"{prefix} → {rendered}" if prefix else rendered


@dataclass(slots=True)
class ApprovalScope:
    """Escopo temporário de aprovação controlada e sempre limitado."""

    id: str
    run_id: str
    transport: str | None = None
    server_origin: str | None = None
    tool_name: str | None = None
    agent_name: str | None = None
    target_agent_name: str | None = None
    path: str | None = None
    risk: RiskLevel | None = None
    expires_at: float | None = None
    remaining_uses: int | None = None
    approve_all_in_run: bool = False

    def matches(self, request: ApprovalRequest) -> bool:
        now = time.time()
        if self.expires_at is None or self.expires_at <= now:
            return False
        if self.remaining_uses is None or self.remaining_uses <= 0:
            return False
        if self.run_id != request.context.run_id:
            return False
        if (
            self.transport is not None
            and self.transport != request.context.transport
        ):
            return False
        if (
            self.server_origin is not None
            and self.server_origin != request.context.server_origin
        ):
            return False
        if (
            self.tool_name is not None
            and self.tool_name != request.tool_name
        ):
            return False
        if (
            self.agent_name is not None
            and self.agent_name != request.context.agent_name
        ):
            return False
        if (
            self.target_agent_name is not None
            and self.target_agent_name != request.target_agent_name
        ):
            return False
        if self.path is not None and self.path != request.path:
            return False
        if self.risk is not None and self.risk != request.risk:
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

    _READ_TOOLS = frozenset({
        "list_files", "read_file", "grep_search", "list_tasks",
        "list_jobs", "get_job", "todo_list", "list_agents",
    })
    _NETWORK_TOOLS = frozenset({"web_search", "web_fetch"})
    _WRITE_TOOLS = frozenset({
        "write_file", "apply_patch", "todo_write", "write_stdin",
        "close_command_session", "git_add", "git_commit",
        "git_checkout", "git_push",
    })
    _SHELL_TOOLS = frozenset({
        "run_shell", "run_shell_command", "exec_command",
    })
    _DESTRUCTIVE_TOOLS = frozenset({"remove_file"})
    _PATH_TOOLS = frozenset({
        "read_file", "list_files", "grep_search",
        "write_file", "remove_file",
    })
    _DEFAULT_SCOPE_TTL_SECONDS = 300

    def __init__(self, config, approval_handler) -> None:
        self.config = config
        self._approval_handler = approval_handler
        self._prompt_lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._scope_lock = threading.Lock()
        self._budget_lock = threading.Lock()
        self._lock_table_lock = threading.Lock()
        self._scopes: list[ApprovalScope] = []
        self.audit_log: list[dict[str, Any]] = []
        self._delegation_budget: dict[str, int] = {}
        self._locks: dict[str, threading.RLock] = {}

    def build_context(self, call: ToolCall) -> TrustedToolExecutionContext:
        return TrustedToolExecutionContext.from_trusted_metadata(call.metadata)

    def classify(self, call: ToolCall) -> RiskLevel:
        if call.name == "delegate":
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
        target_agent_name = (
            _clean(call.arguments.get("target_agent"))
            if call.name == "delegate"
            else None
        )
        effective_reason = reason or (
            "path_permission" if permission_error is not None else None
        )
        summary = self._summary(
            call,
            risk,
            context,
            path=path,
            command=command,
            permission_error=permission_error,
            reason=effective_reason,
        )
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
            target_agent_name=target_agent_name,
        )

    def should_request_approval(
        self,
        call: ToolCall,
        *,
        needs_policy_approval: bool,
        permission_error: PathPermissionError | None = None,
    ) -> bool:
        request = self.create_request(call, permission_error=permission_error)
        return not self._can_auto_approve(
            request,
            needs_policy_approval=needs_policy_approval,
            consume=False,
        )

    def approve(
        self,
        call: ToolCall,
        *,
        needs_policy_approval: bool,
        permission_error: PathPermissionError | None = None,
    ) -> bool:
        """Fluxo completo de aprovação: governance + delega ao approval_handler."""
        request = self.create_request(call, permission_error=permission_error)
        self._record("request", request, decision=None)

        if self._can_auto_approve(
            request, needs_policy_approval=needs_policy_approval, consume=True
        ):
            self._record("auto_approved", request, decision=True)
            return True

        with self._prompt_lock:
            approve_request = _static_callable_attr(
                self._approval_handler, "approve_request"
            )
            if callable(approve_request):
                approved = bool(approve_request(request))
            else:
                approved = bool(
                    self._approval_handler.approve(
                        tool_name=call.name, summary=request.summary
                    )
                )
        self._record(
            "approved" if approved else "denied",
            request,
            decision=approved,
        )
        return approved

    def approve_call(
        self,
        call: ToolCall,
        *,
        needs_policy_approval: bool,
        permission_error: PathPermissionError | None = None,
    ) -> bool:
        """Alias para ``approve`` — compatibilidade com fluxo do ToolExecutor."""
        return self.approve(
            call,
            needs_policy_approval=needs_policy_approval,
            permission_error=permission_error,
        )

    @contextmanager
    def execution_guard(self, call: ToolCall) -> Iterator[None]:
        keys = self._serialization_keys(call)
        if not keys:
            yield
            return

        ordered_keys = sorted(set(keys))
        with self._lock_table_lock:
            locks = [
                self._locks.setdefault(key, threading.RLock())
                for key in ordered_keys
            ]

        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    def approve_scope(self, scope: ApprovalScope) -> None:
        self._validate_scope(scope)
        with self._scope_lock:
            self._scopes.append(scope)

    def approve_equivalent(
        self,
        request: ApprovalRequest,
        *,
        ttl_seconds: float | None = None,
        uses: int = 1,
    ) -> ApprovalScope:
        if ttl_seconds is None:
            ttl_seconds = self._DEFAULT_SCOPE_TTL_SECONDS
        scope = ApprovalScope(
            id=f"equiv:{request.id}",
            run_id=request.context.run_id or f"native:{uuid.uuid4()}",
            transport=request.context.transport,
            server_origin=request.context.server_origin,
            tool_name=request.tool_name,
            agent_name=request.context.agent_name,
            target_agent_name=request.target_agent_name,
            path=request.path,
            risk=request.risk,
            expires_at=time.time() + ttl_seconds,
            remaining_uses=uses,
        )
        self.approve_scope(scope)
        return scope

    def _can_auto_approve(
        self,
        request: ApprovalRequest,
        *,
        needs_policy_approval: bool,
        consume: bool,
    ) -> bool:
        if self._consume_matching_scope(request, consume=consume):
            return True

        if request.risk == RiskLevel.READ and request.reason is None:
            return True
        if request.risk == RiskLevel.NETWORK and request.reason is None:
            return True
        if request.risk == RiskLevel.DELEGATION:
            if (
                request.context.transport == "http_mcp"
                and not request.context.http_delegate_auto_approve
            ):
                return False
            return self._delegation_within_budget(request, consume=consume)
        return not needs_policy_approval and request.reason is None

    def _delegation_within_budget(
        self, request: ApprovalRequest, *, consume: bool
    ) -> bool:
        budget = request.context.delegation_budget
        if budget is None:
            budget = int(
                getattr(self.config, "delegation_budget_per_run", 8)
            )
        if budget <= 0:
            return False
        key = request.context.scope_key
        with self._budget_lock:
            used = self._delegation_budget.get(key, 0)
            if used >= budget:
                return False
            if consume:
                self._delegation_budget[key] = used + 1
            return True

    def _consume_matching_scope(
        self, request: ApprovalRequest, *, consume: bool
    ) -> bool:
        with self._scope_lock:
            self._prune_scopes_locked()
            for scope in list(self._scopes):
                if scope.matches(request):
                    if consume:
                        scope.consume()
                        self._prune_scopes_locked()
                    return True
        return False

    def _validate_scope(self, scope: ApprovalScope) -> None:
        if not scope.run_id:
            raise ValueError("ApprovalScope requer run_id confiável")
        if not scope.transport:
            raise ValueError("ApprovalScope requer transport explícito")
        if not scope.server_origin:
            raise ValueError("ApprovalScope requer server_origin explícito")
        if scope.expires_at is None or scope.expires_at <= time.time():
            raise ValueError("ApprovalScope requer expires_at futuro")
        if scope.remaining_uses is None or scope.remaining_uses <= 0:
            raise ValueError("ApprovalScope requer remaining_uses positivo")
        if scope.risk is None:
            raise ValueError("ApprovalScope requer risk explícito")
        if not scope.approve_all_in_run and not scope.tool_name:
            raise ValueError(
                "ApprovalScope requer tool_name ou approve_all_in_run explícito"
            )
        if scope.tool_name in {
            "write_file", "remove_file", "apply_patch",
        } and not scope.path:
            raise ValueError(
                "ApprovalScope de mutação de arquivo requer path"
            )
        if scope.tool_name == "delegate" and (
            not scope.agent_name or not scope.target_agent_name
        ):
            raise ValueError(
                "ApprovalScope de delegate requer agent_name "
                "e target_agent_name"
            )

    def _prune_scopes(self) -> None:
        with self._scope_lock:
            self._prune_scopes_locked()

    def _prune_scopes_locked(self) -> None:
        now = time.time()
        self._scopes = [
            s
            for s in self._scopes
            if not s.exhausted
            and s.expires_at is not None
            and s.expires_at > now
        ]

    def _serialization_keys(self, call: ToolCall) -> list[str]:
        if call.name in {"run_shell", "run_shell_command", "exec_command"}:
            return [f"workspace:{self.config.workspace_root}"]
        if call.name in {"write_stdin", "close_command_session"}:
            session_id = call.arguments.get("session_id")
            return [
                f"command-session:{session_id}"
                if session_id is not None
                else "command-session:unknown"
            ]
        if call.name in {"write_file", "remove_file"}:
            path = self._extract_path(call)
            return [
                f"path:{path}"
                if path
                else f"workspace:{self.config.workspace_root}"
            ]
        if call.name == "apply_patch":
            paths = self._extract_patch_paths(
                str(call.arguments.get("patch", ""))
            )
            if paths:
                return [f"path:{path}" for path in sorted(set(paths))]
            return [f"workspace:{self.config.workspace_root}"]
        return []

    def _serialization_key(self, call: ToolCall) -> str | None:
        keys = self._serialization_keys(call)
        if not keys:
            return None
        if len(keys) == 1:
            return keys[0]
        return "|".join(keys)

    def _extract_path(
        self,
        call: ToolCall,
        *,
        permission_error: PathPermissionError | None = None,
    ) -> str | None:
        if permission_error is not None:
            return str(permission_error.resolved_path)
        if call.name == "apply_patch":
            paths = self._extract_patch_paths(
                str(call.arguments.get("patch", ""))
            )
            return (
                paths[0]
                if len(paths) == 1
                else (", ".join(paths) if paths else None)
            )
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

        def add(raw_path: str) -> None:
            raw = raw_path.strip()
            if not raw or raw == "/dev/null":
                return
            if raw.startswith("a/") or raw.startswith("b/"):
                raw = raw[2:]
            try:
                path = (self.config.workspace_root / raw.lstrip("/")).resolve()
            except Exception:
                return
            if is_path_inside(path, self.config.workspace_root):
                text = str(path)
                if text not in paths:
                    paths.append(text)

        for line in patch.splitlines():
            if line.startswith("*** Add File: "):
                add(line[len("*** Add File: ") :])
                continue
            if line.startswith("*** Delete File: "):
                add(line[len("*** Delete File: ") :])
                continue
            if line.startswith("*** Update File: "):
                add(line[len("*** Update File: ") :])
                continue
            if line.startswith("*** Move to: "):
                add(line[len("*** Move to: ") :])
                continue
            if line.startswith("+++ ") or line.startswith("--- "):
                add(line[4:].strip().split("\t", 1)[0])
        return paths

    def _summary(
        self,
        call: ToolCall,
        risk: RiskLevel,
        context: TrustedToolExecutionContext,
        *,
        path: str | None,
        command: str | None,
        permission_error: PathPermissionError | None,
        reason: str | None,
    ) -> str:
        route = self.create_route(call, context, path=path, command=command)
        details = [f"risco: {risk.value}"]
        origin = self._origin_context(route, call.name)
        if origin:
            details.append(f"origem: {origin}")
        if command:
            details.append(f"comando: {command}")
        if reason:
            details.append(f"justificativa: {reason}")
        body = (
            f"Permissão necessária para acessar: "
            f"{permission_error.resolved_path}"
            if permission_error
            else ToolPreview.build(
                call.name,
                call.arguments,
                context="approval",
                omit_fields={"command"} if command else None,
            )
        )
        details.append(body)
        return "\n".join(details)

    @staticmethod
    def _origin_context(route: str, tool_name: str) -> str | None:
        """Extrai só o encadeamento de agentes, sem repetir a ação/tool."""
        cleaned = route.strip()
        if not cleaned:
            return None
        if " → " not in cleaned:
            return None
        parts = [part.strip() for part in cleaned.split(" → ") if part.strip()]
        if len(parts) <= 1:
            return None
        return " → ".join(parts[:-1]) or None

    def create_route(
        self,
        call: ToolCall,
        context: TrustedToolExecutionContext,
        *,
        path: str | None = None,
        command: str | None = None,
    ) -> str:
        req = ApprovalRequest(
            "route",
            call.name,
            call.arguments,
            self.classify(call),
            context,
            "",
            path=path,
            command=command,
        )
        return req.route

    def _record(
        self,
        event: str,
        request: ApprovalRequest,
        decision: bool | None,
    ) -> None:
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
                "session_id": request.context.session_id,
                "server_origin": request.context.server_origin,
                "http_profile": request.context.http_profile,
                "target_agent_name": request.target_agent_name,
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


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _extract_command(call: ToolCall) -> str | None:
    if call.name in {"run_shell", "run_shell_command"}:
        return str(call.arguments.get("command", "")).strip() or None
    if call.name == "exec_command":
        return (
            str(call.arguments.get("cmd") or call.arguments.get("command") or "")
        ).strip() or None
    return None


def _static_callable_attr(obj: Any, name: str):
    """Retorna atributo chamável somente se ele existir estaticamente.

    Evita falsos positivos com mocks dinâmicos: ``MagicMock`` cria qualquer
    atributo em ``getattr()``, o que faria o broker acreditar que um handler
    legado suporta ``approve_request`` e aprovaria com retorno truthy de mock.
    """
    try:
        inspect.getattr_static(obj, name)
    except AttributeError:
        return None
    try:
        value = getattr(obj, name)
    except Exception:
        return None
    return value if callable(value) else None

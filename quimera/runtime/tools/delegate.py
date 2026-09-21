"""Componentes de `quimera.runtime.tools.delegate`."""
from __future__ import annotations

import json
import logging
import inspect
import threading
import time
import uuid
from typing import Protocol, Callable

from ..config import (
    DEFAULT_DELEGATE_MAX_CONTEXT_CHARS,
    DEFAULT_DELEGATE_MAX_REQUEST_CHARS,
    ToolRuntimeConfig,
)
from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError
from ...tasks.api import (
    add_job,
    complete_task,
    create_task,
    fail_task,
    get_job,
    update_job_status,
)
from ..approval import TrustedToolExecutionContext
from .base import ToolBase, ValidatableTool

logger = logging.getLogger(__name__)

_DELEGATE_TOOL_NAMES = ["delegate", "list_agents"]
_DELEGATE_ROLES = frozenset({"planner", "executor", "reviewer", "verifier", "synthesizer"})
_DELEGATE_TIMEOUT_CANCEL_JOIN_SECONDS = 2.0


class _DelegateStepCancelHandle:
    """Handle cooperativo para cancelar um step paralelo em execução."""

    def __init__(self) -> None:
        self._callbacks: list[Callable[[], None]] = []
        self._cancelled = False
        self._lock = threading.Lock()

    def register(self, callback: Callable[[], None]) -> None:
        """Registra callback de cancelamento, disparando imediatamente se vencido."""
        should_call = False
        with self._lock:
            if self._cancelled:
                should_call = True
            else:
                self._callbacks.append(callback)
        if should_call:
            callback()

    def cancel(self) -> None:
        """Aciona cancelamento uma única vez."""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.warning("delegate cancel callback failed", exc_info=True)


def _watch_request_cancellation(
    cancel_event: threading.Event | None,
    cancel_handles: list[_DelegateStepCancelHandle],
    done_event: threading.Event,
) -> threading.Thread | None:
    """Propaga cancelamento da chamada MCP aos AgentClients do delegate."""
    if cancel_event is None:
        return None

    def _watch() -> None:
        while not done_event.wait(0.05):
            if cancel_event.is_set():
                for handle in cancel_handles:
                    handle.cancel()
                return

    watcher = threading.Thread(
        target=_watch,
        daemon=True,
        name="delegate-request-cancel",
    )
    watcher.start()
    return watcher


class _DelegateFnProto(Protocol):
    """Protocolo para a função de despacho de tarefas entre agentes."""

    def __call__(
        self,
        agent: str,
        *,
        delegation: dict[str, object] | None = None,
        delegation_only: bool = True,
        protocol_mode: str = "delegation",
        primary: bool = False,
        silent: bool = True,
        show_output: bool = False,
        persist_history: bool = True,
        history_snapshot: list | None = None,
        max_retries: int = 1,
        from_agent: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
        cancel_handle: _DelegateStepCancelHandle | None = None,
    ) -> str | None: ...


class DelegateTools(ToolBase):
    """Ferramentas de delegação entre agentes via MCP."""

    _DELEGATE_MAX_REQUEST_CHARS = DEFAULT_DELEGATE_MAX_REQUEST_CHARS
    _DELEGATE_MAX_CONTEXT_CHARS = DEFAULT_DELEGATE_MAX_CONTEXT_CHARS

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de DelegateTools."""
        super().__init__(config)
        self._delegate_fn: _DelegateFnProto | None = None
        self._background_delegate_fn: _DelegateFnProto | None = None
        self._active_agents_provider = None
        self._agent_stats_provider = None
        self._orchestrator_provider = None
        self._progress_callback: Callable[[str], None] | None = None
        self._cleanup_callback: Callable[[str], None] | None = None
        self._cancel_checker: Callable[[], bool] | None = None

    def set_delegate_fn(self, fn: _DelegateFnProto) -> None:
        """Injeta callable para despachar tarefas a outro agente."""
        self._delegate_fn = fn

    def set_background_delegate_fn(self, fn: _DelegateFnProto | None) -> None:
        """Injeta callable independente para delegação assíncrona via HTTP MCP.

        Deve usar dispatch services com AgentClient próprio (cancel_event isolado),
        garantindo que cancelamentos do fluxo do chat não interfiram no delegate
        assíncrono e vice-versa.
        """
        self._background_delegate_fn = fn

    def set_active_agents_provider(self, fn) -> None:
        """Injeta provider que retorna agentes ativos no momento da delegação."""
        self._active_agents_provider = fn

    def set_agent_stats_provider(self, fn) -> None:
        """Injeta provider de métricas observadas por agente."""
        self._agent_stats_provider = fn

    def set_orchestrator_provider(self, fn) -> None:
        """Injeta provider que retorna o agente orquestrador ativo (ou None)."""
        self._orchestrator_provider = fn

    def set_progress_callback(self, fn: Callable[[str], None] | None) -> None:
        """Injeta callback para reporte de progresso."""
        self._progress_callback = fn

    def set_cleanup_callback(self, fn: Callable[[str], None] | None) -> None:
        """Injeta callback para limpeza do estado de render após cada step."""
        self._cleanup_callback = fn

    def set_cancel_checker(self, fn: Callable[[], bool] | None) -> None:
        """Injeta checker de cancelamento do usuário."""
        self._cancel_checker = fn

    @staticmethod
    def _accepts_keyword(fn: Callable[..., object], keyword: str) -> bool:
        """Retorna se callable aceita um kwarg específico sem invocar a função."""
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return False
        for parameter in signature.parameters.values():
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                return True
            if parameter.name == keyword and parameter.kind in {
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }:
                return True
        return False

    def is_delegate_available(self) -> bool:
        """Indica se a tool delegate está operável no contexto atual."""
        return callable(self._delegate_fn)

    @staticmethod
    def _normalize_agent_identity(agent_name: str | None) -> str:
        """Normaliza o nome de um agente para comparação."""
        if agent_name is None:
            return ""
        return str(agent_name).strip().lower().lstrip("/")

    def _resolve_active_agents(self) -> set[str]:
        """Retorna o conjunto de agentes ativos via o provider injetado."""
        provider = self._active_agents_provider
        if not callable(provider):
            return set()
        try:
            raw_agents = provider() or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("_resolve_active_agents: falha ao consultar provider: %s", exc)
            return set()
        active: set[str] = set()
        for item in raw_agents:
            normalized = self._normalize_agent_identity(item)
            if normalized:
                active.add(normalized)
        return active

    def _build_agent_descriptor(self, agent_name: str) -> dict[str, object]:
        """Monta catálogo compacto para orientar a escolha de um delegado."""
        descriptor: dict[str, object] = {"name": agent_name}

        # O registry de profiles já é a fonte de verdade para capacidades e
        # conexão efetiva. Import local evita acoplar a carga das tools ao
        # carregamento de todos os profiles.
        from ... import profiles

        profile = profiles.get(agent_name)
        if profile is not None:
            base_profile = getattr(profile, "_profile_name", None) or profile.name
            descriptor["profile"] = base_profile

            try:
                model = profile.resolve_runtime_model(
                    cwd=str(self.workspace.cwd) if self.workspace is not None else None,
                )
            except Exception:  # noqa: BLE001 - metadado opcional não pode quebrar list_agents
                model = profile.effective_model()
            if model:
                descriptor["model"] = model

            routing_aliases = {
                "code_editing": "code_edit",
                "general_coding": "general",
                "tool_use": None,
            }
            best_for: list[str] = []
            for item in [*profile.preferred_task_types, *profile.capabilities]:
                normalized = routing_aliases.get(item, item)
                if normalized and normalized not in best_for:
                    best_for.append(normalized)
            if best_for:
                descriptor["best_for"] = best_for

            descriptor["tier"] = profile.base_tier
            descriptor["tools"] = (
                profile.tool_use_reliability if profile.supports_tools else "none"
            )
            if profile.supports_long_context:
                descriptor["long_context"] = True

        stats_provider = self._agent_stats_provider
        if callable(stats_provider):
            try:
                summary = stats_provider(agent_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("list_agents: falha ao consultar stats de %s: %s", agent_name, exc)
                summary = None
            if isinstance(summary, dict):
                observed: dict[str, object] = {}
                responses = int(summary.get("responses_total") or 0)
                tool_calls = int(summary.get("tool_calls_total") or 0)
                if responses:
                    observed["responses"] = responses
                    latency = float(summary.get("avg_latency_seconds") or 0.0)
                    if latency:
                        observed["latency_s"] = latency
                if tool_calls:
                    observed["tool_calls"] = tool_calls
                    observed["tool_success"] = float(summary.get("tool_success_rate") or 0.0)
                invalid_tools = int(summary.get("invalid_tool_calls") or 0)
                if invalid_tools:
                    observed["invalid_tools"] = invalid_tools
                tool_aborts = int(summary.get("tool_loop_abortions") or 0)
                if tool_aborts:
                    observed["tool_aborts"] = tool_aborts
                if observed:
                    descriptor["observed"] = observed

        return descriptor

    def list_agents(self, call: ToolCall) -> ToolResult:
        """Retorna catálogo compacto dos agentes ativos no pool da sessão."""
        agents = self._resolve_active_agents()
        catalog = [self._build_agent_descriptor(agent) for agent in sorted(agents)]
        content = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        return ToolResult(ok=True, tool_name=call.name, content=content)

    # ── transport detection ──────────────────────────────────────────────

    @staticmethod
    def _get_transport(call: ToolCall) -> str:
        """Extrai o transporte do contexto confiável no metadata do ToolCall."""
        ctx = call.metadata.get("trusted_context", {})
        if isinstance(ctx, TrustedToolExecutionContext):
            return ctx.transport
        if isinstance(ctx, dict):
            return str(ctx.get("transport", "native_tool_call"))
        return "native_tool_call"

    @staticmethod
    def _get_calling_agent(call: ToolCall) -> str | None:
        """Extrai o nome do agente que emitiu o tool call, se disponível."""
        raw = call.metadata.get("calling_agent")
        if raw and isinstance(raw, str):
            return raw.strip().lower().lstrip("/")
        ctx = call.metadata.get("trusted_context")
        if isinstance(ctx, TrustedToolExecutionContext) and ctx.agent_name:
            return ctx.agent_name.strip().lower().lstrip("/")
        if isinstance(ctx, dict):
            name = ctx.get("agent_name")
            if name and isinstance(name, str):
                return name.strip().lower().lstrip("/")
        return None

    @staticmethod
    def _get_parent_agent(call: ToolCall) -> str | None:
        """Extrai o agente pai (quem delegou para o agente atual), se disponível."""
        ctx = call.metadata.get("trusted_context")
        if isinstance(ctx, TrustedToolExecutionContext) and ctx.parent_agent:
            return ctx.parent_agent.strip().lower().lstrip("/")
        if isinstance(ctx, dict):
            name = ctx.get("parent_agent")
            if name and isinstance(name, str):
                return name.strip().lower().lstrip("/")
        return None

    def _get_db_path(self) -> str | None:
        """Retorna o banco de tasks definido pelo Workspace atual."""
        workspace = getattr(self.config, "workspace", None)
        if workspace is None:
            return None
        return str(workspace.tasks_db)

    # ── tracking unificado (job/task no banco) ───────────────────────────

    @staticmethod
    def _tracking_job_desc(steps: list[dict]) -> str:
        """Descrição curta do job que representa a delegação no banco."""
        step_one = steps[0]
        return f"delegate → {step_one['target_agent']}: {step_one['request'][:80]}"

    def _tracking_created_by(self, call: ToolCall) -> str:
        """Identifica quem originou a delegação para o registro no banco."""
        calling_agent = self._get_calling_agent(call)
        if calling_agent:
            return calling_agent
        return "mcp_http" if self._get_transport(call) == "http_mcp" else "delegate"

    def _insert_tracking_task(
        self, job_id: int, steps: list[dict], db_path: str,
    ) -> tuple[int, dict]:
        """Cria a task de acompanhamento e ativa o job; levanta exceção em falha."""
        step_one = steps[0]
        body = json.dumps(steps, ensure_ascii=False)
        task_id = create_task(
            job_id,
            step_one["request"][:120],
            body=body,
            assigned_to=step_one["target_agent"],
            origin="delegate",
            status="in_progress",
            db_path=db_path,
        )
        update_job_status(job_id, "active", db_path=db_path)
        job_snapshot = get_job(job_id, db_path=db_path) or {}
        return task_id, job_snapshot

    def _finalize_tracking(
        self, job_id: int, task_id: int, result: ToolResult, db_path: str,
    ) -> None:
        """Grava o desfecho da delegação na task/job de acompanhamento."""
        try:
            if result.ok:
                complete_task(task_id, result=result.content, db_path=db_path)
                update_job_status(job_id, "completed", db_path=db_path)
            else:
                fail_task(task_id, reason=result.error, db_path=db_path)
                update_job_status(job_id, "failed", db_path=db_path)
        except Exception as exc:
            logger.warning(
                "delegate tracking: failed to update task/job %s: %s", task_id, exc
            )

    # ── async path (wait=false e HTTP MCP sem SSE) ───────────────────────

    def _delegate_async(
        self,
        call: ToolCall,
        steps: list[dict],
        parallel: bool = False,
    ) -> ToolResult:
        """Registra a delegação como job/task e executa em background thread.

        Retorna imediatamente {job_id, task_id, status: in_progress} para o
        chamador acompanhar via list_tasks/get_job; o resultado final é
        persistido na task (completed/failed). Usado por wait=false em
        qualquer transporte e sempre por HTTP MCP sem SSE, que não sustenta
        uma chamada bloqueante longa.
        """
        db_path = self._get_db_path()
        if not db_path:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="db_path not configured — cannot run delegate async (wait=false / HTTP MCP)",
            )

        try:
            job_id = add_job(
                self._tracking_job_desc(steps),
                created_by=self._tracking_created_by(call),
                db_path=db_path,
            )
        except Exception as exc:
            return ToolResult(ok=False, tool_name=call.name, error=f"Failed to create job: {exc}")

        try:
            task_id, job_snapshot = self._insert_tracking_task(job_id, steps, db_path)
        except Exception as exc:
            return ToolResult(ok=False, tool_name=call.name, error=f"Failed to create task: {exc}")

        _fn = self._background_delegate_fn or self._delegate_fn
        _progress_cb = self._progress_callback
        _resolve_active = self._resolve_active_agents
        _normalize = self._normalize_agent_identity
        _cleanup_cb = self._cleanup_callback

        def _run() -> None:
            if parallel and len(steps) > 1:
                result = self._execute_steps_parallel(
                    steps,
                    _fn,
                    _progress_cb,
                    _resolve_active,
                    _normalize,
                    cleanup_callback=_cleanup_cb,
                )
            else:
                result = self._execute_steps_inner(
                    steps,
                    _fn,
                    _progress_cb,
                    _resolve_active,
                    _normalize,
                    cleanup_callback=_cleanup_cb,
                )
            self._finalize_tracking(job_id, task_id, result, db_path)

        t = threading.Thread(target=_run, daemon=True, name=f"delegate-{task_id}")
        t.start()

        payload = {
            "job_id": job_id,
            "task_id": task_id,
            "status": "in_progress",
            "job_status": job_snapshot.get("status", "active"),
            "task_status": "in_progress",
            "started_at": job_snapshot.get("started_at"),
            "hint": (
                f"acompanhe com list_tasks {{\"id\": {task_id}}}; "
                "o resultado final é salvo na task"
            ),
        }
        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=json.dumps(payload, ensure_ascii=False),
            data=dict(payload),
        )

    def _delegate_http_async(
        self,
        call: ToolCall,
        steps: list[dict],
        parallel: bool = False,
    ) -> ToolResult:
        """Executa delegate via HTTP MCP.

        Com SSE: executa inline na thread pool (bloqueante, com tracking) — o
        resultado chega ao cliente via SSE quando a thread pool completar.
        Sem SSE: delegação assíncrona idêntica a wait=false — job/task no
        banco, execução em background e {job_id, task_id} imediato p/ polling.
        """
        meta_state = call.metadata.get("_mcp_state") or {}
        sse_available = meta_state.get("sse_queue") is not None

        if sse_available:
            _bg_fn = self._background_delegate_fn or self._delegate_fn
            return self._execute_tracked_sync(call, steps, parallel, _bg_fn)

        return self._delegate_async(call, steps, parallel)

    # ── synchronous execution core ───────────────────────────────────────

    @staticmethod
    def _new_delegation_id() -> str:
        """Gera identificador curto para correlacionar uma delegação no feed."""
        return f"dlg-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _delegation_chain(source_agent: str | None, target_agent: str | None) -> list[str]:
        """Monta cadeia visual mínima, sem valores vazios ou duplicados adjacentes."""
        chain: list[str] = []
        for value in (source_agent, target_agent):
            normalized = str(value or "").strip()
            if normalized and (not chain or chain[-1] != normalized):
                chain.append(normalized)
        return chain

    @staticmethod
    def _normalize_role(value: object, field_name: str = "role") -> str:
        """Normaliza papel opcional da delegação."""
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"'{field_name}' must be a string when provided")
        role = value.strip()
        if not role:
            raise ValueError(f"'{field_name}' must be a non-empty string when provided")
        if role not in _DELEGATE_ROLES:
            allowed = ", ".join(sorted(_DELEGATE_ROLES))
            raise ValueError(f"'{field_name}' must be one of: {allowed}")
        return role

    @staticmethod
    def _normalize_access_list(value: object, field_name: str = "access_list") -> list[str]:
        """Normaliza escopo declarado opcional da delegação."""
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"'{field_name}' must be a list of strings when provided")
        normalized: list[str] = []
        for idx, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"'{field_name}[{idx}]' must be a non-empty string")
            normalized.append(item.strip())
        return normalized

    @staticmethod
    def _execute_single_step(
        step: dict,
        delegate_fn: _DelegateFnProto,
        progress_callback: Callable[[str], None] | None,
        normalize_agent_fn: Callable[[str | None], str],
        cleanup_callback: Callable[[str], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        cancel_handle: _DelegateStepCancelHandle | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """Executa um único step. Retorna (selected_agent, result_str, error_str)."""
        attempt_targets = [step["target_agent"], *step["fallback_agents"]]
        step_result = None
        last_error = None
        selected_agent = None
        normalized_target_agent = ""
        source_agent = str(step.get("source_agent") or "").strip() or None
        delegation_id = str(step.get("delegation_id") or "").strip() or DelegateTools._new_delegation_id()
        for target_agent in attempt_targets:
            if callable(cancel_checker) and cancel_checker():
                return None, None, "Execução cancelada pelo usuário"
            normalized_target_agent = normalize_agent_fn(target_agent)
            delegation = {
                "task": step["request"],
                "context": step["context"],
                "delegation_id": delegation_id,
                "chain": DelegateTools._delegation_chain(source_agent, normalized_target_agent or target_agent),
            }
            role = str(step.get("role") or "").strip()
            access_list = step.get("access_list") or []
            if role:
                delegation["role"] = role
            if access_list:
                delegation["access_list"] = list(access_list)
            try:
                delegate_options = {
                    "delegation": delegation,
                    "delegation_only": True,
                    "protocol_mode": "delegation",
                    "primary": False,
                    "silent": False,
                    "show_output": False,
                    "persist_history": True,
                    "history_snapshot": [],
                    "max_retries": 3,
                    "from_agent": source_agent,
                    "progress_callback": progress_callback,
                }
                if cancel_handle is not None and DelegateTools._accepts_keyword(
                    delegate_fn,
                    "cancel_handle",
                ):
                    delegate_options["cancel_handle"] = cancel_handle
                result = delegate_fn(normalized_target_agent, **delegate_options)
            except Exception as dispatch_error:
                if callable(cancel_checker) and cancel_checker():
                    return None, None, "Execução cancelada pelo usuário"
                last_error = str(dispatch_error)
                logger.warning(
                    "delegate: dispatch to '%s' failed: %s",
                    target_agent, last_error,
                )
                continue
            if result is None:
                if callable(cancel_checker) and cancel_checker():
                    return None, None, "Execução cancelada pelo usuário"
                last_error = f"Agent '{target_agent}' returned no response"
                logger.warning(
                    "delegate: dispatch to '%s' returned no response",
                    target_agent,
                )
                continue
            selected_agent = target_agent
            step_result = str(result)
            if cleanup_callback and normalized_target_agent:
                try:
                    cleanup_callback(normalized_target_agent)
                except Exception:
                    logger.warning(
                        "cleanup_callback failed for %s",
                        normalized_target_agent, exc_info=True,
                    )
            break

        if step_result is None:
            if cleanup_callback and normalized_target_agent:
                try:
                    cleanup_callback(normalized_target_agent)
                except Exception:
                    logger.warning(
                        "cleanup_callback failed for %s",
                        normalized_target_agent, exc_info=True,
                    )
            error_detail = (
                f"{last_error}. Tried: {', '.join(attempt_targets)}"
                if last_error
                else f"No response from any target. Tried: {', '.join(attempt_targets)}"
            )
            return None, None, error_detail

        return selected_agent, step_result, None

    @staticmethod
    def _execute_steps_inner(
        steps: list[dict],
        delegate_fn: _DelegateFnProto,
        progress_callback: Callable[[str], None] | None,
        resolve_active_agents_fn: Callable[[], set[str]],
        normalize_agent_fn: Callable[[str | None], str],
        cleanup_callback: Callable[[str], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        request_cancel_event: threading.Event | None = None,
    ) -> ToolResult:
        """Loop sequencial de execução dos steps — reusado síncrono e assíncrono."""
        tool_name = "delegate"
        try:
            step_outputs: list[str] = []
            for step in steps:
                if callable(cancel_checker) and cancel_checker():
                    return ToolResult(
                        ok=False,
                        tool_name=tool_name,
                        error="Execução cancelada pelo usuário",
                    )
                active_agents = resolve_active_agents_fn()
                if active_agents:
                    invalid_targets: list[str] = []
                    targets = [step["target_agent"], *step["fallback_agents"]]
                    for target in targets:
                        normalized_target = normalize_agent_fn(target)
                        if normalized_target and normalized_target not in active_agents:
                            invalid_targets.append(target)
                    if invalid_targets:
                        invalid_label = ", ".join(dict.fromkeys(invalid_targets))
                        active_label = ", ".join(sorted(active_agents))
                        return ToolResult(
                            ok=False,
                            tool_name=tool_name,
                            error=(
                                f"Agents not active in current pool: {invalid_label}. "
                                f"Active agents: {active_label}"
                            ),
                        )

                cancel_handle = (
                    _DelegateStepCancelHandle()
                    if request_cancel_event is not None
                    else None
                )
                step_done = threading.Event()
                watcher = _watch_request_cancellation(
                    request_cancel_event,
                    [cancel_handle] if cancel_handle is not None else [],
                    step_done,
                )
                try:
                    selected_agent, step_result, error = DelegateTools._execute_single_step(
                        step, delegate_fn, progress_callback, normalize_agent_fn,
                        cleanup_callback, cancel_checker, cancel_handle,
                    )
                finally:
                    step_done.set()
                    if watcher is not None:
                        watcher.join(timeout=0.1)

                if step_result is None:
                    return ToolResult(ok=False, tool_name=tool_name, error=error)

                if len(steps) == 1:
                    step_outputs.append(step_result)
                else:
                    step_outputs.append(f"[{selected_agent}] {step_result}")

            content = "\n\n".join(step_outputs)
            return ToolResult(ok=True, tool_name=tool_name, content=content)
        except Exception as exc:
            return ToolResult(
                ok=False,
                tool_name=tool_name,
                error=str(exc),
            )

    def _execute_steps_parallel(
        self,
        steps: list[dict],
        delegate_fn: _DelegateFnProto,
        progress_callback: Callable[[str], None] | None,
        resolve_active_agents_fn: Callable[[], set[str]],
        normalize_agent_fn: Callable[[str | None], str],
        cleanup_callback: Callable[[str], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        request_cancel_event: threading.Event | None = None,
    ) -> ToolResult:
        """Execução paralela de steps — cada step roda em thread própria."""
        tool_name = "delegate"
        n = len(steps)
        timeout_cancelled = threading.Event()

        def is_cancelled() -> bool:
            return timeout_cancelled.is_set() or bool(
                callable(cancel_checker) and cancel_checker()
            )

        # Validate all agents before spawning threads
        active_agents = resolve_active_agents_fn()
        if active_agents:
            for step in steps:
                invalid_targets: list[str] = []
                for target in [step["target_agent"], *step["fallback_agents"]]:
                    normalized = normalize_agent_fn(target)
                    if normalized and normalized not in active_agents:
                        invalid_targets.append(target)
                if invalid_targets:
                    invalid_label = ", ".join(dict.fromkeys(invalid_targets))
                    active_label = ", ".join(sorted(active_agents))
                    return ToolResult(
                        ok=False,
                        tool_name=tool_name,
                        error=(
                            f"Agents not active in current pool: {invalid_label}. "
                            f"Active agents: {active_label}"
                        ),
                    )

        results: list[tuple[str | None, str | None, str | None]] = [
            (None, None, None)
        ] * n
        results_lock = threading.Lock()
        timed_out_indexes: set[int] = set()
        cancel_handles = [_DelegateStepCancelHandle() for _ in steps]
        steps_done = threading.Event()
        cancel_watcher = _watch_request_cancellation(
            request_cancel_event,
            cancel_handles,
            steps_done,
        )

        def run_step(idx: int, step: dict) -> None:
            step_result = DelegateTools._execute_single_step(
                step, delegate_fn, progress_callback, normalize_agent_fn,
                cleanup_callback, is_cancelled, cancel_handles[idx],
            )
            with results_lock:
                if idx not in timed_out_indexes:
                    results[idx] = step_result

        threads = [
            threading.Thread(
                target=run_step, args=(i, step), daemon=True,
                name=f"delegate-parallel-{i}",
            )
            for i, step in enumerate(steps)
        ]
        try:
            for t in threads:
                t.start()
            timeout_seconds = max(1, int(self.config.delegate_parallel_timeout_seconds))
            deadline = time.monotonic() + timeout_seconds
            for index, thread in enumerate(threads):
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    thread.join(timeout=remaining)
                if thread.is_alive():
                    with results_lock:
                        timed_out_indexes.add(index)
                        results[index] = (
                            None,
                            None,
                            f"Delegação paralela excedeu o limite de {timeout_seconds}s",
                        )
                    timeout_cancelled.set()
                    cancel_handles[index].cancel()
                    thread.join(timeout=_DELEGATE_TIMEOUT_CANCEL_JOIN_SECONDS)
        finally:
            steps_done.set()
            if cancel_watcher is not None:
                cancel_watcher.join(timeout=0.1)

        if timed_out_indexes:
            if cleanup_callback:
                for index in timed_out_indexes:
                    targets = (
                        steps[index]["target_agent"],
                        *steps[index]["fallback_agents"],
                    )
                    for target in targets:
                        normalized = normalize_agent_fn(target)
                        if not normalized:
                            continue
                        try:
                            cleanup_callback(normalized)
                        except Exception:
                            logger.warning(
                                "cleanup_callback failed for %s after timeout",
                                normalized,
                                exc_info=True,
                            )

        with results_lock:
            result_snapshot = list(results)

        errors = [
            (i, err)
            for i, (_, _, err) in enumerate(result_snapshot)
            if err is not None
        ]
        if errors:
            error_msg = "; ".join(f"step[{i}]: {e}" for i, e in errors)
            completed = [
                f"[{selected or steps[i]['target_agent']}] {result}"
                for i, (selected, result, _) in enumerate(result_snapshot)
                if result is not None
            ]
            return ToolResult(
                ok=False,
                tool_name=tool_name,
                content="\n\n".join(completed),
                error=error_msg,
            )

        step_outputs: list[str] = []
        for i, (selected_agent, step_result, _) in enumerate(result_snapshot):
            if step_result is None:
                continue
            if n == 1:
                step_outputs.append(step_result)
            else:
                label = selected_agent or steps[i]["target_agent"]
                step_outputs.append(f"[{label}] {step_result}")

        return ToolResult(ok=True, tool_name=tool_name, content="\n\n".join(step_outputs))

    def _execute_tracked_sync(
        self,
        call: ToolCall,
        steps: list[dict],
        parallel: bool,
        delegate_fn: _DelegateFnProto,
        cancel_checker: Callable[[], bool] | None = None,
        request_cancel_event: threading.Event | None = None,
    ) -> ToolResult:
        """Executa steps bloqueando o chamador, com a delegação registrada como task.

        Mesmo substrato de acompanhamento do caminho assíncrono: task
        in_progress durante a execução e completed/failed ao final, com o
        resultado persistido. O rastreio é best-effort — sem db_path (ou com
        falha no banco) a execução segue sem registro, preservando o
        comportamento síncrono.
        """
        db_path = self._get_db_path()
        tracking: tuple[int, int] | None = None
        if db_path:
            try:
                job_id = add_job(
                    self._tracking_job_desc(steps),
                    created_by=self._tracking_created_by(call),
                    db_path=db_path,
                )
                task_id, _snapshot = self._insert_tracking_task(job_id, steps, db_path)
                tracking = (job_id, task_id)
            except Exception as exc:
                logger.warning("delegate tracking: failed to create job/task: %s", exc)

        if parallel and len(steps) > 1:
            result = self._execute_steps_parallel(
                steps,
                delegate_fn,
                self._progress_callback,
                self._resolve_active_agents,
                self._normalize_agent_identity,
                cleanup_callback=self._cleanup_callback,
                cancel_checker=cancel_checker,
                request_cancel_event=request_cancel_event,
            )
        else:
            result = self._execute_steps_inner(
                steps,
                delegate_fn,
                self._progress_callback,
                self._resolve_active_agents,
                self._normalize_agent_identity,
                cleanup_callback=self._cleanup_callback,
                cancel_checker=cancel_checker,
                request_cancel_event=request_cancel_event,
            )

        if tracking is not None:
            job_id, task_id = tracking
            self._finalize_tracking(job_id, task_id, result, db_path)
            result.data["job_id"] = job_id
            result.data["task_id"] = task_id
            result.data["task_status"] = "completed" if result.ok else "failed"
            if result.ok:
                footer = (
                    f"[delegação registrada como task {task_id} (job {job_id}); "
                    "resultado persistido — consultável via list_tasks]"
                )
                result.content = f"{result.content}\n\n{footer}" if result.content else footer
        return result

    @staticmethod
    def _truncate_delegate_text(value: str, max_chars: int, label: str) -> tuple[str, str | None]:
        """Trunca texto acima do limite, deixando marcador no payload e devolvendo aviso.

        O marcador é embutido no próprio texto para que o agente alvo saiba que
        recebeu conteúdo incompleto; o aviso retornado é anexado ao resultado da
        tool para que o agente chamador também fique ciente do corte.
        """
        if len(value) <= max_chars:
            return value, None
        original_len = len(value)
        marker = (
            f"\n...[truncado pela tool delegate: {label} original tinha "
            f"{original_len} caracteres, limite {max_chars}]"
        )
        truncated = value[: max(0, max_chars - len(marker))] + marker
        warning = (
            f"{label} excedeu o limite de {max_chars} caracteres e foi truncado "
            f"({original_len} → {max_chars}); o excedente não foi enviado ao agente."
        )
        return truncated, warning

    @staticmethod
    def _attach_truncation_warnings(result: ToolResult, warnings: list[str]) -> ToolResult:
        """Anexa avisos de truncamento de entrada ao resultado da delegação."""
        if not warnings:
            return result
        result.data["truncation_warnings"] = list(warnings)
        notice = "\n".join(f"⚠ Aviso de truncamento: {w}" for w in warnings)
        if result.ok:
            result.content = f"{notice}\n\n{result.content}" if result.content else notice
        return result

    def delegate(self, call: ToolCall) -> ToolResult:
        """Despacha uma tarefa para outro agente Quimera via MCP tool."""
        if not self.is_delegate_available():
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="Agent dispatch not available in this context",
            )
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        target_agent_raw = arguments.get("target_agent")
        request_raw = arguments.get("request")
        context_raw = arguments.get("context")
        role_raw = arguments.get("role")
        access_list_raw = arguments.get("access_list")
        fallback_agents_raw = arguments.get("fallback_agents")
        steps_raw = arguments.get("steps")

        calling_agent = self._get_calling_agent(call)

        orchestrator = (
            self._orchestrator_provider()
            if callable(self._orchestrator_provider)
            else None
        )
        is_orchestrator_call = bool(
            calling_agent
            and orchestrator
            and self._normalize_agent_identity(orchestrator) == calling_agent
        )

        if orchestrator and calling_agent and not is_orchestrator_call:
            # Only block re-delegation for agents that were themselves delegated to by the
            # orchestrator. Agents called directly by the human (parent_agent is None or
            # not the orchestrator) must not be silently blocked — that would break explicit
            # prefix routing like `/codex ...` while orchestrator mode is active.
            parent_of_caller = self._get_parent_agent(call)
            is_in_orchestrated_chain = (
                parent_of_caller is not None
                and parent_of_caller == self._normalize_agent_identity(orchestrator)
            )
            if is_in_orchestrated_chain:
                return ToolResult(
                    ok=False,
                    tool_name=call.name,
                    error=(
                        f"Agent '{calling_agent}' cannot re-delegate while orchestrator mode is active. "
                        "Only the orchestrator may delegate."
                    ),
                )

        truncation_warnings: list[str] = []
        target_agent = str(target_agent_raw).strip() if isinstance(target_agent_raw, str) else ""
        request = str(request_raw).strip() if isinstance(request_raw, str) else ""
        request, request_warning = self._truncate_delegate_text(
            request, self._DELEGATE_MAX_REQUEST_CHARS, "request",
        )
        if request_warning:
            truncation_warnings.append(request_warning)

        if calling_agent and self._normalize_agent_identity(target_agent) == calling_agent:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=f"Agent '{target_agent}' cannot delegate to itself",
            )

        context = ""
        if context_raw is not None:
            if not isinstance(context_raw, str):
                return ToolResult(
                    ok=False,
                    tool_name=call.name,
                    error="'context' must be a string when provided",
                )
            context = context_raw.strip()
            context, context_warning = self._truncate_delegate_text(
                context, self._DELEGATE_MAX_CONTEXT_CHARS, "context",
            )
            if context_warning:
                truncation_warnings.append(context_warning)

        try:
            role = self._normalize_role(role_raw)
            access_list = self._normalize_access_list(access_list_raw)
        except ValueError as exc:
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

        fallback_agents: list[str] = []
        fallback_identities = {self._normalize_agent_identity(target_agent)}
        if calling_agent:
            fallback_identities.add(calling_agent)
        if fallback_agents_raw is not None:
            if not isinstance(fallback_agents_raw, list):
                return ToolResult(
                    ok=False,
                    tool_name=call.name,
                    error="'fallback_agents' must be a list of strings when provided",
                )
            for item in fallback_agents_raw:
                if not isinstance(item, str) or not item.strip():
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error="'fallback_agents' must contain only non-empty strings",
                    )
                normalized_item = self._normalize_agent_identity(item)
                if normalized_item in fallback_identities:
                    continue
                fallback_identities.add(normalized_item)
                fallback_agents.append(item.strip())

        if not target_agent or not request:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="Both 'target_agent' and 'request' are required",
            )
        steps: list[dict] = [
            {
                "target_agent": target_agent,
                "request": request,
                "context": context,
                "role": role,
                "access_list": access_list,
                "fallback_agents": fallback_agents,
                "source_agent": calling_agent or "",
                "delegation_id": self._new_delegation_id(),
            }
        ]

        _ORCHESTRATOR_MAX_STEPS = 3

        if steps_raw is not None:
            if not isinstance(steps_raw, list):
                return ToolResult(
                    ok=False,
                    tool_name=call.name,
                    error="'steps' must be a list of objects when provided",
                )
            if is_orchestrator_call and 1 + len(steps_raw) > _ORCHESTRATOR_MAX_STEPS:
                return ToolResult(
                    ok=False,
                    tool_name=call.name,
                    error=(
                        f"Orchestrator mode allows at most {_ORCHESTRATOR_MAX_STEPS} steps "
                        f"(got {1 + len(steps_raw)}). Split into smaller delegations."
                    ),
                )
            for idx, item in enumerate(steps_raw):
                if not isinstance(item, dict):
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"steps[{idx}] must be an object",
                    )
                extra_agent = item.get("target_agent")
                extra_task = item.get("request")
                extra_context = item.get("context")
                extra_role = item.get("role")
                extra_access_list = item.get("access_list")
                extra_fallback = item.get("fallback_agents", [])

                if not isinstance(extra_agent, str) or not extra_agent.strip():
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"steps[{idx}].target_agent must be a non-empty string",
                    )
                if calling_agent and self._normalize_agent_identity(extra_agent) == calling_agent:
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"steps[{idx}]: agent '{extra_agent}' cannot delegate to itself",
                    )
                if not isinstance(extra_task, str) or not extra_task.strip():
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"steps[{idx}].request must be a non-empty string",
                    )
                if extra_context is not None and not isinstance(extra_context, str):
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"steps[{idx}].context must be a string when provided",
                    )
                try:
                    normalized_role = self._normalize_role(extra_role, f"steps[{idx}].role")
                    normalized_access_list = self._normalize_access_list(
                        extra_access_list,
                        f"steps[{idx}].access_list",
                    )
                except ValueError as exc:
                    return ToolResult(ok=False, tool_name=call.name, error=str(exc))
                if not isinstance(extra_fallback, list):
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"steps[{idx}].fallback_agents must be a list",
                    )
                normalized_extra_fallback: list[str] = []
                extra_fallback_identities = {self._normalize_agent_identity(extra_agent)}
                if calling_agent:
                    extra_fallback_identities.add(calling_agent)
                for fb_idx, fb in enumerate(extra_fallback):
                    if not isinstance(fb, str) or not fb.strip():
                        return ToolResult(
                            ok=False,
                            tool_name=call.name,
                            error=(
                                f"steps[{idx}].fallback_agents[{fb_idx}] "
                                "must be a non-empty string"
                            ),
                        )
                    normalized_fb = self._normalize_agent_identity(fb)
                    if normalized_fb in extra_fallback_identities:
                        continue
                    extra_fallback_identities.add(normalized_fb)
                    normalized_extra_fallback.append(fb.strip())

                normalized_context = extra_context.strip() if isinstance(extra_context, str) else ""
                normalized_context, step_context_warning = self._truncate_delegate_text(
                    normalized_context,
                    self._DELEGATE_MAX_CONTEXT_CHARS,
                    f"steps[{idx}].context",
                )
                if step_context_warning:
                    truncation_warnings.append(step_context_warning)
                normalized_task = extra_task.strip()
                normalized_task, step_task_warning = self._truncate_delegate_text(
                    normalized_task,
                    self._DELEGATE_MAX_REQUEST_CHARS,
                    f"steps[{idx}].request",
                )
                if step_task_warning:
                    truncation_warnings.append(step_task_warning)
                steps.append(
                    {
                        "target_agent": extra_agent.strip(),
                        "request": normalized_task,
                        "context": normalized_context,
                        "role": normalized_role,
                        "access_list": normalized_access_list,
                        "fallback_agents": normalized_extra_fallback,
                        "source_agent": calling_agent or "",
                        "delegation_id": self._new_delegation_id(),
                    }
                )

        parallel_raw = arguments.get("parallel")
        if parallel_raw is not None and not isinstance(parallel_raw, bool):
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="'parallel' must be a boolean when provided",
            )
        parallel = bool(parallel_raw)

        wait_raw = arguments.get("wait")
        if wait_raw is not None and not isinstance(wait_raw, bool):
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="'wait' must be a boolean when provided",
            )
        wait = True if wait_raw is None else wait_raw

        transport = self._get_transport(call)
        if transport == "http_mcp":
            meta_state = call.metadata.get("_mcp_state") or {}
            if meta_state.get("sse_queue") is None:
                # Streamable HTTP sem SSE não sustenta chamada bloqueante
                # longa: a delegação é sempre assíncrona neste transporte.
                wait = False

        if not wait:
            return self._attach_truncation_warnings(
                self._delegate_async(call, steps, parallel),
                truncation_warnings,
            )

        if transport == "http_mcp":
            return self._attach_truncation_warnings(
                self._delegate_http_async(call, steps, parallel),
                truncation_warnings,
            )

        raw_cancel_event = call.metadata.get("_mcp_cancel_event")
        request_cancel_event = (
            raw_cancel_event
            if isinstance(raw_cancel_event, threading.Event)
            else None
        )

        def request_cancelled() -> bool:
            return bool(
                (request_cancel_event is not None and request_cancel_event.is_set())
                or (callable(self._cancel_checker) and self._cancel_checker())
            )

        return self._attach_truncation_warnings(
            self._execute_tracked_sync(
                call,
                steps,
                parallel,
                self._delegate_fn,
                cancel_checker=request_cancelled,
                request_cancel_event=request_cancel_event,
            ),
            truncation_warnings,
        )


class DelegateToolsValidator(ValidatableTool):
    """Validação de policy para as ferramentas de delegação."""

    @staticmethod
    def _validate_role(value: object, field_name: str) -> None:
        if value is None:
            return
        if not isinstance(value, str) or value.strip() not in _DELEGATE_ROLES:
            allowed = ", ".join(sorted(_DELEGATE_ROLES))
            raise ToolPolicyError(f"{field_name} deve ser um destes valores: {allowed}")

    @staticmethod
    def _validate_access_list(value: object, field_name: str) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            raise ToolPolicyError(f"{field_name} deve ser uma lista")
        for idx, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise ToolPolicyError(f"{field_name}[{idx}] deve ser string não vazia")

    def _validate_delegate(self, call: ToolCall) -> None:
        """Valida delegate: bloqueia campos reservados e exige target_agent/request."""
        reserved = {
            "allowlisted",
            "approval_budget",
            "approval_scope_id",
            "transport",
            "run_id",
            "parent_run_id",
        }
        present_reserved = sorted(reserved.intersection(call.arguments))
        if present_reserved:
            raise ToolPolicyError(
                "delegate recebeu campos reservados não confiáveis: "
                + ", ".join(present_reserved)
            )
        target_agent = call.arguments.get("target_agent")
        request = call.arguments.get("request")
        if not isinstance(target_agent, str) or not target_agent.strip():
            raise ToolPolicyError("delegate requer 'target_agent' não vazio")
        if not isinstance(request, str) or not request.strip():
            raise ToolPolicyError("delegate requer 'request' não vazia")
        context = call.arguments.get("context")
        if context is not None and not isinstance(context, str):
            raise ToolPolicyError("delegate.context deve ser string quando fornecido")
        self._validate_role(call.arguments.get("role"), "delegate.role")
        self._validate_access_list(call.arguments.get("access_list"), "delegate.access_list")
        fallback_agents = call.arguments.get("fallback_agents", [])
        if fallback_agents is not None and not isinstance(fallback_agents, list):
            raise ToolPolicyError("delegate.fallback_agents deve ser uma lista")
        wait = call.arguments.get("wait")
        if wait is not None and not isinstance(wait, bool):
            raise ToolPolicyError("delegate.wait deve ser booleano quando fornecido")
        steps = call.arguments.get("steps")
        if steps is not None:
            if not isinstance(steps, list):
                raise ToolPolicyError("delegate.steps deve ser uma lista")
            for i, step in enumerate(steps):
                if not isinstance(step, dict):
                    raise ToolPolicyError(f"delegate.steps[{i}] deve ser um objeto")
                step_target = step.get("target_agent")
                step_request = step.get("request")
                if not isinstance(step_target, str) or not step_target.strip():
                    raise ToolPolicyError(f"delegate.steps[{i}].target_agent não pode ser vazio")
                if not isinstance(step_request, str) or not step_request.strip():
                    raise ToolPolicyError(f"delegate.steps[{i}].request não pode ser vazio")
                self._validate_role(step.get("role"), f"delegate.steps[{i}].role")
                self._validate_access_list(
                    step.get("access_list"),
                    f"delegate.steps[{i}].access_list",
                )

    def _validate_list_agents(self, call: ToolCall) -> None:
        """list_agents não requer argumentos — sempre válida."""


def register(registry, policy, config) -> DelegateTools:
    """Registra as tools de delegação no registry e a validação na policy.

    Retorna a instância de DelegateTools para que o executor possa injetar
    callbacks (set_delegate_fn, set_active_agents_provider, etc.).
    """
    delegate_tools = DelegateTools(config)
    delegate_validator = DelegateToolsValidator(config)
    for name in _DELEGATE_TOOL_NAMES:
        registry.register(name, getattr(delegate_tools, name))
    policy.register_tool_validator(_DELEGATE_TOOL_NAMES, delegate_validator)
    return delegate_tools

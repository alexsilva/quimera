"""Componentes de `quimera.runtime.tools.delegate`."""
from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Protocol, Callable

from ..config import ToolRuntimeConfig
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
    ) -> str | None: ...


class DelegateTools(ToolBase):
    """Ferramentas de delegação entre agentes via MCP."""

    _DELEGATE_MAX_REQUEST_CHARS = 1_200
    _DELEGATE_MAX_CONTEXT_CHARS = 4_000

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de DelegateTools."""
        super().__init__(config)
        self._delegate_fn: _DelegateFnProto | None = None
        self._background_delegate_fn: _DelegateFnProto | None = None
        self._active_agents_provider = None
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

    def list_agents(self, call: ToolCall) -> ToolResult:
        """Retorna a lista de agentes ativos no pool da sessão atual."""
        agents = self._resolve_active_agents()
        agent_list = sorted(agents) if agents else []
        content = json.dumps(agent_list, ensure_ascii=False)
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
        """Retorna db_path como string ou None se não configurado."""
        raw = getattr(self.config, "db_path", None)
        if raw is None:
            return None
        return str(raw)

    # ── async HTTP path ──────────────────────────────────────────────────

    def _delegate_http_async(
        self,
        call: ToolCall,
        steps: list[dict],
    ) -> ToolResult:
        """Executa delegate via HTTP MCP.

        Com SSE: executa inline na thread pool — o resultado chega ao cliente
        via SSE quando a thread pool completar.
        Sem SSE: cria job/task no banco e executa em background thread.
        """
        meta_state = call.metadata.get("_mcp_state") or {}
        sse_available = meta_state.get("sse_queue") is not None

        if sse_available:
            _bg_fn = self._background_delegate_fn or self._delegate_fn
            return self._execute_steps_inner(
                steps,
                _bg_fn,
                self._progress_callback,
                self._resolve_active_agents,
                self._normalize_agent_identity,
                cleanup_callback=self._cleanup_callback,
            )

        db_path = self._get_db_path()
        if not db_path:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="db_path not configured — cannot run delegate async via HTTP MCP",
            )

        step_one = steps[0]
        job_desc = f"delegate → {step_one['target_agent']}: {step_one['request'][:80]}"
        try:
            job_id = add_job(job_desc, created_by="mcp_http", db_path=db_path)
        except Exception as exc:
            return ToolResult(ok=False, tool_name=call.name, error=f"Failed to create job: {exc}")

        body = json.dumps(steps, ensure_ascii=False)
        try:
            task_id = create_task(
                job_id,
                step_one["request"][:120],
                body=body,
                assigned_to=step_one["target_agent"],
                origin="mcp_http_delegate",
                status="in_progress",
                db_path=db_path,
            )
            update_job_status(job_id, "active", db_path=db_path)
            job_snapshot = get_job(job_id, db_path=db_path) or {}
        except Exception as exc:
            return ToolResult(ok=False, tool_name=call.name, error=f"Failed to create task: {exc}")

        _fn = self._background_delegate_fn or self._delegate_fn
        _progress_cb = self._progress_callback
        _resolve_active = self._resolve_active_agents
        _normalize = self._normalize_agent_identity
        _cleanup_cb = self._cleanup_callback

        def _run() -> None:
            result = self._execute_steps_inner(
                steps,
                _fn,
                _progress_cb,
                _resolve_active,
                _normalize,
                cleanup_callback=_cleanup_cb,
            )
            try:
                if result.ok:
                    complete_task(task_id, result=result.content, db_path=db_path)
                    update_job_status(job_id, "completed", db_path=db_path)
                else:
                    fail_task(task_id, reason=result.error, db_path=db_path)
                    update_job_status(job_id, "failed", db_path=db_path)
            except Exception as exc:
                logger.warning(
                    "delegate async: failed to update task/job %d: %s", task_id, exc
                )

        t = threading.Thread(target=_run, daemon=True, name=f"delegate-{task_id}")
        t.start()

        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=json.dumps({
                "job_id": job_id,
                "task_id": task_id,
                "status": "in_progress",
                "job_status": job_snapshot.get("status", "active"),
                "task_status": "in_progress",
                "started_at": job_snapshot.get("started_at"),
            }),
            data={
                "job_id": job_id,
                "task_id": task_id,
                "job_status": job_snapshot.get("status", "active"),
                "task_status": "in_progress",
                "started_at": job_snapshot.get("started_at"),
            },
        )

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
    def _execute_single_step(
        step: dict,
        delegate_fn: _DelegateFnProto,
        progress_callback: Callable[[str], None] | None,
        normalize_agent_fn: Callable[[str | None], str],
        cleanup_callback: Callable[[str], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
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
            try:
                result = delegate_fn(
                    normalized_target_agent,
                    delegation=delegation,
                    delegation_only=True,
                    protocol_mode="delegation",
                    primary=False,
                    silent=False,
                    show_output=False,
                    persist_history=True,
                    history_snapshot=[],
                    max_retries=3,
                    from_agent=source_agent,
                    progress_callback=progress_callback,
                )
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

                selected_agent, step_result, error = DelegateTools._execute_single_step(
                    step, delegate_fn, progress_callback, normalize_agent_fn,
                    cleanup_callback, cancel_checker,
                )

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

    @staticmethod
    def _execute_steps_parallel(
        steps: list[dict],
        delegate_fn: _DelegateFnProto,
        progress_callback: Callable[[str], None] | None,
        resolve_active_agents_fn: Callable[[], set[str]],
        normalize_agent_fn: Callable[[str | None], str],
        cleanup_callback: Callable[[str], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ToolResult:
        """Execução paralela de steps — cada step roda em thread própria."""
        tool_name = "delegate"
        n = len(steps)
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

        results: list[tuple[str | None, str | None, str | None]] = [(None, None, None)] * n

        def run_step(idx: int, step: dict) -> None:
            results[idx] = DelegateTools._execute_single_step(
                step, delegate_fn, progress_callback, normalize_agent_fn,
                cleanup_callback, cancel_checker,
            )

        threads = [
            threading.Thread(
                target=run_step, args=(i, step), daemon=True,
                name=f"delegate-parallel-{i}",
            )
            for i, step in enumerate(steps)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        errors = [
            (i, err) for i, (_, _, err) in enumerate(results) if err is not None
        ]
        if errors:
            error_msg = "; ".join(f"step[{i}]: {e}" for i, e in errors)
            return ToolResult(ok=False, tool_name=tool_name, error=error_msg)

        step_outputs: list[str] = []
        for i, (selected_agent, step_result, _) in enumerate(results):
            if step_result is None:
                continue
            if n == 1:
                step_outputs.append(step_result)
            else:
                label = selected_agent or steps[i]["target_agent"]
                step_outputs.append(f"[{label}] {step_result}")

        return ToolResult(ok=True, tool_name=tool_name, content="\n\n".join(step_outputs))

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

        target_agent = str(target_agent_raw).strip() if isinstance(target_agent_raw, str) else ""
        request = str(request_raw).strip() if isinstance(request_raw, str) else ""
        if len(request) > self._DELEGATE_MAX_REQUEST_CHARS:
            request = request[: self._DELEGATE_MAX_REQUEST_CHARS]

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
            if len(context) > self._DELEGATE_MAX_CONTEXT_CHARS:
                context = context[: self._DELEGATE_MAX_CONTEXT_CHARS]

        fallback_agents: list[str] = []
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
                if calling_agent and self._normalize_agent_identity(item) == calling_agent:
                    continue
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
                if not isinstance(extra_fallback, list):
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"steps[{idx}].fallback_agents must be a list",
                    )
                normalized_extra_fallback: list[str] = []
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
                    if calling_agent and self._normalize_agent_identity(fb) == calling_agent:
                        continue
                    normalized_extra_fallback.append(fb.strip())

                normalized_context = extra_context.strip() if isinstance(extra_context, str) else ""
                if len(normalized_context) > self._DELEGATE_MAX_CONTEXT_CHARS:
                    normalized_context = normalized_context[: self._DELEGATE_MAX_CONTEXT_CHARS]
                normalized_task = extra_task.strip()
                if len(normalized_task) > self._DELEGATE_MAX_REQUEST_CHARS:
                    normalized_task = normalized_task[: self._DELEGATE_MAX_REQUEST_CHARS]
                steps.append(
                    {
                        "target_agent": extra_agent.strip(),
                        "request": normalized_task,
                        "context": normalized_context,
                        "fallback_agents": normalized_extra_fallback,
                        "source_agent": calling_agent or "",
                        "delegation_id": self._new_delegation_id(),
                    }
                )

        parallel_raw = arguments.get("parallel")
        parallel = bool(parallel_raw) if parallel_raw is not None else False

        transport = self._get_transport(call)

        if transport == "http_mcp":
            return self._delegate_http_async(call, steps)

        if parallel and len(steps) > 1:
            return self._execute_steps_parallel(
                steps,
                self._delegate_fn,
                self._progress_callback,
                self._resolve_active_agents,
                self._normalize_agent_identity,
                cleanup_callback=self._cleanup_callback,
                cancel_checker=self._cancel_checker,
            )

        return self._execute_steps_inner(
            steps,
            self._delegate_fn,
            self._progress_callback,
            self._resolve_active_agents,
            self._normalize_agent_identity,
            cleanup_callback=self._cleanup_callback,
            cancel_checker=self._cancel_checker,
        )


class DelegateToolsValidator(ValidatableTool):
    """Validação de policy para as ferramentas de delegação."""

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
        fallback_agents = call.arguments.get("fallback_agents", [])
        if fallback_agents is not None and not isinstance(fallback_agents, list):
            raise ToolPolicyError("delegate.fallback_agents deve ser uma lista")
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

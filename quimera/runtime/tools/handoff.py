"""Componentes de `quimera.runtime.tools.handoff`."""
from __future__ import annotations

import logging
from typing import Protocol, Callable

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult

logger = logging.getLogger(__name__)


class _CallAgentFnProto(Protocol):
    """Protocolo para a função de despacho de tarefas entre agentes."""

    def __call__(
        self,
        agent: str,
        *,
        handoff: dict[str, object] | None = None,
        handoff_only: bool = True,
        protocol_mode: str = "handoff",
        primary: bool = False,
        silent: bool = True,
        show_output: bool = False,
        persist_history: bool = True,
        history_snapshot: list | None = None,
        max_retries: int = 1,
        progress_callback: Callable[[str], None] | None = None,
    ) -> str | None: ...


class HandoffTools:
    """Implementa `HandoffTools` — delegação entre agentes via MCP."""
    _CALL_AGENT_MAX_TASK_CHARS = 1_200
    _CALL_AGENT_MAX_CONTEXT_CHARS = 4_000

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de HandoffTools."""
        self.config = config
        self._call_agent_fn: _CallAgentFnProto | None = None
        self._active_agents_provider = None
        self._progress_callback: Callable[[str], None] | None = None

    def set_call_agent_fn(self, fn: _CallAgentFnProto) -> None:
        """Injeta callable para despachar tarefas a outro agente."""
        self._call_agent_fn = fn

    def set_active_agents_provider(self, fn) -> None:
        """Injeta provider que retorna agentes ativos no momento da delegação."""
        self._active_agents_provider = fn

    def set_progress_callback(self, fn: Callable[[str], None] | None) -> None:
        """Injeta callback para reporte de progresso."""
        self._progress_callback = fn

    def is_call_agent_available(self) -> bool:
        """Indica se a tool call_agent está operável no contexto atual."""
        return callable(self._call_agent_fn)

    @staticmethod
    def _normalize_agent_identity(agent_name: str | None) -> str:
        if agent_name is None:
            return ""
        return str(agent_name).strip().lower().lstrip("/")

    def _resolve_active_agents(self) -> set[str]:
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

    def call_agent(self, call: ToolCall) -> ToolResult:
        """Dispatch a task to another Quimera agent via MCP tool."""
        if not self.is_call_agent_available():
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="Agent dispatch not available in this context",
            )
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        agent_name_raw = arguments.get("agent_name")
        task_raw = arguments.get("task")
        context_raw = arguments.get("context")
        fallback_agents_raw = arguments.get("fallback_agents")
        handoffs_raw = arguments.get("handoffs")

        agent_name = str(agent_name_raw).strip() if isinstance(agent_name_raw, str) else ""
        task = str(task_raw).strip() if isinstance(task_raw, str) else ""
        if len(task) > self._CALL_AGENT_MAX_TASK_CHARS:
            task = task[: self._CALL_AGENT_MAX_TASK_CHARS]
        context = ""
        if context_raw is not None:
            if not isinstance(context_raw, str):
                return ToolResult(
                    ok=False,
                    tool_name=call.name,
                    error="'context' must be a string when provided",
                )
            context = context_raw.strip()
            if len(context) > self._CALL_AGENT_MAX_CONTEXT_CHARS:
                context = context[: self._CALL_AGENT_MAX_CONTEXT_CHARS]

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
                fallback_agents.append(item.strip())

        if not agent_name or not task:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="Both 'agent_name' and 'task' are required",
            )
        steps: list[dict] = [
            {
                "agent_name": agent_name,
                "task": task,
                "context": context,
                "fallback_agents": fallback_agents,
            }
        ]

        if handoffs_raw is not None:
            if not isinstance(handoffs_raw, list):
                return ToolResult(
                    ok=False,
                    tool_name=call.name,
                    error="'handoffs' must be a list of objects when provided",
                )
            for idx, item in enumerate(handoffs_raw):
                if not isinstance(item, dict):
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"handoffs[{idx}] must be an object",
                    )
                extra_agent = item.get("agent_name")
                extra_task = item.get("task")
                extra_context = item.get("context")
                extra_fallback = item.get("fallback_agents", [])

                if not isinstance(extra_agent, str) or not extra_agent.strip():
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"handoffs[{idx}].agent_name must be a non-empty string",
                    )
                if not isinstance(extra_task, str) or not extra_task.strip():
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"handoffs[{idx}].task must be a non-empty string",
                    )
                if extra_context is not None and not isinstance(extra_context, str):
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"handoffs[{idx}].context must be a string when provided",
                    )
                if not isinstance(extra_fallback, list):
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=f"handoffs[{idx}].fallback_agents must be a list",
                    )
                normalized_extra_fallback: list[str] = []
                for fb_idx, fb in enumerate(extra_fallback):
                    if not isinstance(fb, str) or not fb.strip():
                        return ToolResult(
                            ok=False,
                            tool_name=call.name,
                            error=(
                                f"handoffs[{idx}].fallback_agents[{fb_idx}] must be a non-empty string"
                            ),
                        )
                    normalized_extra_fallback.append(fb.strip())

                normalized_context = extra_context.strip() if isinstance(extra_context, str) else ""
                if len(normalized_context) > self._CALL_AGENT_MAX_CONTEXT_CHARS:
                    normalized_context = normalized_context[: self._CALL_AGENT_MAX_CONTEXT_CHARS]
                normalized_task = extra_task.strip()
                if len(normalized_task) > self._CALL_AGENT_MAX_TASK_CHARS:
                    normalized_task = normalized_task[: self._CALL_AGENT_MAX_TASK_CHARS]
                steps.append(
                    {
                        "agent_name": extra_agent.strip(),
                        "task": normalized_task,
                        "context": normalized_context,
                        "fallback_agents": normalized_extra_fallback,
                    }
                )

        try:
            step_outputs: list[str] = []
            for step in steps:
                # Re-validate active agents for each step to handle agents that may have become inactive
                active_agents = self._resolve_active_agents()
                if active_agents:
                    invalid_targets: list[str] = []
                    targets = [step["agent_name"], *step["fallback_agents"]]
                    for target in targets:
                        normalized_target = self._normalize_agent_identity(target)
                        if normalized_target and normalized_target not in active_agents:
                            invalid_targets.append(target)
                    if invalid_targets:
                        invalid_label = ", ".join(dict.fromkeys(invalid_targets))
                        active_label = ", ".join(sorted(active_agents))
                        return ToolResult(
                            ok=False,
                            tool_name=call.name,
                            error=f"Agents not active in current pool: {invalid_label}. Active agents: {active_label}",
                        )

                attempt_targets = [step["agent_name"], *step["fallback_agents"]]
                step_result = None
                last_error = None
                selected_agent = None
                for target_agent in attempt_targets:
                    normalized_target_agent = self._normalize_agent_identity(target_agent)
                    handoff = {
                        "task": step["task"],
                        "context": step["context"],
                    }
                    try:
                        result = self._call_agent_fn(
                            normalized_target_agent,
                            handoff=handoff,
                            handoff_only=True,
                            protocol_mode="handoff",
                            primary=False,
                            silent=False,
                            show_output=False,
                            persist_history=True,
                            # Evita reenviar todo o histórico da conversa principal
                            # para o subagente em handoff MCP (reduz latência/timeout).
                            history_snapshot=[],
                            max_retries=3,
                            progress_callback=self._progress_callback,
                        )
                    except Exception as dispatch_error:  # noqa: BLE001
                        last_error = str(dispatch_error)
                        logger.warning(
                            "call_agent: dispatch to '%s' failed after %d attempt(s): %s",
                            target_agent, len(step_outputs) + 1, last_error,
                        )
                        continue
                    if result is None:
                        last_error = f"Agent '{target_agent}' returned no response"
                        logger.warning(
                            "call_agent: dispatch to '%s' returned no response after %d attempt(s)",
                            target_agent, len(step_outputs) + 1,
                        )
                        continue
                    selected_agent = target_agent
                    step_result = str(result)
                    break

                if step_result is None:
                    error_detail = (
                        f"{last_error}. Tried: {', '.join(attempt_targets)}"
                        if last_error
                        else f"No response from any target. Tried: {', '.join(attempt_targets)}"
                    )
                    return ToolResult(
                        ok=False,
                        tool_name=call.name,
                        error=error_detail,
                    )

                if len(steps) == 1:
                    step_outputs.append(step_result)
                else:
                    step_outputs.append(f"[{selected_agent}] {step_result}")

            content = "\n\n".join(step_outputs)
            return ToolResult(ok=True, tool_name=call.name, content=content)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=str(exc),
            )

"""Ferramentas de memória determinística por workspace."""

from __future__ import annotations

from quimera.runtime.approval_broker import TrustedToolExecutionContext
from quimera.workspace_memory import WorkspaceMemoryStore

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult


class MemoryTools:
    """Expõe memória estruturada por workspace via memory.json interno."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        self.config = config
        self._store: WorkspaceMemoryStore | None = None

    def memory_save(self, call: ToolCall) -> ToolResult:
        try:
            store = self._get_store()
            result = store.save(
                namespace=str(call.arguments.get("namespace", "")),
                key=str(call.arguments.get("key", "")),
                value=call.arguments.get("value"),
                ttl_seconds=call.arguments.get("ttl_seconds"),
                actor=self._actor_from_call(call),
            )
        except (ValueError, RuntimeError) as exc:
            return ToolResult(ok=False, tool_name=call.name, content="", error=str(exc))

        payload = {
            "ok": True,
            "revision": result.revision,
            "namespace": result.namespace,
            "key": result.key,
            "updated_at": result.updated_at,
        }
        return ToolResult(ok=True, tool_name=call.name, content=str(payload), data=payload)

    def memory_retrieve(self, call: ToolCall) -> ToolResult:
        try:
            store = self._get_store()
            result = store.retrieve(
                namespace=call.arguments.get("namespace"),
                key=call.arguments.get("key"),
                prefix=call.arguments.get("prefix"),
                tags=call.arguments.get("tags"),
                limit=call.arguments.get("limit"),
            )
        except (ValueError, RuntimeError) as exc:
            return ToolResult(ok=False, tool_name=call.name, content="", error=str(exc))

        payload = {
            "ok": True,
            "revision": result["revision"],
            "entries": result["entries"],
        }
        return ToolResult(ok=True, tool_name=call.name, content=str(payload), data=payload)

    def _get_store(self) -> WorkspaceMemoryStore:
        memory_file = self.config.memory_file
        if memory_file is None:
            raise RuntimeError("memory storage não configurado para este workspace")
        if self._store is None:
            self._store = WorkspaceMemoryStore(memory_file)
        return self._store

    @staticmethod
    def _actor_from_call(call: ToolCall) -> str | None:
        trusted = TrustedToolExecutionContext.from_trusted_metadata(call.metadata)
        if trusted.agent_name:
            return trusted.agent_name
        raw = call.metadata.get("calling_agent")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

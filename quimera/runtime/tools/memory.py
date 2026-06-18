"""Ferramentas de memória determinística por workspace."""
from __future__ import annotations

import json

from quimera.runtime.approval_broker import TrustedToolExecutionContext
from quimera.workspace_memory import WorkspaceMemoryStore

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError
from .base import ToolBase, ValidatableTool


class MemoryTools(ToolBase, tool_prefix="memory"):
    """Expõe memória estruturada por workspace via memory.json interno."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de MemoryTools."""
        super().__init__(config)
        self._store: WorkspaceMemoryStore | None = None

    def memory_save(self, call: ToolCall) -> ToolResult:
        """Persiste um valor no store de memória do workspace."""
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
        """Recupera entradas do store de memória do workspace."""
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
        """Retorna (criando se necessário) o store de memória do workspace."""
        memory_file = self.config.memory_file
        if memory_file is None:
            raise RuntimeError("memory storage não configurado para este workspace")
        if self._store is None:
            self._store = WorkspaceMemoryStore(memory_file)
        return self._store

    @staticmethod
    def _actor_from_call(call: ToolCall) -> str | None:
        """Extrai o nome do agente que originou o call."""
        trusted = TrustedToolExecutionContext.from_trusted_metadata(call.metadata)
        if trusted.agent_name:
            return trusted.agent_name
        raw = call.metadata.get("calling_agent")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None


class MemoryToolsValidator(ValidatableTool):
    """Validação de policy para as ferramentas de memória."""

    _ALLOWED_TOKEN_CHARS = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    )

    def _validate_memory_save(self, call: ToolCall) -> None:
        """Valida memory_save: namespace, key e value obrigatórios e seguros."""
        namespace = call.arguments.get("namespace")
        key = call.arguments.get("key")
        if not isinstance(namespace, str) or not namespace.strip():
            raise ToolPolicyError("memory_save requer 'namespace' não vazio")
        if not isinstance(key, str) or not key.strip():
            raise ToolPolicyError("memory_save requer 'key' não vazio")
        self._validate_memory_token(namespace, field_name="namespace")
        self._validate_memory_token(key, field_name="key")
        if "value" not in call.arguments:
            raise ToolPolicyError("memory_save requer 'value'")
        try:
            serialized = json.dumps(call.arguments.get("value"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ToolPolicyError("memory_save requer 'value' JSON-serializable") from exc
        if len(serialized.encode("utf-8")) > 32_000:
            raise ToolPolicyError(
                "memory_save rejeitou value grande demais; limite de 32000 bytes serializados"
            )
        ttl = call.arguments.get("ttl_seconds")
        if ttl is not None:
            try:
                ttl_int = int(ttl)
            except (TypeError, ValueError) as exc:
                raise ToolPolicyError("memory_save.ttl_seconds deve ser inteiro positivo") from exc
            if ttl_int <= 0:
                raise ToolPolicyError("memory_save.ttl_seconds deve ser inteiro positivo")

    def _validate_memory_retrieve(self, call: ToolCall) -> None:
        """Valida memory_retrieve: campos opcionais devem ser strings seguras."""
        namespace = call.arguments.get("namespace")
        key = call.arguments.get("key")
        prefix = call.arguments.get("prefix")
        if namespace is not None:
            if not isinstance(namespace, str) or not namespace.strip():
                raise ToolPolicyError("memory_retrieve.namespace deve ser string não vazia")
            self._validate_memory_token(namespace, field_name="namespace")
        if key is not None:
            if not isinstance(key, str) or not key.strip():
                raise ToolPolicyError("memory_retrieve.key deve ser string não vazia")
            self._validate_memory_token(key, field_name="key")
        if prefix is not None:
            if not isinstance(prefix, str) or not prefix.strip():
                raise ToolPolicyError("memory_retrieve.prefix deve ser string não vazia")
            self._validate_memory_token(prefix, field_name="prefix")
        tags = call.arguments.get("tags")
        if tags is not None:
            if not isinstance(tags, list):
                raise ToolPolicyError("memory_retrieve.tags deve ser lista de strings")
            for tag in tags:
                if not isinstance(tag, str) or not tag.strip():
                    raise ToolPolicyError(
                        "memory_retrieve.tags deve conter apenas strings não vazias"
                    )
                self._validate_memory_token(tag, field_name="tag")
        limit = call.arguments.get("limit")
        if limit is not None:
            try:
                limit_int = int(limit)
            except (TypeError, ValueError) as exc:
                raise ToolPolicyError("memory_retrieve.limit deve ser inteiro positivo") from exc
            if limit_int <= 0:
                raise ToolPolicyError("memory_retrieve.limit deve ser inteiro positivo")

    def _validate_memory_token(self, value: str, *, field_name: str) -> None:
        """Valida que um token de memória não contém paths nem caracteres inválidos."""
        if value.startswith("/") or "/" in value or "\\" in value:
            raise ToolPolicyError(f"{field_name} não pode conter path")
        if any(ch not in self._ALLOWED_TOKEN_CHARS for ch in value):
            raise ToolPolicyError(
                f"{field_name} contém caracteres inválidos; "
                "use apenas letras, números, '.', '_', ':' ou '-'"
            )


def register(registry, policy, config) -> None:
    """Registra todas as tools de memória no registry e a validação na policy."""
    memory_tools = MemoryTools(config)
    memory_validator = MemoryToolsValidator(config)
    tool_names = [name for name in dir(MemoryTools) if name.startswith("memory_")]
    for name in tool_names:
        registry.register(name, getattr(memory_tools, name))
    policy.register_tool_validator(tool_names, memory_validator)

"""Materialização e filtragem dos schemas de ferramentas do runtime.

Os contratos nativos vivem em :mod:`quimera.runtime.drivers.tool_catalog`.
Este módulo mantém a API pública histórica ``TOOL_SCHEMAS`` e concentra apenas
as responsabilidades dinâmicas: bridge MCP e filtragem conforme capabilities do
executor ativo.
"""
from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy

from ..tool_metadata import get_tool_metadata
from .tool_catalog import TOOL_SPECS, ToolSpec, materialize_tool_schemas

_BRIDGE_SCHEMAS: list[dict] = []


def set_bridge_schemas(schemas: list[dict]) -> None:
    """Substitui os schemas de ferramentas bridgeadas de servidores MCP."""
    _BRIDGE_SCHEMAS.clear()
    _BRIDGE_SCHEMAS.extend(deepcopy(schemas))


def get_bridge_schemas() -> list[dict]:
    """Retorna cópias independentes dos schemas bridgeados."""
    return deepcopy(_BRIDGE_SCHEMAS)


# API pública histórica. Cada item é materializado a partir do catálogo tipado,
# preservando exatamente o JSON Schema usado antes desta reorganização.
TOOL_SCHEMAS = materialize_tool_schemas()

_CAPABILITY_METHODS = {
    "delegate": "is_delegate_available",
    "tasks": "is_tasks_available",
    "ask_user": "is_ask_user_available",
    "update_shared_state": "is_update_state_available",
}


def _capability_available(tool_executor, capability: str) -> bool:
    if capability == "task_db":
        config = getattr(tool_executor, "config", None)
        return config is not None and getattr(config, "db_path", None) is not None
    method_name = _CAPABILITY_METHODS.get(capability)
    if method_name is None:
        return False
    checker = getattr(tool_executor, method_name, None)
    return bool(callable(checker) and checker())


def resolve_tool_schemas(tool_executor=None) -> list[dict]:
    """Retorna somente schemas coerentes com o executor e a policy atuais."""
    schemas = list(TOOL_SCHEMAS)
    schemas.extend(get_bridge_schemas())
    if tool_executor is None:
        return schemas

    registry = getattr(tool_executor, "registry", None)
    if registry is not None and hasattr(registry, "names"):
        registry_names = registry.names()
        if isinstance(registry_names, Iterable) and not isinstance(
            registry_names,
            (str, bytes, dict),
        ):
            enabled_names = set(registry_names)
            schemas = [
                schema
                for schema in schemas
                if schema["function"]["name"] in enabled_names
            ]

    schemas = [
        schema
        for schema in schemas
        if (
            (metadata := get_tool_metadata(schema["function"]["name"])) is None
            or all(
                _capability_available(tool_executor, capability)
                for capability in metadata.capabilities
            )
        )
    ]

    policy = getattr(tool_executor, "policy", None)
    allowed_tools = getattr(policy, "allowed_tools", None)
    if isinstance(allowed_tools, (list, tuple, set, frozenset)):
        allowed_names = set(allowed_tools)
        schemas = [
            schema
            for schema in schemas
            if schema["function"]["name"] in allowed_names
        ]
    blocked_tools = getattr(policy, "blocked_tools", None)
    if blocked_tools:
        blocked_names = set(blocked_tools)
        schemas = [
            schema
            for schema in schemas
            if schema["function"]["name"] not in blocked_names
        ]

    return schemas


__all__ = [
    "TOOL_SCHEMAS",
    "TOOL_SPECS",
    "ToolSpec",
    "get_bridge_schemas",
    "resolve_tool_schemas",
    "set_bridge_schemas",
]

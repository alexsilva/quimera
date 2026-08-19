"""Contratos do catálogo declarativo de ferramentas."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.drivers.tool_catalog import TOOL_SPECS, materialize_tool_schemas
from quimera.runtime.drivers.tool_schemas import TOOL_SCHEMAS
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.tools import memory as memory_tools
from quimera.runtime.tools import todo as todo_tools


_EXPECTED_SCHEMA_FINGERPRINT = (
    "6ecaff3c0f41203556564bf23b825f8d704411e6f7da1e28994969ae78e4c42a"
)


def _fingerprint(schemas: list[dict]) -> str:
    payload = json.dumps(
        schemas,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_catalog_materializes_the_public_schema_without_contract_changes():
    """A representação tipada não pode alterar o contrato publicado."""
    assert materialize_tool_schemas() == TOOL_SCHEMAS
    assert len(TOOL_SPECS) == len(TOOL_SCHEMAS) == 50
    assert _fingerprint(TOOL_SCHEMAS) == _EXPECTED_SCHEMA_FINGERPRINT


def test_materialization_does_not_share_nested_mutable_structures():
    """Um consumidor não deve corromper o catálogo nem outra materialização."""
    first = materialize_tool_schemas()
    second = materialize_tool_schemas()

    first[0]["function"]["parameters"]["properties"]["path"]["description"] = "changed"

    assert first != second
    assert second == TOOL_SCHEMAS


def test_schema_and_registry_are_one_to_one():
    """Cada schema publicado deve ter handler no registry e vice-versa."""
    root = Path(tempfile.mkdtemp())
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=root), approval_handler=None)
    registry_names = set(executor.registry.names())
    schema_names = {item["function"]["name"] for item in TOOL_SCHEMAS}

    assert registry_names == schema_names
    assert len(registry_names) == 50


def test_explicit_tool_name_lists_match_registered_handlers():
    """Listas explícitas dos módulos não podem divergir do que o register expõe."""
    root = Path(tempfile.mkdtemp())
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=root), approval_handler=None)
    registered = set(executor.registry.names())

    for names in (
        memory_tools._MEMORY_TOOL_NAMES,
        todo_tools._TODO_TOOL_NAMES,
        ["update_shared_state"],
    ):
        for name in names:
            assert name in registered, name


def test_memory_todo_state_contracts_are_available_on_registry(tmp_path: Path):
    """Contratos mínimos de memory/todo/state devem existir e responder."""
    config = ToolRuntimeConfig(workspace_root=tmp_path, memory_file=tmp_path / "memory.json")
    executor = ToolExecutor(config, approval_handler=None)

    for name in ("memory_save", "memory_retrieve", "todo_write", "todo_list", "update_shared_state"):
        assert executor.registry.get(name) is not None

    save = executor.registry.get("memory_save")(
        ToolCall(
            "memory_save",
            {"namespace": "session", "key": "k1", "value": "v1"},
        )
    )
    assert save.ok is True

    retrieve = executor.registry.get("memory_retrieve")(
        ToolCall("memory_retrieve", {"namespace": "session", "key": "k1"})
    )
    assert retrieve.ok is True

    written = executor.registry.get("todo_write")(
        ToolCall(
            "todo_write",
            {
                "todos": [
                    {"id": "1", "content": "item", "status": "pending"},
                ]
            },
        )
    )
    # Fora de job ativo a tool falha de forma controlada, mas o handler existe.
    assert written.ok is False
    assert "QUIMERA_CURRENT_JOB_ID" in (written.error or "")

    listed = executor.registry.get("todo_list")(ToolCall("todo_list", {}))
    assert listed.ok is False

    state = executor.registry.get("update_shared_state")(
        ToolCall("update_shared_state", {"updates": {"phase": "x"}})
    )
    assert state.ok is False
    assert "não está disponível" in (state.error or "")

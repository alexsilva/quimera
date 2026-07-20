"""Contratos do catálogo declarativo de ferramentas."""
from __future__ import annotations

import hashlib
import json

from quimera.runtime.drivers.tool_catalog import TOOL_SPECS, materialize_tool_schemas
from quimera.runtime.drivers.tool_schemas import TOOL_SCHEMAS


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

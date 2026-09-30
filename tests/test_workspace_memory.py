"""Testes do WorkspaceMemoryStore: comportamento legado fixado e tags explícitas."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quimera.workspace_memory import WorkspaceMemoryStore


@pytest.fixture()
def store(tmp_path: Path) -> WorkspaceMemoryStore:
    return WorkspaceMemoryStore(tmp_path / "memory.json")


def _entries(result: dict) -> list[dict]:
    return result["entries"]


# ── comportamento legado: roundtrip, upsert e metadados ───────


def test_save_and_retrieve_roundtrip(store: WorkspaceMemoryStore):
    saved = store.save(
        namespace="rules",
        key="exit-code",
        value={"rule": "validar exit real"},
        ttl_seconds=None,
        actor="claude-fable",
    )
    assert saved.revision == 1
    assert saved.namespace == "rules"
    assert saved.key == "exit-code"

    result = store.retrieve(namespace="rules", key="exit-code", prefix=None, tags=None, limit=None)
    assert result["revision"] == 1
    (entry,) = _entries(result)
    assert entry["value"] == {"rule": "validar exit real"}
    assert entry["tags"] == []
    assert entry["created_by"] == "claude-fable"
    assert entry["updated_by"] == "claude-fable"
    assert entry["ttl_seconds_remaining"] is None


def test_update_preserves_created_metadata_and_bumps_revision(store: WorkspaceMemoryStore):
    store.save(namespace="rules", key="k", value={"v": 1}, ttl_seconds=None, actor="claude-fable")
    first = _entries(store.retrieve(namespace="rules", key="k", prefix=None, tags=None, limit=None))[0]

    saved = store.save(namespace="rules", key="k", value={"v": 2}, ttl_seconds=None, actor="codex")
    assert saved.revision == 2

    entry = _entries(store.retrieve(namespace="rules", key="k", prefix=None, tags=None, limit=None))[0]
    assert entry["value"] == {"v": 2}
    assert entry["created_at"] == first["created_at"]
    assert entry["created_by"] == "claude-fable"
    assert entry["updated_by"] == "codex"


def test_retrieve_filters_by_prefix_and_limit(store: WorkspaceMemoryStore):
    for key in ("aws-design", "aws-review", "git-worktree"):
        store.save(namespace="workspace", key=key, value={"k": key}, ttl_seconds=None, actor=None)

    result = store.retrieve(namespace="workspace", key=None, prefix="aws-", tags=None, limit=None)
    assert [e["key"] for e in _entries(result)] == ["aws-design", "aws-review"]

    limited = store.retrieve(namespace=None, key=None, prefix=None, tags=None, limit=1)
    assert len(_entries(limited)) == 1


# ── comportamento legado: tags extraídas de value.tags ────────


def test_legacy_tags_extracted_from_value_dict(store: WorkspaceMemoryStore):
    store.save(
        namespace="workspace",
        key="aws-design",
        value={"doc": "design do plugin", "tags": ["aws", "design"]},
        ttl_seconds=None,
        actor=None,
    )
    entry = _entries(store.retrieve(namespace="workspace", key=None, prefix=None, tags=None, limit=None))[0]
    assert entry["tags"] == ["aws", "design"]

    filtered = store.retrieve(namespace=None, key=None, prefix=None, tags=["aws"], limit=None)
    assert [e["key"] for e in _entries(filtered)] == ["aws-design"]

    missing = store.retrieve(namespace=None, key=None, prefix=None, tags=["aws", "outra"], limit=None)
    assert _entries(missing) == []


def test_legacy_value_without_tags_yields_empty_tags(store: WorkspaceMemoryStore):
    store.save(namespace="rules", key="a", value={"rule": "x"}, ttl_seconds=None, actor=None)
    store.save(namespace="rules", key="b", value="texto puro", ttl_seconds=None, actor=None)
    for entry in _entries(store.retrieve(namespace="rules", key=None, prefix=None, tags=None, limit=None)):
        assert entry["tags"] == []


def test_legacy_value_tags_with_path_rejected(store: WorkspaceMemoryStore):
    with pytest.raises(ValueError, match="tags não podem conter path"):
        store.save(
            namespace="rules",
            key="k",
            value={"tags": ["ok", "../etc"]},
            ttl_seconds=None,
            actor=None,
        )


def test_legacy_update_without_value_tags_clears_tags(store: WorkspaceMemoryStore):
    store.save(namespace="w", key="k", value={"tags": ["a"]}, ttl_seconds=None, actor=None)
    store.save(namespace="w", key="k", value={"doc": "novo"}, ttl_seconds=None, actor=None)
    entry = _entries(store.retrieve(namespace="w", key="k", prefix=None, tags=None, limit=None))[0]
    assert entry["tags"] == []


# ── comportamento legado: arquivo antigo continua legível ─────


_LEGACY_FILE = {
    "revision": 7,
    "updated_at": "2026-07-10T10:00:00+00:00",
    "entries": {
        "todo": {
            "debug-checkbox": {
                "namespace": "todo",
                "key": "debug-checkbox",
                "value": {"status": "in_progress"},
                "created_at": "2026-07-10T10:00:00+00:00",
                "updated_at": "2026-07-10T10:00:00+00:00",
            },
            "expirado": {
                "namespace": "todo",
                "key": "expirado",
                "value": {"status": "done"},
                "expires_at": "2026-07-11T10:00:00+00:00",
            },
            "corrompido": "não sou um objeto",
        }
    },
}


def _write_legacy_file(path: Path) -> None:
    path.write_text(json.dumps(_LEGACY_FILE, ensure_ascii=False, indent=2), encoding="utf-8")


def test_legacy_file_without_new_fields_is_readable(tmp_path: Path):
    memory_file = tmp_path / "memory.json"
    _write_legacy_file(memory_file)
    store = WorkspaceMemoryStore(memory_file)

    result = store.retrieve(namespace="todo", key=None, prefix=None, tags=None, limit=None)
    (entry,) = _entries(result)
    assert entry["key"] == "debug-checkbox"
    assert entry["tags"] == []
    assert entry["created_by"] is None
    assert entry["updated_by"] is None
    assert entry["ttl_seconds_remaining"] is None


def test_legacy_file_prunes_expired_and_corrupted_entries(tmp_path: Path):
    memory_file = tmp_path / "memory.json"
    _write_legacy_file(memory_file)
    store = WorkspaceMemoryStore(memory_file)

    store.retrieve(namespace=None, key=None, prefix=None, tags=None, limit=None)

    on_disk = json.loads(memory_file.read_text(encoding="utf-8"))
    assert set(on_disk["entries"]["todo"].keys()) == {"debug-checkbox"}


def test_legacy_file_update_keeps_other_entries_intact(tmp_path: Path):
    memory_file = tmp_path / "memory.json"
    _write_legacy_file(memory_file)
    store = WorkspaceMemoryStore(memory_file)

    saved = store.save(namespace="rules", key="nova", value={"rule": "x"}, ttl_seconds=None, actor="claude-fable")
    assert saved.revision == 8

    on_disk = json.loads(memory_file.read_text(encoding="utf-8"))
    legacy_entry = on_disk["entries"]["todo"]["debug-checkbox"]
    assert legacy_entry["value"] == {"status": "in_progress"}
    assert legacy_entry["created_at"] == "2026-07-10T10:00:00+00:00"


def test_list_namespaces_counts(tmp_path: Path):
    memory_file = tmp_path / "memory.json"
    _write_legacy_file(memory_file)
    store = WorkspaceMemoryStore(memory_file)
    store.save(namespace="rules", key="a", value={"v": 1}, ttl_seconds=None, actor=None)
    store.save(namespace="rules", key="b", value={"v": 2}, ttl_seconds=None, actor=None)

    result = store.list_namespaces()
    as_map = {item["namespace"]: item["keys"] for item in result["namespaces"]}
    assert as_map == {"todo": 1, "rules": 2}


# ── comportamento legado: delete e validações ─────────────────


def test_delete_key_and_namespace(store: WorkspaceMemoryStore):
    store.save(namespace="w", key="a", value=1, ttl_seconds=None, actor=None)
    store.save(namespace="w", key="b", value=2, ttl_seconds=None, actor=None)

    removed = store.delete(namespace="w", key="a")
    assert removed["removed"] == 1

    removed_all = store.delete(namespace="w")
    assert removed_all["removed"] == 1
    assert store.list_namespaces()["namespaces"] == []


def test_delete_missing_does_not_bump_revision(store: WorkspaceMemoryStore):
    store.save(namespace="w", key="a", value=1, ttl_seconds=None, actor=None)
    before = store.retrieve(namespace=None, key=None, prefix=None, tags=None, limit=None)["revision"]
    result = store.delete(namespace="w", key="inexistente")
    assert result["removed"] == 0
    assert result["revision"] == before


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        pytest.param({"namespace": "../w", "key": "k"}, "não pode conter path", id="namespace-path"),
        pytest.param({"namespace": "w", "key": "a b"}, "caracteres inválidos", id="key-invalida"),
        pytest.param({"namespace": "", "key": "k"}, "não pode ser vazio", id="namespace-vazio"),
    ],
)
def test_save_rejects_invalid_tokens(store: WorkspaceMemoryStore, kwargs: dict, match: str):
    with pytest.raises(ValueError, match=match):
        store.save(value={"v": 1}, ttl_seconds=None, actor=None, **kwargs)


def test_save_rejects_oversized_value(store: WorkspaceMemoryStore):
    with pytest.raises(ValueError, match="excede o limite"):
        store.save(namespace="w", key="k", value={"blob": "x" * 40_000}, ttl_seconds=None, actor=None)


@pytest.mark.parametrize("ttl", [0, -5, "abc"], ids=["zero", "negativo", "nao-inteiro"])
def test_save_rejects_invalid_ttl(store: WorkspaceMemoryStore, ttl):
    with pytest.raises(ValueError, match="ttl_seconds deve ser inteiro positivo"):
        store.save(namespace="w", key="k", value=1, ttl_seconds=ttl, actor=None)


def test_ttl_entry_reports_remaining_seconds(store: WorkspaceMemoryStore):
    store.save(namespace="w", key="k", value=1, ttl_seconds=3600, actor=None)
    entry = _entries(store.retrieve(namespace="w", key="k", prefix=None, tags=None, limit=None))[0]
    assert 0 < entry["ttl_seconds_remaining"] <= 3600


# ── tags explícitas no save ───────────────────────────────────


def test_save_with_explicit_tags_persists_and_filters(store: WorkspaceMemoryStore):
    saved = store.save(
        namespace="rules",
        key="exit-code",
        value={"rule": "validar exit real"},
        ttl_seconds=None,
        actor="claude-fable",
        tags=["testing", "shell"],
    )
    assert saved.tags == ["testing", "shell"]

    filtered = store.retrieve(namespace=None, key=None, prefix=None, tags=["testing"], limit=None)
    assert [e["key"] for e in _entries(filtered)] == ["exit-code"]
    assert _entries(filtered)[0]["tags"] == ["testing", "shell"]


def test_explicit_tags_override_value_tags(store: WorkspaceMemoryStore):
    store.save(
        namespace="w",
        key="k",
        value={"doc": "x", "tags": ["do-value"]},
        ttl_seconds=None,
        actor=None,
        tags=["explicita"],
    )
    entry = _entries(store.retrieve(namespace="w", key="k", prefix=None, tags=None, limit=None))[0]
    assert entry["tags"] == ["explicita"]


def test_explicit_empty_tags_clear_previous_tags(store: WorkspaceMemoryStore):
    store.save(namespace="w", key="k", value={"v": 1}, ttl_seconds=None, actor=None, tags=["a"])
    store.save(namespace="w", key="k", value={"v": 2, "tags": ["do-value"]}, ttl_seconds=None, actor=None, tags=[])
    entry = _entries(store.retrieve(namespace="w", key="k", prefix=None, tags=None, limit=None))[0]
    assert entry["tags"] == []


def test_omitted_tags_on_update_keep_legacy_extraction(store: WorkspaceMemoryStore):
    store.save(namespace="w", key="k", value={"v": 1}, ttl_seconds=None, actor=None, tags=["a"])
    store.save(namespace="w", key="k", value={"v": 2}, ttl_seconds=None, actor=None)
    entry = _entries(store.retrieve(namespace="w", key="k", prefix=None, tags=None, limit=None))[0]
    assert entry["tags"] == []


def test_explicit_tags_normalized_deduped_and_stripped(store: WorkspaceMemoryStore):
    saved = store.save(
        namespace="w",
        key="k",
        value={"v": 1},
        ttl_seconds=None,
        actor=None,
        tags=[" git ", "git", "", "pdb"],
    )
    assert saved.tags == ["git", "pdb"]


def test_explicit_tags_reject_non_list_and_path(store: WorkspaceMemoryStore):
    with pytest.raises(ValueError, match="tags deve ser lista de strings"):
        store.save(namespace="w", key="k", value=1, ttl_seconds=None, actor=None, tags="git")
    with pytest.raises(ValueError, match="tags não podem conter path"):
        store.save(namespace="w", key="k", value=1, ttl_seconds=None, actor=None, tags=["../etc"])


# ── updated_at por namespace no list_namespaces ───────────────


def test_list_namespaces_reports_latest_updated_at(store: WorkspaceMemoryStore):
    store.save(namespace="rules", key="a", value=1, ttl_seconds=None, actor=None)
    store.save(namespace="rules", key="b", value=2, ttl_seconds=None, actor=None)
    entry_b = _entries(store.retrieve(namespace="rules", key="b", prefix=None, tags=None, limit=None))[0]

    (listed,) = store.list_namespaces()["namespaces"]
    assert listed["namespace"] == "rules"
    assert listed["keys"] == 2
    assert listed["updated_at"] == entry_b["updated_at"]


def test_list_namespaces_updated_at_tolerates_legacy_file(tmp_path: Path):
    memory_file = tmp_path / "memory.json"
    legacy = {
        "revision": 1,
        "entries": {
            "todo": {
                "sem-updated": {"namespace": "todo", "key": "sem-updated", "value": 1},
                "naive": {
                    "namespace": "todo",
                    "key": "naive",
                    "value": 2,
                    "updated_at": "2026-07-10T10:00:00",
                },
            }
        },
    }
    memory_file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    store = WorkspaceMemoryStore(memory_file)

    (listed,) = store.list_namespaces()["namespaces"]
    assert listed["keys"] == 2
    assert listed["updated_at"] == "2026-07-10T10:00:00+00:00"

import hashlib
from pathlib import Path

from quimera.evidence.models import Evidence
from quimera.evidence.store import EvidenceStore


def test_append_and_query_round_trip(tmp_path):
    store = EvidenceStore(tmp_path, "sessao-1")
    first = Evidence(ts="2026-05-18T20:36:11.000Z", path="/tmp/a.txt", digest="aaa")
    second = Evidence(ts="2026-05-18T20:36:12.000Z", path="/tmp/b.txt", digest="bbb")

    try:
        store.append(first)
        store.append(second)
    finally:
        store.close()

    reader = EvidenceStore(tmp_path, "sessao-1")
    try:
        assert reader.query("sessao-1", "2026-05-18T20:36:12.000Z") == [second]
        assert reader.query("sessao-1", None) == [first, second]
    finally:
        reader.close()


def test_is_valid_returns_false_after_file_changes(tmp_path):
    evidence_file = tmp_path / "artifact.txt"
    evidence_file.write_text("original", encoding="utf-8")
    digest = hashlib.sha1(evidence_file.read_bytes()).hexdigest()
    store = EvidenceStore(tmp_path, "sessao-1")

    try:
        evidence_file.write_text("modificado", encoding="utf-8")
        assert store.is_valid(evidence_file, digest) is False
    finally:
        store.close()


def test_is_valid_returns_true_for_matching_digest(tmp_path):
    evidence_file = tmp_path / "artifact.txt"
    evidence_file.write_text("conteudo", encoding="utf-8")
    digest = hashlib.sha1(evidence_file.read_bytes()).hexdigest()
    store = EvidenceStore(tmp_path, "sessao-1")

    try:
        assert store.is_valid(Path(evidence_file), digest) is True
    finally:
        store.close()

"""Persistência JSONL de evidências por sessão."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from quimera.evidence.models import Evidence


class EvidenceStore:
    def __init__(self, base_dir: Path, session_id: str):
        self.base_dir = Path(base_dir)
        self.session_id = session_id
        self.evidence_dir = self.base_dir / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.evidence_dir / f"{session_id}.jsonl"
        self._handle = self.path.open("a+", encoding="utf-8")

    def append(self, evidence: Evidence) -> None:
        line = json.dumps(evidence.to_dict(), ensure_ascii=False)
        self._handle.write(line)
        self._handle.write("\n")
        self._handle.flush()

    def query(self, session_id: str, since_ts: str | None = None) -> list[Evidence]:
        path = self.evidence_dir / f"{session_id}.jsonl"
        if not path.exists():
            return []

        items: list[Evidence] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                payload = json.loads(raw)
                evidence = Evidence(**payload)
                if since_ts is not None and evidence.ts < since_ts:
                    continue
                items.append(evidence)
        return items

    def is_valid(self, path: Path, digest: str) -> bool:
        file_path = Path(path)
        try:
            actual_digest = hashlib.sha1(file_path.read_bytes()).hexdigest()
        except OSError:
            return False
        return actual_digest == digest

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "EvidenceStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

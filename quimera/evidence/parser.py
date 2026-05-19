"""Parser de evidências baseado em padrões de output de agentes."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from quimera.evidence.models import Evidence


class PatternExtractor(Protocol):
    """Protocolo para extratores de evidências."""

    def extract(self, output: str, agent: str, session_id: str) -> list[Evidence]: ...


@dataclass
class _PatternRegistry:
    """Registro central de extratores de padrões."""

    _extractors: dict[str, PatternExtractor] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, key: str, extractor: PatternExtractor) -> None:
        with self._lock:
            self._extractors[key] = extractor

    def extract_all(self, output: str, agent: str, session_id: str) -> list[Evidence]:
        with self._lock:
            extractors = list(self._extractors.values())
        results: list[Evidence] = []
        for extractor in extractors:
            results.extend(extractor.extract(output, agent, session_id))
        return results

    def default(self) -> None:
        """Registra os extratores padrão."""
        self.register("think", ThinkExtractor())
        self.register("file_read", FileReadExtractor())
        self.register("file_edit", FileEditExtractor())


PatternRegistry = _PatternRegistry()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ThinkExtractor:
    """Captura blocos <think>...</thinking> e produz resumos."""

    _RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL)
    _MAX_SUMMARY = 500

    def extract(self, output: str, agent: str, session_id: str) -> list[Evidence]:
        evidences: list[Evidence] = []
        for match in self._RE.finditer(output):
            raw = match.group(1).strip()
            summary = raw[: self._MAX_SUMMARY]
            evidences.append(
                Evidence(
                    ts=_now_iso(),
                    path="",
                    digest="",
                    type="think_summary",
                    summary=summary,
                    agent=agent,
                    session_id=session_id,
                )
            )
        return evidences


class FileReadExtractor:
    """Captura padrões de leitura de arquivos no output."""

    _PATTERNS = [
        re.compile(r"Read file:\s*(\S+)", re.IGNORECASE),
        re.compile(r"Lendo\s+(\S+)", re.IGNORECASE),
        re.compile(r"Read\s+(\S+)"),
    ]

    def extract(self, output: str, agent: str, session_id: str) -> list[Evidence]:
        evidences: list[Evidence] = []
        seen: set[str] = set()
        for line in output.splitlines():
            for pattern in self._PATTERNS:
                m = pattern.search(line)
                if m:
                    path = m.group(1).strip()
                    if path and path not in seen:
                        seen.add(path)
                        evidences.append(
                            Evidence(
                                ts=_now_iso(),
                                path=path,
                                digest="",
                                type="file_read",
                                agent=agent,
                                session_id=session_id,
                            )
                        )
                    break
        return evidences


class FileEditExtractor:
    """Captura padrões de edição de arquivos no output."""

    _PATTERNS = [
        re.compile(r"\u2713\s*Edit\s+(\S+)"),
        re.compile(r"Edit\s+(\S+)"),
        re.compile(r"Wrote\s+(\S+)"),
    ]

    def extract(self, output: str, agent: str, session_id: str) -> list[Evidence]:
        evidences: list[Evidence] = []
        seen: set[str] = set()
        for line in output.splitlines():
            for pattern in self._PATTERNS:
                m = pattern.search(line)
                if m:
                    path = m.group(1).strip()
                    if path and path not in seen:
                        seen.add(path)
                        evidences.append(
                            Evidence(
                                ts=_now_iso(),
                                path=path,
                                digest="",
                                type="file_edit",
                                agent=agent,
                                session_id=session_id,
                            )
                        )
                    break
        return evidences

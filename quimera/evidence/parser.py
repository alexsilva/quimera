"""Parser de evidências baseado em padrões de output de agentes."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from quimera.evidence.models import Evidence

# ANSI escape sequences (CSI, SGR, etc.)
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\([a-zA-Z]')

# Terminal/markdown artifacts that can leak into captured paths
_PATH_TRAILING_NOISE = re.compile(r'[✓✗✔✘\u2713\u2714\u2717\u2718\[\]`\'",;:]+$')
_PATH_LEADING_NOISE = re.compile(r'^[✓✗✔✘\u2713\u2714\u2717\u2718\[\]`\'",;:]+')

# Characters allowed in Unix-style file paths
_VALID_PATH_CHARS = re.compile(r'^[a-zA-Z0-9_./\-~@+]+$')

# Common file extensions to help validate path-like strings
_HAS_PATH_STRUCTURE = re.compile(r'(?:/|\.[a-zA-Z0-9]+$)')

# Valid file extension: 1-5 lowercase alphabetic chars after the last dot
# (covers py, js, ts, yaml, json, html, css, scss, md, txt, etc.)
_VALID_EXTENSION = re.compile(r'\.[a-z]{1,5}$')


def _sanitize_path(raw: str) -> str | None:
    """Limpa e valida um path capturado do stdout.

    Remove ANSI codes, artefatos de terminal (checkmarks, brackets),
    e rejeita strings que não parecem paths de arquivo válidos.
    """
    if not raw:
        return None

    # Strip ANSI escape sequences
    path = _ANSI_RE.sub('', raw)

    # Strip leading/trailing noise characters
    path = _PATH_LEADING_NOISE.sub('', path)
    path = _PATH_TRAILING_NOISE.sub('', path)

    # Strip whitespace
    path = path.strip()

    if not path:
        return None

    # Must contain only valid path characters
    if not _VALID_PATH_CHARS.match(path):
        return None

    # Must have path structure (directory separator or file extension)
    if not _HAS_PATH_STRUCTURE.search(path):
        return None

    # If path has a dot, validate the extension looks real (catches concatenated noise like .pyRead)
    if '.' in path and not _VALID_EXTENSION.search(path):
        return None

    return path


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
        re.compile(r"Read file:\s*([^\s]+)", re.IGNORECASE),
        re.compile(r"Lendo\s+([^\s]+)", re.IGNORECASE),
        re.compile(r"Read\s+([^\s]+)"),
    ]

    def extract(self, output: str, agent: str, session_id: str) -> list[Evidence]:
        evidences: list[Evidence] = []
        seen: set[str] = set()
        for line in output.splitlines():
            for pattern in self._PATTERNS:
                m = pattern.search(line)
                if m:
                    path = _sanitize_path(m.group(1))
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
        re.compile(r"\u2713\s*Edit\s+([^\s]+)"),
        re.compile(r"Edit\s+([^\s]+)"),
        re.compile(r"Wrote\s+([^\s]+)"),
    ]

    def extract(self, output: str, agent: str, session_id: str) -> list[Evidence]:
        evidences: list[Evidence] = []
        seen: set[str] = set()
        for line in output.splitlines():
            for pattern in self._PATTERNS:
                m = pattern.search(line)
                if m:
                    path = _sanitize_path(m.group(1))
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

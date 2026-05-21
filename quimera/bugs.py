"""Persistência e detecção de bugs operacionais para agentes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import threading


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _severity_rank(value: str) -> int:
    normalized = str(value or "").strip().lower()
    if normalized == "critical":
        return 4
    if normalized == "high":
        return 3
    if normalized == "medium":
        return 2
    return 1


def _coerce_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class BugEvidenceRef:
    kind: str
    path: str = ""
    ts: str = ""
    line: int | None = None
    offset: int | None = None
    event: str = ""
    preview: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "BugEvidenceRef":
        return cls(
            kind=str(payload.get("kind", "") or ""),
            path=str(payload.get("path", "") or ""),
            ts=str(payload.get("ts", "") or ""),
            line=payload.get("line"),
            offset=payload.get("offset"),
            event=str(payload.get("event", "") or ""),
            preview=str(payload.get("preview", "") or ""),
        )


@dataclass(slots=True)
class BugReport:
    id: str
    session_id: str
    category: str
    summary: str
    severity: str = "medium"
    confidence: float = 0.5
    description: str = ""
    status: str = "open"
    fingerprint: str = ""
    evidence_refs: list[BugEvidenceRef] = field(default_factory=list)
    agent: str = ""
    turn_id: str = ""
    first_seen_at: str = field(default_factory=_utc_now)
    last_seen_at: str = field(default_factory=_utc_now)
    count: int = 1

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["evidence_refs"] = [item.to_dict() for item in self.evidence_refs]
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "BugReport":
        refs = payload.get("evidence_refs", []) or []
        evidence_refs = [
            BugEvidenceRef.from_dict(item) for item in refs if isinstance(item, dict)
        ]
        return cls(
            id=str(payload.get("id", "") or ""),
            session_id=str(payload.get("session_id", "") or ""),
            category=str(payload.get("category", "") or ""),
            summary=str(payload.get("summary", "") or ""),
            severity=str(payload.get("severity", "medium") or "medium"),
            confidence=_coerce_float(payload.get("confidence", 0.5), 0.5),
            description=str(payload.get("description", "") or ""),
            status=str(payload.get("status", "open") or "open"),
            fingerprint=str(payload.get("fingerprint", "") or ""),
            evidence_refs=evidence_refs,
            agent=str(payload.get("agent", "") or ""),
            turn_id=str(payload.get("turn_id", "") or ""),
            first_seen_at=str(payload.get("first_seen_at", "") or ""),
            last_seen_at=str(payload.get("last_seen_at", "") or ""),
            count=max(1, _coerce_int(payload.get("count", 1), 1)),
        )


def make_bug_fingerprint(session_id: str, category: str, summary: str) -> str:
    base = f"{session_id}|{category}|{summary.strip().lower()}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


class BugStore:
    """Store append-only JSONL para bugs com deduplicação por fingerprint."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.bugs_dir = self.base_dir / "bugs"
        self.bugs_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.bugs_dir / "bugs.jsonl"
        self._lock = threading.RLock()
        self._closed = False
        self._handle = self.path.open("a+", encoding="utf-8")

    def _iter_records(self) -> list[BugReport]:
        if not self.path.exists():
            return []
        reports: list[BugReport] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                try:
                    report = BugReport.from_dict(payload)
                except (TypeError, ValueError):
                    continue
                if report.id and report.session_id and report.category:
                    reports.append(report)
        return reports

    def _write(self, report: BugReport) -> None:
        if self._closed:
            return
        self._handle.write(json.dumps(report.to_dict(), ensure_ascii=False))
        self._handle.write("\n")
        self._handle.flush()

    def file(self, report: BugReport) -> BugReport:
        with self._lock:
            now = _utc_now()
            latest_by_id: dict[str, BugReport] = {}
            for item in self._iter_records():
                latest_by_id[item.id] = item

            if not report.fingerprint:
                report.fingerprint = make_bug_fingerprint(
                    report.session_id, report.category, report.summary
                )

            existing: BugReport | None = None
            for item in latest_by_id.values():
                if (
                    item.fingerprint == report.fingerprint
                    and item.status == "open"
                    and item.session_id == report.session_id
                ):
                    existing = item
                    break

            if existing is not None:
                merged = BugReport(
                    id=existing.id,
                    session_id=existing.session_id,
                    category=existing.category,
                    summary=report.summary or existing.summary,
                    severity=(
                        report.severity
                        if _severity_rank(report.severity) >= _severity_rank(existing.severity)
                        else existing.severity
                    ),
                    confidence=max(existing.confidence, report.confidence),
                    description=report.description or existing.description,
                    status="open",
                    fingerprint=existing.fingerprint,
                    evidence_refs=report.evidence_refs or existing.evidence_refs,
                    agent=report.agent or existing.agent,
                    turn_id=report.turn_id or existing.turn_id,
                    first_seen_at=existing.first_seen_at or now,
                    last_seen_at=now,
                    count=max(1, existing.count) + 1,
                )
                self._write(merged)
                return merged

            report_id = report.id or f"bug_{now.replace(':', '').replace('-', '').replace('.', '')}"
            created = BugReport(
                id=report_id,
                session_id=report.session_id,
                category=report.category,
                summary=report.summary,
                severity=report.severity,
                confidence=report.confidence,
                description=report.description,
                status=report.status or "open",
                fingerprint=report.fingerprint,
                evidence_refs=report.evidence_refs,
                agent=report.agent,
                turn_id=report.turn_id,
                first_seen_at=report.first_seen_at or now,
                last_seen_at=now,
                count=max(1, report.count),
            )
            self._write(created)
            return created

    def close_bug(self, bug_id: str) -> BugReport | None:
        with self._lock:
            latest: dict[str, BugReport] = {}
            for item in self._iter_records():
                latest[item.id] = item
            target = latest.get(str(bug_id or "").strip())
            if target is None:
                return None
            if target.status == "closed":
                return target
            closed = BugReport(
                id=target.id,
                session_id=target.session_id,
                category=target.category,
                summary=target.summary,
                severity=target.severity,
                confidence=target.confidence,
                description=target.description,
                status="closed",
                fingerprint=target.fingerprint,
                evidence_refs=target.evidence_refs,
                agent=target.agent,
                turn_id=target.turn_id,
                first_seen_at=target.first_seen_at,
                last_seen_at=_utc_now(),
                count=target.count,
            )
            self._write(closed)
            return closed

    def query(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
        category: str | None = None,
        limit: int | None = None,
    ) -> list[BugReport]:
        with self._lock:
            latest: dict[str, BugReport] = {}
            for item in self._iter_records():
                latest[item.id] = item
            results = list(latest.values())
            if session_id:
                results = [r for r in results if r.session_id == session_id]
            if status:
                results = [r for r in results if r.status == status]
            if category:
                results = [r for r in results if r.category == category]
            results.sort(key=lambda item: item.last_seen_at or item.first_seen_at, reverse=True)
            if isinstance(limit, int) and limit > 0:
                return results[:limit]
            return results

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._handle.close()
            self._closed = True

    def __enter__(self) -> "BugStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class RenderBugDetector:
    """Detector determinístico de bugs a partir de logs de render."""

    _PROMPT_COLLISION_PATTERN = re.compile(
        r"\b[A-Za-z][A-Za-z0-9_\-]{0,32}:\s*(?:⚙|←|TOOLS?:)",
        re.IGNORECASE,
    )

    def __init__(self, repeat_threshold: int = 1):
        self.repeat_threshold = max(1, int(repeat_threshold))

    def analyze_session(
        self,
        *,
        session_id: str,
        events_path: Path | None,
        ansi_path: Path | None = None,
        agent: str = "",
    ) -> list[BugReport]:
        reports: list[BugReport] = []
        if events_path is not None:
            events_file = Path(events_path)
        else:
            events_file = None
        if events_file is not None and events_file.is_file():
            reports.extend(self._scan_events_file(session_id=session_id, path=events_file, agent=agent))
        if ansi_path is not None:
            ansi_file = Path(ansi_path)
            if ansi_file.is_file():
                reports.extend(self._scan_ansi_file(session_id=session_id, path=ansi_file, agent=agent))
        return reports

    def _scan_events_file(self, *, session_id: str, path: Path, agent: str) -> list[BugReport]:
        reports: list[BugReport] = []
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                event = str(payload.get("event", "") or "")
                ts = str(payload.get("ts", "") or "")
                if event == "ansi_duplicate_suppressed":
                    repeats = int(payload.get("repeats", 0) or 0)
                    if repeats >= self.repeat_threshold:
                        summary = "Bloco ANSI repetido suprimido"
                        reports.append(
                            self._build_report(
                                session_id=session_id,
                                category="render_repeat_block",
                                summary=summary,
                                severity="medium",
                                confidence=min(0.99, 0.65 + (repeats * 0.05)),
                                agent=agent,
                                evidence=BugEvidenceRef(
                                    kind="render_jsonl",
                                    path=str(path),
                                    line=index,
                                    ts=ts,
                                    event=event,
                                    preview=f"{summary} ({repeats}x)",
                                ),
                            )
                        )
                if event == "print":
                    preview = str(payload.get("preview", "") or "")
                    if self._PROMPT_COLLISION_PATTERN.search(preview):
                        summary = "Saída operacional colada na linha do prompt"
                        reports.append(
                            self._build_report(
                                session_id=session_id,
                                category="prompt_line_collision",
                                summary=summary,
                                severity="high",
                                confidence=0.9,
                                agent=agent,
                                evidence=BugEvidenceRef(
                                    kind="render_jsonl",
                                    path=str(path),
                                    line=index,
                                    ts=ts,
                                    event=event,
                                    preview=preview[:180],
                                ),
                            )
                        )
        return reports

    def _scan_ansi_file(self, *, session_id: str, path: Path, agent: str) -> list[BugReport]:
        payload = path.read_text(encoding="utf-8", errors="replace")
        if "KeyboardInterrupt:" not in payload or "_python_exit" not in payload:
            return []
        marker = "KeyboardInterrupt:"
        offset = payload.find(marker)
        summary = "KeyboardInterrupt durante shutdown de threads"
        report = self._build_report(
            session_id=session_id,
            category="interrupt_shutdown_traceback",
            summary=summary,
            severity="high",
            confidence=0.95,
            agent=agent,
            evidence=BugEvidenceRef(
                kind="render_ansi",
                path=str(path),
                offset=max(0, offset),
                preview=summary,
            ),
        )
        return [report]

    @staticmethod
    def _build_report(
        *,
        session_id: str,
        category: str,
        summary: str,
        severity: str,
        confidence: float,
        evidence: BugEvidenceRef,
        agent: str = "",
    ) -> BugReport:
        fingerprint = make_bug_fingerprint(session_id, category, summary)
        report_id = f"bug_{fingerprint[:12]}"
        return BugReport(
            id=report_id,
            session_id=session_id,
            category=category,
            summary=summary,
            severity=severity,
            confidence=confidence,
            fingerprint=fingerprint,
            evidence_refs=[evidence],
            agent=agent,
        )


def format_bug_context(reports: list[BugReport]) -> str:
    if not reports:
        return ""
    lines = [
        '<bug_context title="Bugs Operacionais Abertos">',
        "Estes bugs foram detectados automaticamente a partir de sinais de runtime e audit de render.",
        "",
    ]
    for report in reports:
        line = f"- [{report.severity}] [{report.category}] {report.summary} (count={report.count})"
        lines.append(line)
        if report.evidence_refs:
            evidence = report.evidence_refs[0]
            location = evidence.path
            if evidence.line is not None:
                location = f"{location}:{evidence.line}"
            lines.append(f"  evidence: {evidence.kind} | {location}")
    lines.append("</bug_context>")
    return "\n".join(lines)

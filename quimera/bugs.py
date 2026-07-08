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
    """Referência a uma evidência de bug com tipo, localização e pré-visualização."""
    kind: str
    path: str = ""
    ts: str = ""
    line: int | None = None
    offset: int | None = None
    event: str = ""
    preview: str = ""

    def to_dict(self) -> dict:
        """Converte a referência de evidência para dicionário."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "BugEvidenceRef":
        """Constrói uma referência de evidência a partir de um dicionário."""
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
    """Relatório estruturado de um bug com fingerprint, severidade e evidências."""
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
        """Converte o relatório para dicionário com evidências serializadas."""
        payload = asdict(self)
        payload["evidence_refs"] = [item.to_dict() for item in self.evidence_refs]
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "BugReport":
        """Constrói um relatório de bug a partir de um dicionário."""
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
    """Gera um fingerprint SHA-1 para deduplicação de bugs."""
    base = f"{session_id}|{category}|{summary.strip().lower()}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


class BugStore:
    """Store append-only JSONL para bugs com deduplicação por fingerprint."""

    def __init__(self, base_dir: Path):
        if not isinstance(base_dir, (str, Path)):
            raise TypeError(f"BugStore requires a str or Path, got {type(base_dir).__name__}")
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
        """Registra ou mescla um relatório de bug no armazenamento persistente."""
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
        """Marca um bug como fechado no armazenamento."""
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
        """Consulta bugs por sessão, status e categoria, ordenados do mais recente."""
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
        """Fecha o arquivo de armazenamento de bugs."""
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

    def __init__(
        self,
        repeat_threshold: int = 1,
        gap_threshold_seconds: float = 30.0,
        rapid_window_seconds: float = 2.0,
        rapid_count_threshold: int = 5,
    ):
        """Configura os limiares de detecção de bugs de render."""
        self.repeat_threshold = max(1, int(repeat_threshold))
        self.gap_threshold_seconds = float(gap_threshold_seconds)
        self.rapid_window_seconds = float(rapid_window_seconds)
        self.rapid_count_threshold = max(1, int(rapid_count_threshold))

    def analyze_session(
        self,
        *,
        session_id: str,
        events_path: Path | None,
        ansi_path: Path | None = None,
        agent: str = "",
    ) -> list[BugReport]:
        """Analisa logs de render e ANSI em busca de anomalias operacionais."""
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

    def _parse_ts(self, ts: str) -> datetime | None:
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return None

    def _scan_events_file(self, *, session_id: str, path: Path, agent: str) -> list[BugReport]:
        reports: list[BugReport] = []
        events_with_ts: list[tuple[int, str, str, str, dict]] = []
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
                preview = str(payload.get("preview", "") or "")
                events_with_ts.append((index, event, ts, preview, payload))

        for index, event, ts, preview, payload in events_with_ts:
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

        self._detect_long_gaps(session_id, path, events_with_ts, agent, reports)
        self._detect_rapid_burst(session_id, path, events_with_ts, agent, reports)
        return reports

    def _detect_long_gaps(
        self,
        session_id: str,
        path: Path,
        events: list[tuple[int, str, str, str, dict]],
        agent: str,
        reports: list[BugReport],
    ) -> None:
        parsed: list[tuple[int, str, str, datetime]] = []
        for index, event, ts, preview, payload in events:
            dt = self._parse_ts(ts)
            if dt is not None:
                parsed.append((index, event, ts, dt))
        if len(parsed) < 2:
            return
        for i in range(1, len(parsed)):
            prev_idx, prev_evt, prev_ts, prev_dt = parsed[i - 1]
            curr_idx, curr_evt, curr_ts, curr_dt = parsed[i]
            gap = (curr_dt - prev_dt).total_seconds()
            if gap >= self.gap_threshold_seconds:
                summary = f"Gap de {gap:.0f}s entre eventos de render"
                reports.append(
                    self._build_report(
                        session_id=session_id,
                        category="render_long_gap",
                        summary=summary,
                        severity="medium",
                        confidence=min(0.95, 0.5 + (gap / 120)),
                        agent=agent,
                        evidence=BugEvidenceRef(
                            kind="render_jsonl",
                            path=str(path),
                            line=curr_idx,
                            ts=curr_ts,
                            event=curr_evt,
                            preview=f"gap={gap:.1f}s after {prev_evt}",
                        ),
                    )
                )

    def _detect_rapid_burst(
        self,
        session_id: str,
        path: Path,
        events: list[tuple[int, str, str, str, dict]],
        agent: str,
        reports: list[BugReport],
    ) -> None:
        parsed: list[tuple[int, str, str, datetime]] = []
        for index, event, ts, preview, payload in events:
            dt = self._parse_ts(ts)
            if dt is not None:
                parsed.append((index, event, ts, dt))
        if len(parsed) < self.rapid_count_threshold:
            return
        window = self.rapid_window_seconds
        threshold = self.rapid_count_threshold
        i = 0
        reported_lines: set[int] = set()
        while i < len(parsed):
            j = i + 1
            while j < len(parsed):
                dt_i = parsed[i][3]
                dt_j = parsed[j][3]
                if (dt_j - dt_i).total_seconds() > window:
                    break
                j += 1
            count = j - i
            if count >= threshold and parsed[i][1] == "print":
                line_idx = parsed[i][0]
                if line_idx not in reported_lines:
                    reported_lines.add(line_idx)
                    summary = f"Rajada de {count} prints em {window:.1f}s"
                    reports.append(
                        self._build_report(
                            session_id=session_id,
                            category="render_rapid_burst",
                            summary=summary,
                            severity="low",
                            confidence=min(0.9, 0.4 + (count * 0.05)),
                            agent=agent,
                            evidence=BugEvidenceRef(
                                kind="render_jsonl",
                                path=str(path),
                                line=line_idx,
                                ts=parsed[i][2],
                                event="print",
                                preview=f"{count} events in {window:.1f}s window",
                            ),
                        )
                    )
            i = max(i + 1, j - threshold + 1)

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


class AgentRuntimeBugDetector:
    """Detector de bugs orientado a métricas de agentes e pressão de prompt."""

    def __init__(
        self,
        *,
        min_failures: int = 2,
        min_tool_calls: int = 3,
        latency_threshold_seconds: float = 45.0,
        prompt_total_chars_threshold: int = 60000,
        prompt_threshold_hits: int = 2,
    ):
        """Configura os limiares de detecção de bugs de runtime de agente."""
        self.min_failures = max(1, int(min_failures))
        self.min_tool_calls = max(1, int(min_tool_calls))
        self.latency_threshold_seconds = max(1.0, float(latency_threshold_seconds))
        self.prompt_total_chars_threshold = max(1000, int(prompt_total_chars_threshold))
        self.prompt_threshold_hits = max(1, int(prompt_threshold_hits))

    def analyze(
        self,
        *,
        session_id: str,
        agent_metrics: dict | None,
        prompt_metrics_path: Path | None = None,
    ) -> list[BugReport]:
        """Analisa métricas de agente e prompt em busca de padrões problemáticos."""
        reports: list[BugReport] = []
        reports.extend(
            self._scan_agent_metrics(
                session_id=session_id,
                agent_metrics=agent_metrics or {},
            )
        )
        if prompt_metrics_path is not None:
            path = Path(prompt_metrics_path)
            if path.is_file():
                reports.extend(self._scan_prompt_metrics_file(session_id=session_id, path=path))
        return reports

    def _scan_agent_metrics(self, *, session_id: str, agent_metrics: dict) -> list[BugReport]:
        reports: list[BugReport] = []
        for raw_agent, raw_metrics in agent_metrics.items():
            if not isinstance(raw_metrics, dict):
                continue
            agent = str(raw_agent or "").strip() or "unknown"
            succeeded = max(0, _coerce_int(raw_metrics.get("succeeded", 0), 0))
            failed = max(0, _coerce_int(raw_metrics.get("failed", 0), 0))
            attempts = succeeded + failed
            latency_total = max(0.0, _coerce_float(raw_metrics.get("latency", 0.0), 0.0))
            avg_latency = (latency_total / attempts) if attempts > 0 else 0.0
            tool_calls_total = max(0, _coerce_int(raw_metrics.get("tool_calls_total", 0), 0))
            tool_calls_failed = max(0, _coerce_int(raw_metrics.get("tool_calls_failed", 0), 0))
            invalid_tool_calls = max(0, _coerce_int(raw_metrics.get("invalid_tool_calls", 0), 0))
            tool_loop_abortions = max(0, _coerce_int(raw_metrics.get("tool_loop_abortions", 0), 0))

            if failed >= self.min_failures and attempts > 0:
                failure_rate = failed / attempts
                if failure_rate >= 0.5:
                    summary = f"Agente {agent} com taxa de falha elevada"
                    description = (
                        f"failed={failed} succeeded={succeeded} "
                        f"failure_rate={failure_rate:.2f}"
                    )
                    reports.append(
                        self._build_report(
                            session_id=session_id,
                            category="agent_failure_rate_high",
                            summary=summary,
                            severity="high" if failure_rate >= 0.8 else "medium",
                            confidence=min(0.99, 0.7 + (failure_rate * 0.2)),
                            description=description,
                            evidence=BugEvidenceRef(
                                kind="agent_metrics",
                                path="session_state.agent_metrics",
                                preview=description,
                            ),
                            agent=agent,
                        )
                    )

            if attempts > 0 and avg_latency >= self.latency_threshold_seconds:
                summary = f"Agente {agent} com latência média elevada"
                description = (
                    f"avg_latency={avg_latency:.2f}s threshold={self.latency_threshold_seconds:.2f}s "
                    f"attempts={attempts}"
                )
                reports.append(
                    self._build_report(
                        session_id=session_id,
                        category="agent_latency_high",
                        summary=summary,
                        severity="medium",
                        confidence=0.75,
                        description=description,
                        evidence=BugEvidenceRef(
                            kind="agent_metrics",
                            path="session_state.agent_metrics",
                            preview=description,
                        ),
                        agent=agent,
                    )
                )

            if tool_calls_total >= self.min_tool_calls:
                failure_ratio = tool_calls_failed / tool_calls_total if tool_calls_total else 0.0
                if tool_calls_failed >= 2 and failure_ratio >= 0.5:
                    summary = f"Agente {agent} com erro recorrente em ferramentas"
                    description = (
                        f"tool_calls_total={tool_calls_total} "
                        f"tool_calls_failed={tool_calls_failed} ratio={failure_ratio:.2f}"
                    )
                    reports.append(
                        self._build_report(
                            session_id=session_id,
                            category="agent_tool_error_burst",
                            summary=summary,
                            severity="medium",
                            confidence=min(0.98, 0.7 + (failure_ratio * 0.2)),
                            description=description,
                            evidence=BugEvidenceRef(
                                kind="agent_metrics",
                                path="session_state.agent_metrics",
                                preview=description,
                            ),
                            agent=agent,
                        )
                    )

            if invalid_tool_calls >= 2:
                summary = f"Agente {agent} com chamadas de ferramenta inválidas"
                description = f"invalid_tool_calls={invalid_tool_calls}"
                reports.append(
                    self._build_report(
                        session_id=session_id,
                        category="agent_invalid_tool_calls",
                        summary=summary,
                        severity="low",
                        confidence=0.8,
                        description=description,
                        evidence=BugEvidenceRef(
                            kind="agent_metrics",
                            path="session_state.agent_metrics",
                            preview=description,
                        ),
                        agent=agent,
                    )
                )

            if tool_loop_abortions >= 2:
                summary = f"Agente {agent} com abortos de tool loop"
                description = f"tool_loop_abortions={tool_loop_abortions}"
                reports.append(
                    self._build_report(
                        session_id=session_id,
                        category="agent_tool_loop_abort",
                        summary=summary,
                        severity="medium",
                        confidence=0.85,
                        description=description,
                        evidence=BugEvidenceRef(
                            kind="agent_metrics",
                            path="session_state.agent_metrics",
                            preview=description,
                        ),
                        agent=agent,
                    )
                )
        return reports

    def _scan_prompt_metrics_file(self, *, session_id: str, path: Path) -> list[BugReport]:
        threshold_hits: dict[str, dict] = {}
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
                agent = str(payload.get("agent", "") or "").strip() or "unknown"
                total_chars = _coerce_int(payload.get("total_chars", 0), 0)
                if total_chars < self.prompt_total_chars_threshold:
                    continue
                entry = threshold_hits.setdefault(
                    agent,
                    {"count": 0, "max_chars": 0, "line": index},
                )
                entry["count"] += 1
                entry["max_chars"] = max(entry["max_chars"], total_chars)
                if entry["line"] <= 0:
                    entry["line"] = index

        reports: list[BugReport] = []
        for agent, info in threshold_hits.items():
            hit_count = _coerce_int(info.get("count", 0), 0)
            if hit_count < self.prompt_threshold_hits:
                continue
            max_chars = _coerce_int(info.get("max_chars", 0), 0)
            first_line = _coerce_int(info.get("line", 0), 0) or None
            summary = f"Prompt acima do orçamento recorrente para {agent}"
            description = (
                f"hits={hit_count} threshold={self.prompt_total_chars_threshold} "
                f"max_total_chars={max_chars}"
            )
            reports.append(
                self._build_report(
                    session_id=session_id,
                    category="agent_prompt_budget_pressure",
                    summary=summary,
                    severity="medium",
                    confidence=0.82,
                    description=description,
                    evidence=BugEvidenceRef(
                        kind="metrics_jsonl",
                        path=str(path),
                        line=first_line,
                        preview=description,
                    ),
                    agent=agent,
                )
            )
        return reports

    @staticmethod
    def _build_report(
        *,
        session_id: str,
        category: str,
        summary: str,
        severity: str,
        confidence: float,
        description: str,
        evidence: BugEvidenceRef,
        agent: str = "",
    ) -> BugReport:
        fingerprint = make_bug_fingerprint(session_id, category, summary)
        return BugReport(
            id=f"bug_{fingerprint[:12]}",
            session_id=session_id,
            category=category,
            summary=summary,
            severity=severity,
            confidence=confidence,
            description=description,
            fingerprint=fingerprint,
            evidence_refs=[evidence],
            agent=agent,
        )


_TIMELESS_CATEGORIES = frozenset({
    "slot_leak_suspect",
    "interrupt_shutdown_traceback",
    "agent_prompt_budget_pressure",
})


class BugCorrelator:
    """Correla relatórios de diferentes detectores na mesma janela temporal.

    Agrupa anomalias de render com falhas de agente que ocorrem próximas no
    tempo e produz bugs compostos de severidade mais alta, consolidando
    evidências de ambas as fontes.
    """

    def __init__(self, window_seconds: float = 60.0):
        """Configura a janela temporal para correlação de bugs."""
        self.window_seconds = max(5.0, float(window_seconds))

    def correlate(
        self,
        reports: list[BugReport],
        *,
        session_id: str,
    ) -> list[BugReport]:
        """Correlaciona relatórios de diferentes detectores em uma janela temporal."""
        if len(reports) < 2:
            return []

        tagged: list[tuple[datetime | None, BugReport]] = []
        for r in reports:
            tagged.append((self._pick_dt(r), r))

        valid: list[tuple[datetime, BugReport]] = []
        for dt, r in tagged:
            if dt is not None and r.category not in _TIMELESS_CATEGORIES:
                valid.append((dt, r))

        if len(valid) < 2:
            return []

        valid.sort(key=lambda x: x[0])
        clusters = self._cluster(valid)
        return self._build_correlation_bugs(clusters, session_id)

    @staticmethod
    def _pick_dt(report: BugReport) -> datetime | None:
        for ref in report.evidence_refs:
            if ref.ts:
                try:
                    return datetime.fromisoformat(ref.ts)
                except (ValueError, TypeError):
                    continue
        try:
            return datetime.fromisoformat(report.last_seen_at)
        except (ValueError, TypeError):
            return None

    def _cluster(
        self,
        valid: list[tuple[datetime, BugReport]],
    ) -> list[list[BugReport]]:
        clusters: list[list[BugReport]] = []
        current: list[tuple[datetime, BugReport]] = []
        for dt, r in valid:
            if not current:
                current.append((dt, r))
            elif (dt - current[0][0]).total_seconds() <= self.window_seconds:
                current.append((dt, r))
            else:
                clusters.append([item[1] for item in current])
                current = [(dt, r)]
        if current:
            clusters.append([item[1] for item in current])
        return clusters

    def _build_correlation_bugs(
        self,
        clusters: list[list[BugReport]],
        session_id: str,
    ) -> list[BugReport]:
        results: list[BugReport] = []
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            cats = {r.category for r in cluster}
            render_anomalies = {c for c in cats if c.startswith("render_")}
            agent_failures = {c for c in cats if c.startswith("agent_")}
            if not render_anomalies or not agent_failures:
                continue

            seen_ids: set[int] = set()
            evidence: list[BugEvidenceRef] = []
            for r in cluster:
                for ref in r.evidence_refs:
                    rid = id(ref)
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        evidence.append(ref)

            sorted_cats = sorted(cats)
            summary = (
                f"Correlação: anomalia de render e falha de agente "
                f"na janela de {self.window_seconds:.0f}s"
            )
            fingerprint = make_bug_fingerprint(
                session_id, "render_agent_correlation", "|".join(sorted_cats)
            )
            corr = BugReport(
                id=f"corr_{fingerprint[:12]}",
                session_id=session_id,
                category="render_agent_correlation",
                summary=summary,
                severity="high",
                confidence=min(0.95, 0.5 + len(cats) * 0.08),
                description=f"Categorias correlacionadas: {', '.join(sorted_cats)}",
                fingerprint=fingerprint,
                evidence_refs=evidence[:8],
                agent=",".join(sorted({r.agent for r in cluster if r.agent})),
                first_seen_at=cluster[0].first_seen_at,
                last_seen_at=cluster[-1].last_seen_at,
                count=sum(r.count for r in cluster),
            )
            results.append(corr)
        return results


def format_bug_context(reports: list[BugReport]) -> str:
    """Formata uma lista de bugs como contexto textual legível para o prompt."""
    if not reports:
        return ""
    lines = [
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
    return "\n".join(lines)

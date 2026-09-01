"""Rastreamento thread-safe do caminho de um prompt pelo chat."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Callable


logger = logging.getLogger(__name__)

TERMINAL_SUBMISSION_STATUSES = frozenset({"completed", "failed", "cancelled"})
WATCHED_SUBMISSION_STATUSES = frozenset({"accepted", "queued", "starting"})


class SubmittedInput(str):
    """String compatível com o input legado, acrescida de correlação."""

    submission_id: str

    def __new__(cls, value: str, submission_id: str):
        instance = super().__new__(cls, value)
        instance.submission_id = str(submission_id or "")
        return instance


def new_submission_id() -> str:
    """Gera o identificador canônico de uma nova submissão de chat."""
    return f"submission:{uuid.uuid4().hex}"


def submission_id_of(value: object) -> str:
    """Extrai o identificador sem alterar o contrato textual do input."""
    return str(getattr(value, "submission_id", "") or "")


@dataclass(frozen=True)
class SubmissionRecord:
    """Snapshot imutável de uma submissão."""

    submission_id: str
    status: str
    created_at: float
    updated_at: float
    revision: int = 0
    message: str = ""
    queue_position: int | None = None
    agent: str = ""

    def as_payload(self, *, now: float | None = None) -> dict[str, object]:
        """Serializa o snapshot para a camada de apresentação."""
        current = self.updated_at if now is None else now
        return {
            "submission_id": self.submission_id,
            "status": self.status,
            "revision": self.revision,
            "message": self.message,
            "queue_position": self.queue_position,
            "agent": self.agent,
            "elapsed_seconds": max(0.0, current - self.created_at),
        }


class SubmissionTracker:
    """Mantém estados, watchdogs e emissão desacoplada de UI."""

    def __init__(
        self,
        emit: Callable[[dict[str, object]], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        watchdog_seconds: float = 5.0,
        timer_factory: Callable[..., object] = threading.Timer,
        max_records: int = 200,
    ) -> None:
        self._emit = emit
        self._clock = clock
        self._watchdog_seconds = max(0.0, float(watchdog_seconds))
        self._timer_factory = timer_factory
        self._max_records = max(1, int(max_records))
        self._records: OrderedDict[str, SubmissionRecord] = OrderedDict()
        self._watchdogs: dict[str, object] = {}
        self._lock = threading.RLock()

    def start(self, *, emit: bool = True, watch: bool = True) -> SubmissionRecord:
        """Cria uma submissão aceita e, opcionalmente, arma seu watchdog."""
        now = self._clock()
        record = SubmissionRecord(
            submission_id=new_submission_id(),
            status="accepted",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._records[record.submission_id] = record
            self._trim_locked()
            if watch:
                self._arm_watchdog_locked(record.submission_id)
        logger.info(
            "chat_submission_transition submission_id=%s status=accepted",
            record.submission_id,
        )
        if emit:
            self._emit_record(record)
        return record

    def get(self, submission_id: str) -> SubmissionRecord | None:
        """Retorna o último snapshot conhecido."""
        with self._lock:
            return self._records.get(str(submission_id or ""))

    def active_ids(self) -> list[str]:
        """Lista submissões ainda não terminais."""
        with self._lock:
            return [
                key
                for key, record in self._records.items()
                if record.status not in TERMINAL_SUBMISSION_STATUSES
            ]

    def transition(
        self,
        submission_id: str,
        status: str,
        *,
        message: str = "",
        queue_position: int | None = None,
        agent: str = "",
        expected_statuses: frozenset[str] | None = None,
    ) -> SubmissionRecord | None:
        """Aplica transição, ignorando eventos tardios após terminalidade."""
        key = str(submission_id or "")
        normalized = str(status or "").strip().lower()
        if not key or not normalized:
            return None
        with self._lock:
            current = self._records.get(key)
            if current is None or current.status in TERMINAL_SUBMISSION_STATUSES:
                return current
            if expected_statuses is not None and current.status not in expected_statuses:
                return current
            updated = replace(
                current,
                status=normalized,
                updated_at=self._clock(),
                revision=current.revision + 1,
                message=str(message or ""),
                queue_position=queue_position,
                agent=str(agent or current.agent),
            )
            self._records[key] = updated
            if normalized not in WATCHED_SUBMISSION_STATUSES:
                self._cancel_watchdog_locked(key)
        logger.info(
            "chat_submission_transition submission_id=%s status=%s queue_position=%s agent=%s",
            key,
            normalized,
            queue_position,
            updated.agent,
        )
        self._emit_record(updated)
        return updated

    def close(self) -> None:
        """Cancela timers sem descartar os snapshots."""
        with self._lock:
            for key in list(self._watchdogs):
                self._cancel_watchdog_locked(key)

    def _arm_watchdog_locked(self, submission_id: str) -> None:
        if self._watchdog_seconds <= 0:
            return
        timer = self._timer_factory(
            self._watchdog_seconds,
            self._watchdog_expired,
            args=(submission_id,),
        )
        if hasattr(timer, "daemon"):
            timer.daemon = True
        self._watchdogs[submission_id] = timer
        timer.start()

    def _watchdog_expired(self, submission_id: str) -> None:
        with self._lock:
            self._watchdogs.pop(submission_id, None)
            record = self._records.get(submission_id)
            if record is None or record.status not in WATCHED_SUBMISSION_STATUSES:
                return
        seconds = int(self._watchdog_seconds)
        self.transition(
            submission_id,
            "waiting",
            message=f"Aguardando início há mais de {seconds}s",
            expected_statuses=WATCHED_SUBMISSION_STATUSES,
        )

    def _cancel_watchdog_locked(self, submission_id: str) -> None:
        timer = self._watchdogs.pop(submission_id, None)
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            cancel()

    def _trim_locked(self) -> None:
        while len(self._records) > self._max_records:
            oldest_terminal_id = next(
                (
                    key
                    for key, record in self._records.items()
                    if record.status in TERMINAL_SUBMISSION_STATUSES
                ),
                None,
            )
            if oldest_terminal_id is None:
                return
            self._records.pop(oldest_terminal_id, None)
            self._cancel_watchdog_locked(oldest_terminal_id)

    def _emit_record(self, record: SubmissionRecord) -> None:
        try:
            self._emit(record.as_payload(now=self._clock()))
        except Exception:
            logger.exception(
                "falha ao emitir estado da submissão submission_id=%s",
                record.submission_id,
            )

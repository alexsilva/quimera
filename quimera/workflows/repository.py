"""Persistência dos workflows: única camada com SQL de workflows/versões/vínculos/tentativas.

Reutiliza ``TaskRepository`` (mesmo SQLite) para criar tasks, dependências e
transições em lote dentro da mesma transação, de modo que a publicação de um
plano seja atômica: nenhum worker enxerga tasks de um plano parcialmente criado.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from ..domain.task_states import TaskStatus, TaskType
from ..tasks.repository import TaskRepository
from .models import (
    AttemptRecord,
    TaskView,
    WorkflowError,
    WorkflowLimits,
    WorkflowNotFound,
    WorkflowPlan,
    WorkflowRecord,
    WorkflowSnapshot,
    WorkflowStatus,
    WorkflowTaskLink,
    WorkflowTaskSpec,
    can_transition_workflow,
)
from .plan import plan_from_dict, plan_hash, plan_to_dict, resolve_assignees, spec_from_dict, spec_to_dict

_ACTIVE_STATUSES = (
    WorkflowStatus.READY,
    WorkflowStatus.RUNNING,
    WorkflowStatus.PAUSED,
    WorkflowStatus.BLOCKED,
)
_IN_FLIGHT_TASK_STATUSES = (
    TaskStatus.IN_PROGRESS,
    TaskStatus.PENDING_REVIEW,
    TaskStatus.REVIEWING,
)
# Vereditos de revisão: só valem sobre uma tentativa já submetida pelo executor.
_REVIEW_VERDICTS = frozenset({"accepted", "rejected"})


class PlanVersionConflict(WorkflowError):
    """O replanejamento partiu de uma versão que não é mais a vigente."""


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Resultado de uma publicação idempotente."""

    workflow: WorkflowRecord
    created: bool


@dataclass(frozen=True, slots=True)
class ReplanResult:
    """Efeitos persistidos de uma nova versão do plano."""

    workflow: WorkflowRecord
    applied: bool
    kept_task_ids: tuple[int, ...] = ()
    created_task_ids: tuple[int, ...] = ()
    retired_task_ids: tuple[int, ...] = ()
    # Tasks que estavam em execução/revisão e cujas execuções devem ser interrompidas.
    to_interrupt: tuple[int, ...] = ()


class WorkflowRepository:
    """Armazena e consulta o estado durável dos workflows."""

    def __init__(self, tasks: TaskRepository, limits: WorkflowLimits | None = None) -> None:
        self._tasks = tasks
        self.limits = limits or WorkflowLimits()

    # ── Publicação ─────────────────────────────────────────────────────

    def publish(
        self,
        plan: WorkflowPlan,
        *,
        job_id: int,
        publication_key: str,
        active_agents: Iterable[str],
    ) -> PublishResult:
        """Publica o plano de forma atômica e idempotente por ``publication_key``."""
        resolved = resolve_assignees(plan, active_agents)
        with self._tasks.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM workflows WHERE publication_key = ?", (publication_key,)
            ).fetchone()
            if row:
                return PublishResult(self._load(conn, int(row[0])), created=False)
            open_count = conn.execute(
                "SELECT COUNT(*) FROM workflows WHERE job_id = ? AND status IN (?, ?, ?, ?)",
                (job_id, *[status.value for status in _ACTIVE_STATUSES]),
            ).fetchone()[0]
            if open_count >= self.limits.max_open_workflows:
                raise WorkflowError(
                    f"limite de {self.limits.max_open_workflows} workflows abertos atingido"
                )
            now = self._tasks.timestamp()
            cur = conn.execute(
                "INSERT INTO workflows(job_id, objective, coordinator, status, plan_version, "
                "global_criteria, constraints, publication_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    plan.objective,
                    plan.coordinator,
                    WorkflowStatus.READY.value,
                    json.dumps(list(plan.global_criteria), ensure_ascii=False),
                    json.dumps(list(plan.constraints), ensure_ascii=False),
                    publication_key,
                    now,
                    now,
                ),
            )
            if cur.lastrowid is None:
                raise RuntimeError("INSERT em workflows não devolveu rowid")
            workflow_id = int(cur.lastrowid)
            self._insert_plan_version(conn, workflow_id, 1, plan)
            self._create_tasks(conn, workflow_id, job_id, 1, resolved.tasks, existing={})
            return PublishResult(self._load(conn, workflow_id), created=True)

    def _create_tasks(
        self,
        conn: sqlite3.Connection,
        workflow_id: int,
        job_id: int,
        version: int,
        specs: Iterable[WorkflowTaskSpec],
        *,
        existing: dict[str, int],
    ) -> dict[str, int]:
        """Cria as tasks dos ``specs`` e liga dependências (novas e já existentes)."""
        created: dict[str, int] = {}
        new_specs = list(specs)
        for spec in new_specs:
            item = spec.item
            try:
                task_type = TaskType(item.task_type)
            except ValueError:
                task_type = TaskType.GENERAL
            task_id = self._tasks.insert_task(
                conn,
                job_id,
                item.title,
                task_type=task_type,
                assigned_to=item.assigned_to,
                origin="workflow",
                status=TaskStatus.PENDING,
                priority=item.priority,
                created_by=f"workflow:{workflow_id}",
                requested_by="workflow",
                body=json.dumps(
                    {
                        "workflow_id": workflow_id,
                        "plan_version": version,
                        "work_item": spec_to_dict(spec),
                    },
                    ensure_ascii=False,
                ),
                source_context=f"workflow:{workflow_id}:{item.id}:v{version}",
            )
            created[item.id] = task_id
            conn.execute(
                "INSERT INTO workflow_tasks(workflow_id, item_id, task_id, plan_version, "
                "writes, evidence_expected) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    workflow_id,
                    item.id,
                    task_id,
                    version,
                    1 if spec.writes else 0,
                    json.dumps(list(spec.evidence_expected), ensure_ascii=False),
                ),
            )
        by_item = {**existing, **created}
        for spec in new_specs:
            for dependency in spec.item.dependencies:
                self._tasks.insert_task_dependency(
                    conn, created[spec.id], by_item[dependency]
                )
        return created

    @staticmethod
    def _insert_plan_version(
        conn: sqlite3.Connection, workflow_id: int, version: int, plan: WorkflowPlan
    ) -> None:
        conn.execute(
            "INSERT INTO workflow_plans(workflow_id, version, plan_json, plan_hash, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (
                workflow_id,
                version,
                json.dumps(plan_to_dict(plan), ensure_ascii=False),
                plan_hash(plan),
            ),
        )

    # ── Replanejamento ─────────────────────────────────────────────────

    def replan(
        self,
        workflow_id: int,
        plan: WorkflowPlan,
        *,
        base_version: int,
        active_agents: Iterable[str],
        keep_decider: Callable[[dict[str, tuple[WorkflowTaskSpec, str]], WorkflowPlan], set[str]],
    ) -> ReplanResult:
        """Aplica uma nova versão preservando o histórico e as tasks ainda válidas.

        ``keep_decider`` recebe ``{item_id: (spec vigente, status da task)}`` lido
        dentro da transação e devolve os itens a preservar; tudo o mais é
        substituído (nova task) ou retirado (cancelada e marcada como superada).
        """
        resolved = resolve_assignees(plan, active_agents)
        with self._tasks.transaction() as conn:
            workflow = self._load(conn, workflow_id)
            current_hash = conn.execute(
                "SELECT plan_hash FROM workflow_plans WHERE workflow_id = ? AND version = ?",
                (workflow_id, workflow.plan_version),
            ).fetchone()[0]
            if plan_hash(plan) == current_hash:
                return ReplanResult(workflow, applied=False)
            if workflow.plan_version != base_version:
                raise PlanVersionConflict(
                    f"plano vigente e a versao {workflow.plan_version}, nao {base_version}"
                )
            if workflow.status not in (
                WorkflowStatus.RUNNING,
                WorkflowStatus.PAUSED,
                WorkflowStatus.BLOCKED,
                WorkflowStatus.COMPLETED,
            ):
                raise WorkflowError(
                    f"workflow em '{workflow.status.value}' nao admite replanejamento"
                )
            if workflow.replans >= self.limits.max_replans:
                raise WorkflowError(
                    f"limite de {self.limits.max_replans} replanejamentos atingido"
                )
            new_version = workflow.plan_version + 1
            links = self._current_links(conn, workflow_id)
            old_specs = self._plan_specs(conn, workflow_id, workflow.plan_version)
            statuses = self._task_statuses(conn, [link.task_id for link in links])
            current = {
                link.item_id: (old_specs[link.item_id], statuses[link.task_id])
                for link in links
                if link.item_id in old_specs
            }
            keep = keep_decider(current, resolved)
            kept = [link for link in links if link.item_id in keep]
            retired = [link for link in links if link.item_id not in keep]
            retired_ids = [link.task_id for link in retired]
            in_flight = [
                link.task_id
                for link in retired
                if statuses[link.task_id] in {s.value for s in _IN_FLIGHT_TASK_STATUSES}
            ]
            reason = f"superada pelo plano v{new_version}"
            self._close_open_attempts(
                conn,
                self._tasks.transition_tasks(conn, retired_ids, TaskStatus.CANCELLED, reason=reason),
                reason,
            )
            conn.execute(
                f"UPDATE workflow_tasks SET superseded_in = ? WHERE task_id IN "
                f"({', '.join('?' for _ in retired_ids) or 'NULL'})",
                (new_version, *retired_ids),
            )
            existing = {link.item_id: link.task_id for link in kept}
            new_specs = [spec for spec in resolved.tasks if spec.id not in keep]
            created = self._create_tasks(
                conn, workflow_id, workflow.job_id, new_version, new_specs, existing=existing
            )
            self._insert_plan_version(conn, workflow_id, new_version, plan)
            target = (
                workflow.status
                if workflow.status == WorkflowStatus.PAUSED
                else WorkflowStatus.RUNNING
            )
            conn.execute(
                "UPDATE workflows SET plan_version = ?, replans = replans + 1, status = ?, "
                "status_reason = ?, attention = NULL, result = NULL, "
                "global_criteria = ?, constraints = ?, updated_at = ? WHERE id = ?",
                (
                    new_version,
                    target.value,
                    f"replanejado para v{new_version}",
                    json.dumps(list(plan.global_criteria), ensure_ascii=False),
                    json.dumps(list(plan.constraints), ensure_ascii=False),
                    self._tasks.timestamp(),
                    workflow_id,
                ),
            )
            return ReplanResult(
                self._load(conn, workflow_id),
                applied=True,
                kept_task_ids=tuple(link.task_id for link in kept),
                created_task_ids=tuple(created.values()),
                retired_task_ids=tuple(retired_ids),
                to_interrupt=tuple(in_flight),
            )

    # ── Estado do workflow ─────────────────────────────────────────────

    def set_status(
        self,
        workflow_id: int,
        to_status: WorkflowStatus,
        *,
        reason: str | None = None,
        expected: Iterable[WorkflowStatus] | None = None,
        expected_version: int | None = None,
        cancel_tasks: bool = False,
        attention: str | None = None,
        result: str | None = None,
    ) -> tuple[WorkflowRecord, tuple[int, ...]] | None:
        """Transição validada e atômica; ``None`` quando o estado atual não a admite.

        ``expected`` restringe o estado de origem e ``expected_version`` a versão
        do plano sobre a qual a decisão foi tomada (compare-and-set). Com
        ``cancel_tasks`` as tasks vigentes ainda não terminais são canceladas na
        mesma transação e seus ids em voo são devolvidos para interrupção.
        """
        with self._tasks.transaction() as conn:
            workflow = self._load(conn, workflow_id)
            if expected is not None and workflow.status not in set(expected):
                return None
            if expected_version is not None and workflow.plan_version != expected_version:
                return None
            if not can_transition_workflow(workflow.status, to_status):
                return None
            interrupted: list[int] = []
            if cancel_tasks:
                links = self._current_links(conn, workflow_id)
                ids = [link.task_id for link in links]
                statuses = self._task_statuses(conn, ids)
                interrupted = [
                    tid
                    for tid in ids
                    if statuses[tid] in {s.value for s in _IN_FLIGHT_TASK_STATUSES}
                ]
                cancel_reason = reason or "workflow cancelado"
                self._close_open_attempts(
                    conn,
                    self._tasks.transition_tasks(
                        conn, ids, TaskStatus.CANCELLED, reason=cancel_reason
                    ),
                    cancel_reason,
                )
            conn.execute(
                "UPDATE workflows SET status = ?, status_reason = ?, attention = ?, "
                "result = COALESCE(?, result), updated_at = ? WHERE id = ?",
                (to_status.value, reason, attention, result, self._tasks.timestamp(), workflow_id),
            )
            return self._load(conn, workflow_id), tuple(interrupted)

    def set_result(
        self,
        workflow_id: int,
        result: str,
        *,
        expected_status: WorkflowStatus,
        expected_version: int,
        only_if_empty: bool = False,
    ) -> bool:
        """Grava a síntese (CAS por estado e versão do plano); False se o workflow mudou."""
        empty = " AND result IS NULL" if only_if_empty else ""
        with self._tasks.transaction() as conn:
            cur = conn.execute(
                "UPDATE workflows SET result = ?, updated_at = ? "
                "WHERE id = ? AND status = ? AND plan_version = ?" + empty,
                (
                    result,
                    self._tasks.timestamp(),
                    workflow_id,
                    expected_status.value,
                    expected_version,
                ),
            )
            return cur.rowcount == 1

    def claim_consult(self, workflow_id: int, attention: str) -> bool:
        """Reserva (CAS) uma consulta ao agente principal para este motivo, respeitando o limite."""
        with self._tasks.transaction() as conn:
            cur = conn.execute(
                "UPDATE workflows SET attention = ?, consults = consults + 1, updated_at = ? "
                "WHERE id = ? AND consults < ? AND COALESCE(attention, '') != ?",
                (attention, self._tasks.timestamp(), workflow_id, self.limits.max_consults, attention),
            )
            return cur.rowcount == 1

    def adopt(self, workflow_id: int, job_id: int, *, pause_reason: str) -> bool:
        """Adota o workflow no job atual após reinício, numa única transação.

        Move workflow e tasks vigentes para o job, reenfileira as execuções
        interrompidas (fechando suas tentativas) e pausa o workflow se estava em
        ``running``: nenhum executor do novo job reivindica nada antes da pausa.
        Devolve True quando o workflow foi pausado.
        """
        with self._tasks.transaction() as conn:
            workflow = self._load(conn, workflow_id)
            ids = [link.task_id for link in self._current_links(conn, workflow_id)]
            self._tasks.set_tasks_job(conn, ids, job_id)
            paused = workflow.status == WorkflowStatus.RUNNING
            conn.execute(
                "UPDATE workflows SET job_id = ?, status = ?, status_reason = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    job_id,
                    WorkflowStatus.PAUSED.value if paused else workflow.status.value,
                    pause_reason if paused else workflow.status_reason,
                    self._tasks.timestamp(),
                    workflow_id,
                ),
            )
            for tid in ids:
                self._requeue_interrupted(conn, tid, "interrompida por reinicio")
            return paused

    def requeue_orphans(
        self,
        workflow_id: int,
        observed: Mapping[int, tuple[str, int, str | None]],
        reason: str,
    ) -> tuple[int, ...]:
        """Reenfileira as tasks órfãs ainda no estado ``observed`` (lease expirado)."""
        with self._tasks.transaction() as conn:
            return tuple(
                tid
                for tid, seen in observed.items()
                if self._requeue_interrupted(conn, tid, reason, observed=seen)
            )

    def _close_open_attempts(
        self, conn: sqlite3.Connection, task_ids: Iterable[int], reason: str
    ) -> None:
        """Encerra como ``cancelled`` as tentativas sem desfecho das tasks canceladas."""
        ids = list(task_ids)
        if not ids:
            return
        conn.execute(
            "UPDATE task_attempts SET status = 'cancelled', failure_reason = ?, finished_at = ? "
            f"WHERE status IN ('running', 'submitted') AND task_id IN ({', '.join('?' for _ in ids)})",
            (reason, self._tasks.timestamp(), *ids),
        )

    def _requeue_interrupted(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        reason: str,
        *,
        observed: tuple[str, int, str | None] | None = None,
    ) -> bool:
        """Reenfileira com limite de tentativas e encerra a tentativa interrompida."""
        requeued = self._tasks.requeue_interrupted(
            conn, task_id, reason, observed=observed, max_attempts=self.limits.max_task_attempts
        )
        if requeued:
            conn.execute(
                "UPDATE task_attempts SET status = 'interrupted', failure_reason = ?, "
                "finished_at = ? WHERE task_id = ? AND status = 'running'",
                (reason, self._tasks.timestamp(), task_id),
            )
        return requeued

    # ── Tentativas ─────────────────────────────────────────────────────

    def begin_attempt(self, task_id: int, attempt: int, agent: str, run_id: str | None) -> None:
        """Registra o início de uma tentativa (idempotente por task/tentativa/agente)."""
        with self._tasks.transaction() as conn:
            conn.execute(
                "INSERT INTO task_attempts(task_id, attempt, agent, run_id, status, started_at) "
                "VALUES (?, ?, ?, ?, 'running', ?) "
                "ON CONFLICT(task_id, attempt, agent) DO UPDATE SET run_id = excluded.run_id, "
                "status = 'running', failure_reason = NULL, finished_at = NULL",
                (task_id, attempt, agent, run_id, self._tasks.timestamp()),
            )

    def finish_attempt(
        self,
        task_id: int,
        attempt: int,
        agent: str,
        status: str,
        *,
        handoff: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Fecha a tentativa com o desfecho e o handoff estruturado.

        Só transições válidas: execução em curso (``running``) recebe o desfecho
        do executor e uma submetida (``submitted``) recebe o veredito da revisão;
        desfecho já gravado nunca é sobrescrito por um resultado tardio.
        """
        source = "submitted" if status in _REVIEW_VERDICTS else "running"
        with self._tasks.transaction() as conn:
            conn.execute(
                "UPDATE task_attempts SET status = ?, handoff = COALESCE(?, handoff), failure_reason = ?, "
                "finished_at = ? WHERE task_id = ? AND attempt = ? AND agent = ? AND status = ?",
                (
                    status,
                    handoff,
                    failure_reason,
                    self._tasks.timestamp(),
                    task_id,
                    attempt,
                    agent,
                    source,
                ),
            )

    def list_attempts(self, task_id: int) -> list[AttemptRecord]:
        """Tentativas de uma task, da mais antiga à mais nova."""
        conn = self._tasks._conn()
        try:
            rows = conn.execute(
                "SELECT task_id, attempt, agent, status, run_id, failure_reason, handoff, "
                "started_at, finished_at FROM task_attempts WHERE task_id = ? "
                "ORDER BY attempt, started_at",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        return [AttemptRecord(*row) for row in rows]

    # ── Consultas ──────────────────────────────────────────────────────

    def get(self, workflow_id: int) -> WorkflowRecord:
        """Workflow por id; ``WorkflowNotFound`` se não existir."""
        conn = self._tasks._conn()
        try:
            return self._load(conn, workflow_id)
        finally:
            conn.close()

    def list_workflows(
        self,
        *,
        job_id: int | None = None,
        statuses: Iterable[WorkflowStatus] | None = None,
    ) -> list[WorkflowRecord]:
        """Workflows (mais antigos primeiro) filtrados por job e/ou estado."""
        clauses: list[str] = []
        params: list[object] = []
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if statuses is not None:
            values = [status.value for status in statuses]
            clauses.append(f"status IN ({', '.join('?' for _ in values) or 'NULL'})")
            params.extend(values)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._tasks._conn()
        try:
            ids = [
                int(row[0])
                for row in conn.execute(f"SELECT id FROM workflows{where} ORDER BY id", params)
            ]
            return [self._load(conn, workflow_id) for workflow_id in ids]
        finally:
            conn.close()

    def link_for_task(self, task_id: int) -> WorkflowTaskLink | None:
        """Vínculo de uma task com seu workflow, se ela pertencer a algum."""
        conn = self._tasks._conn()
        try:
            row = conn.execute(
                "SELECT workflow_id, item_id, task_id, plan_version, writes, evidence_expected, "
                "superseded_in FROM workflow_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._link(row) if row else None

    def plan(self, workflow_id: int, version: int | None = None) -> WorkflowPlan:
        """Plano de uma versão (padrão: a vigente)."""
        conn = self._tasks._conn()
        try:
            workflow = self._load(conn, workflow_id)
            wanted = workflow.plan_version if version is None else version
            row = conn.execute(
                "SELECT plan_json FROM workflow_plans WHERE workflow_id = ? AND version = ?",
                (workflow_id, wanted),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise WorkflowNotFound(f"workflow {workflow_id} nao tem plano v{wanted}")
        return plan_from_dict(json.loads(row[0]), version=wanted)

    def snapshot(self, workflow_id: int) -> WorkflowSnapshot:
        """Estado completo das tasks vigentes, lido do banco."""
        conn = self._tasks._conn()
        try:
            workflow = self._load(conn, workflow_id)
            links = self._current_links(conn, workflow_id)
            specs = self._plan_specs(conn, workflow_id, workflow.plan_version)
        finally:
            conn.close()
        records = {
            record.id: record
            for record in self._tasks.list_tasks({"ids": [link.task_id for link in links]})
        }
        views = []
        for link in links:
            record = records[link.task_id]
            spec = specs.get(link.item_id)
            views.append(
                TaskView(
                    link=link,
                    status=record.status,
                    assigned_to=record.assigned_to,
                    attempt_count=record.attempt_count,
                    title=record.description,
                    dependencies=spec.item.dependencies if spec else (),
                    result=record.result,
                    notes=record.notes,
                    updated_at=record.updated_at,
                )
            )
        return WorkflowSnapshot(workflow=workflow, tasks=tuple(views))

    # ── Internos ───────────────────────────────────────────────────────

    def _load(self, conn: sqlite3.Connection, workflow_id: int) -> WorkflowRecord:
        row = conn.execute(
            "SELECT id, job_id, objective, coordinator, status, plan_version, global_criteria, "
            "constraints, replans, consults, status_reason, attention, result, created_at, "
            "updated_at FROM workflows WHERE id = ?",
            (int(workflow_id),),
        ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"workflow {workflow_id} nao encontrado")
        return WorkflowRecord(
            id=row[0],
            job_id=row[1],
            objective=row[2],
            coordinator=row[3],
            status=WorkflowStatus(row[4]),
            plan_version=row[5],
            global_criteria=tuple(json.loads(row[6])),
            constraints=tuple(json.loads(row[7])),
            replans=row[8],
            consults=row[9],
            status_reason=row[10],
            attention=row[11],
            result=row[12],
            created_at=row[13],
            updated_at=row[14],
        )

    @staticmethod
    def _link(row: tuple) -> WorkflowTaskLink:
        return WorkflowTaskLink(
            workflow_id=row[0],
            item_id=row[1],
            task_id=row[2],
            plan_version=row[3],
            writes=bool(row[4]),
            evidence_expected=tuple(json.loads(row[5])),
            superseded_in=row[6],
        )

    def _current_links(self, conn: sqlite3.Connection, workflow_id: int) -> list[WorkflowTaskLink]:
        rows = conn.execute(
            "SELECT workflow_id, item_id, task_id, plan_version, writes, evidence_expected, "
            "superseded_in FROM workflow_tasks WHERE workflow_id = ? AND superseded_in IS NULL "
            "ORDER BY id",
            (workflow_id,),
        ).fetchall()
        return [self._link(row) for row in rows]

    @staticmethod
    def _plan_specs(
        conn: sqlite3.Connection, workflow_id: int, version: int
    ) -> dict[str, WorkflowTaskSpec]:
        row = conn.execute(
            "SELECT plan_json FROM workflow_plans WHERE workflow_id = ? AND version = ?",
            (workflow_id, version),
        ).fetchone()
        if row is None:
            return {}
        return {
            raw["id"]: spec_from_dict(raw) for raw in json.loads(row[0])["tasks"]
        }

    @staticmethod
    def _task_statuses(conn: sqlite3.Connection, task_ids: list[int]) -> dict[int, str]:
        if not task_ids:
            return {}
        marks = ", ".join("?" for _ in task_ids)
        return {
            int(tid): status
            for tid, status in conn.execute(
                f"SELECT id, status FROM tasks WHERE id IN ({marks})", task_ids
            )
        }

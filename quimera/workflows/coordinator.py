"""WorkflowCoordinator: serviço de domínio que conduz o objetivo global de um workflow.

Interpreta apenas o estado persistido (nunca eventos isolados nem memória do
chat), aplica regras determinísticas para o trabalho de rotina e só consulta o
agente principal quando há decisão real: plano concluído (síntese e validação
dos critérios globais) ou workflow bloqueado (replanejar ou encerrar).
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone

from ..tasks.repository import TaskRepository
from .models import (
    OPEN_WORKFLOW_STATES,
    TaskView,
    WorkflowError,
    WorkflowLimits,
    WorkflowRecord,
    WorkflowSnapshot,
    WorkflowStatus,
)
from .plan import parse_plan, plan_hash
from .presentation import render_text
from .replan import decide_kept
from .repository import PublishResult, ReplanResult, WorkflowRepository

_logger = logging.getLogger(__name__)

_IN_FLIGHT = frozenset({"in_progress", "reviewing"})
_DEAD = frozenset({"failed", "rejected", "cancelled"})
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

ConsultFn = Callable[[WorkflowRecord, str, str], str | None]


class WorkflowCoordinator:
    """Cria, conduz, replaneja e conclui workflows sobre o sistema de tasks."""

    def __init__(
        self,
        repository: WorkflowRepository,
        tasks: TaskRepository,
        *,
        get_active_agents: Callable[[], list[str]],
        get_job_id: Callable[[], int | None],
        interrupt_tasks: Callable[[Iterable[int]], None] = lambda _ids: None,
        live_task_ids: Callable[[], set[int]] | None = None,
        wake: Callable[[], None] = lambda: None,
        notify: Callable[[str], None] = lambda _message: None,
        consult: ConsultFn | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repository
        self._tasks = tasks
        self._get_active_agents = get_active_agents
        self._get_job_id = get_job_id
        self._interrupt_tasks = interrupt_tasks
        self._live_task_ids = live_task_ids
        self._wake = wake
        self._notify = notify
        self._consult = consult
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def limits(self) -> WorkflowLimits:
        return self._repo.limits

    # ── API de uso pelas tools ─────────────────────────────────────────

    def validate(self, payload: object, *, requested_by: str):
        """Valida o plano sem persistir nada."""
        return parse_plan(payload, coordinator=requested_by, limits=self.limits)

    def create(self, payload: object, *, requested_by: str) -> PublishResult:
        """Valida e publica o plano (atômico e idempotente); fica ``ready`` até o start."""
        plan = self.validate(payload, requested_by=requested_by)
        job_id = self._require_job()
        explicit = payload.get("idempotency_key") if isinstance(payload, dict) else None
        basis = str(explicit) if isinstance(explicit, str) and explicit.strip() else plan_hash(plan)
        key = hashlib.sha256(f"{job_id}|{requested_by}|{basis}".encode()).hexdigest()
        result = self._repo.publish(
            plan,
            job_id=job_id,
            publication_key=key,
            active_agents=self._get_active_agents(),
        )
        self._wake()
        return result

    def start(self, workflow_id: int) -> WorkflowRecord:
        """``ready`` → ``running``; repetir é idempotente."""
        workflow = self._repo.get(workflow_id)
        if workflow.status == WorkflowStatus.RUNNING:
            return workflow
        changed = self._repo.set_status(
            workflow_id,
            WorkflowStatus.RUNNING,
            expected=[WorkflowStatus.READY],
            reason="iniciado",
        )
        if changed is None:
            raise WorkflowError(
                f"workflow {workflow_id} em '{workflow.status.value}' nao pode ser iniciado"
            )
        self._wake()
        return changed[0]

    def pause(self, workflow_id: int) -> WorkflowRecord:
        """Impede novas reivindicações; o que já executa termina."""
        record = self._transition(
            workflow_id,
            WorkflowStatus.PAUSED,
            expected=[WorkflowStatus.RUNNING, WorkflowStatus.BLOCKED],
            reason="pausado",
        )
        self._wake()
        return record

    def resume(self, workflow_id: int) -> WorkflowRecord:
        """``paused`` → ``running`` (adota o workflow no job atual, se vier de outra sessão)."""
        job_id = self._require_job()
        workflow = self._repo.get(workflow_id)
        if workflow.status == WorkflowStatus.RUNNING:
            return workflow
        if workflow.status == WorkflowStatus.PAUSED and workflow.job_id != job_id:
            self._repo.adopt(workflow_id, job_id, pause_reason="pausado")
        record = self._transition(
            workflow_id,
            WorkflowStatus.RUNNING,
            expected=[WorkflowStatus.PAUSED],
            reason="retomado",
        )
        self._wake()
        return self.reconcile(record.id)

    def cancel(self, workflow_id: int, *, reason: str | None = None) -> WorkflowRecord:
        """Cancela o workflow, suas tasks e as execuções em curso (só as dele)."""
        workflow = self._repo.get(workflow_id)
        if workflow.status == WorkflowStatus.CANCELLED:
            return workflow
        changed = self._repo.set_status(
            workflow_id,
            WorkflowStatus.CANCELLED,
            reason=reason or "cancelado",
            cancel_tasks=True,
        )
        if changed is None:
            raise WorkflowError(
                f"workflow {workflow_id} em '{workflow.status.value}' nao pode ser cancelado"
            )
        record, interrupted = changed
        if interrupted:
            self._interrupt_tasks(interrupted)
        self._wake()
        return record

    def replan(
        self, workflow_id: int, payload: object, *, base_version: int
    ) -> ReplanResult:
        """Aplica uma nova versão do plano, preservando o que continua válido."""
        current = self._repo.get(workflow_id)
        plan = parse_plan(
            payload,
            coordinator=current.coordinator,
            limits=self.limits,
            version=base_version + 1,
        )
        result = self._repo.replan(
            workflow_id,
            plan,
            base_version=base_version,
            active_agents=self._get_active_agents(),
            keep_decider=lambda existing, new: decide_kept(existing, new),
        )
        if result.to_interrupt:
            self._interrupt_tasks(result.to_interrupt)
        if result.applied:
            self._wake()
        return result

    def finish(self, workflow_id: int, *, outcome: str, summary: str) -> WorkflowRecord:
        """Conclusão explícita do agente principal: ``completed``, ``blocked`` ou ``failed``."""
        summary = summary.strip()
        if not summary:
            raise WorkflowError("summary e obrigatorio")
        workflow = self._repo.get(workflow_id)
        if outcome == "completed":
            snapshot = self._repo.snapshot(workflow_id)
            workflow = snapshot.workflow
            pending = [v.link.item_id for v in snapshot.tasks if v.status != "completed"]
            if pending:
                raise WorkflowError(
                    "nao e possivel concluir: tasks sem aceite: " + ", ".join(pending)
                )
            if workflow.status == WorkflowStatus.RUNNING:
                # A versão vincula a decisão ao plano verificado: um replan concorrente a anula.
                self._transition(
                    workflow_id,
                    WorkflowStatus.COMPLETED,
                    expected=[WorkflowStatus.RUNNING],
                    expected_version=workflow.plan_version,
                    reason="concluido pelo coordenador",
                )
            elif workflow.status != WorkflowStatus.COMPLETED:
                raise WorkflowError(
                    f"workflow em '{workflow.status.value}' nao pode ser concluido"
                )
            self._record_result(workflow_id, summary, WorkflowStatus.COMPLETED, workflow.plan_version)
        elif outcome == "blocked":
            # Já bloqueado pela reconciliação (caso da consulta ao principal): só registra a decisão.
            if workflow.status != WorkflowStatus.BLOCKED:
                self._transition(
                    workflow_id,
                    WorkflowStatus.BLOCKED,
                    expected=[WorkflowStatus.RUNNING],
                    expected_version=workflow.plan_version,
                    reason=f"bloqueado: {summary}"[:500],
                )
            self._record_result(workflow_id, summary, WorkflowStatus.BLOCKED, workflow.plan_version)
        elif outcome == "failed":
            changed = self._repo.set_status(
                workflow_id,
                WorkflowStatus.FAILED,
                reason=summary[:500],
                cancel_tasks=True,
                result=summary,
                expected=[WorkflowStatus.RUNNING, WorkflowStatus.BLOCKED, WorkflowStatus.PAUSED],
            )
            if changed is None:
                raise WorkflowError(
                    f"workflow em '{workflow.status.value}' nao pode ser marcado como falho"
                )
            if changed[1]:
                self._interrupt_tasks(changed[1])
        else:
            raise WorkflowError("outcome deve ser completed, blocked ou failed")
        self._wake()
        return self._repo.get(workflow_id)

    def snapshot(self, workflow_id: int) -> WorkflowSnapshot:
        return self._repo.snapshot(workflow_id)

    def list_workflows(self, statuses: Iterable[WorkflowStatus] | None = None) -> list[WorkflowRecord]:
        """Workflows do banco, opcionalmente filtrados por estado."""
        return self._repo.list_workflows(statuses=statuses)

    def list_running(self) -> list[WorkflowRecord]:
        """Workflows em execução, candidatos à reconciliação."""
        return self._repo.list_workflows(statuses=[WorkflowStatus.RUNNING])

    # ── Reconciliação determinística ───────────────────────────────────

    def tick(self) -> None:
        """Reavalia todos os workflows em execução (idempotente; segura perda de eventos)."""
        for workflow in self.list_running():
            try:
                self.reconcile(workflow.id)
            except WorkflowError:
                _logger.exception("falha ao reconciliar workflow %s", workflow.id)

    def reconcile(self, workflow_id: int) -> WorkflowRecord:
        """Deriva o estado do workflow a partir do banco; consulta o agente só se preciso."""
        workflow = self._repo.get(workflow_id)
        if workflow.status != WorkflowStatus.RUNNING:
            return workflow
        snapshot = self._requeue_expired(self._repo.snapshot(workflow_id))
        snapshot = self._settle_unrunnable(snapshot)
        views = snapshot.tasks
        if all(view.status == "completed" for view in views):
            return self._conclude(workflow, WorkflowStatus.COMPLETED, "plano concluido", snapshot)
        by_item = {view.link.item_id: view for view in views}
        active = self._get_active_agents()
        in_flight = any(
            view.status in _IN_FLIGHT
            or (view.status == "pending_review" and self._reviewable(view, active))
            for view in views
        )
        runnable = any(
            view.status == "pending"
            and all(by_item[dep].status == "completed" for dep in view.dependencies if dep in by_item)
            for view in views
        )
        if in_flight or runnable:
            return workflow
        failed = sorted(view.link.item_id for view in views if view.status in _DEAD)
        unreviewed = sorted(view.link.item_id for view in views if view.status == "pending_review")
        if unreviewed and not failed:
            reason = "revisao sem revisor elegivel no pool: " + ", ".join(unreviewed)
        else:
            reason = "tarefas bloqueadas por falhas: " + (", ".join(failed) or "dependencias")
        return self._conclude(workflow, WorkflowStatus.BLOCKED, reason, snapshot, failed=failed)

    def recover(self) -> list[WorkflowRecord]:
        """Reconciliação de partida: nada executa após um reinício, então nada pode seguir ativo.

        Sem job atual não há para onde adotar workflows anteriores; nada é alterado
        e a recuperação ocorre na próxima partida com job.
        """
        job_id = self._get_job_id()
        if job_id is None:
            return []
        recovered = []
        for workflow in self._repo.list_workflows(statuses=OPEN_WORKFLOW_STATES):
            # Adoção e pausa na mesma transação: nenhum executor reivindica no intervalo.
            paused = self._repo.adopt(
                workflow.id,
                job_id,
                pause_reason="recuperado apos reinicio; retome com workflows_control resume",
            )
            if paused:
                self._notify(
                    f"[workflow {workflow.id}] recuperado apos reinicio e pausado; "
                    "retome com workflows_control (resume)."
                )
            recovered.append(self._repo.get(workflow.id))
        return recovered

    # ── Internos ───────────────────────────────────────────────────────

    def _require_job(self) -> int:
        job_id = self._get_job_id()
        if job_id is None:
            raise WorkflowError("nenhum job atual disponivel")
        return int(job_id)

    def _transition(
        self,
        workflow_id: int,
        target: WorkflowStatus,
        *,
        expected: list[WorkflowStatus],
        reason: str,
        expected_version: int | None = None,
    ) -> WorkflowRecord:
        changed = self._repo.set_status(
            workflow_id, target, expected=expected, expected_version=expected_version, reason=reason
        )
        if changed is None:
            current = self._repo.get(workflow_id)
            raise WorkflowError(
                f"workflow {workflow_id} em '{current.status.value}' nao pode ir para '{target.value}'"
            )
        return changed[0]

    def _record_result(
        self, workflow_id: int, summary: str, status: WorkflowStatus, version: int
    ) -> None:
        """Grava a síntese do coordenador; falha alto se o workflow mudou no intervalo."""
        if not self._repo.set_result(
            workflow_id, summary, expected_status=status, expected_version=version
        ):
            current = self._repo.get(workflow_id)
            raise WorkflowError(
                f"workflow {workflow_id} mudou para '{current.status.value}' "
                f"v{current.plan_version} antes do registro do resultado"
            )

    def _requeue_expired(self, snapshot: WorkflowSnapshot) -> WorkflowSnapshot:
        """Leases expirados: tasks em voo sem execução viva voltam à fila (nova tentativa)."""
        if self._live_task_ids is None:
            return snapshot
        live = self._live_task_ids()
        deadline = self._now() - timedelta(seconds=self.limits.lease_seconds)
        # O estado observado cerca o requeue: se a task foi concluída e reivindicada
        # de novo depois do snapshot, a nova tentativa não é tocada.
        orphans = {
            view.link.task_id: (view.status, view.attempt_count, view.updated_at)
            for view in snapshot.tasks
            if view.status in _IN_FLIGHT
            and view.link.task_id not in live
            and self._parse_time(view.updated_at) < deadline
        }
        if not orphans:
            return snapshot
        self._repo.requeue_orphans(
            snapshot.workflow.id, orphans, "lease expirado sem execucao ativa"
        )
        return self._repo.snapshot(snapshot.workflow.id)

    def _settle_unrunnable(self, snapshot: WorkflowSnapshot) -> WorkflowSnapshot:
        """Falha tasks que nunca poderão rodar (sem agente elegível ou sem tentativas)."""
        active = self._get_active_agents()
        by_item = {view.link.item_id: view for view in snapshot.tasks}
        changed = False
        for view in snapshot.tasks:
            ready = view.status == "pending" and all(
                by_item[dep].status == "completed"
                for dep in view.dependencies
                if dep in by_item
            )
            if not ready:
                continue
            if view.assigned_to and view.assigned_to not in active:
                # Só esta task volta ao roteador; tasks do agente fora do workflow não são tocadas.
                changed |= self._tasks.unassign_pending_task(view.link.task_id)
                continue
            if view.attempt_count >= self.limits.max_task_attempts:
                reason = f"limite de {self.limits.max_task_attempts} tentativas atingido"
            elif not self._tasks.can_reassign_task(view.link.task_id, active):
                reason = "nenhum agente elegivel restante"
            else:
                continue
            # CAS: se um worker reivindicou a task depois do snapshot, a decisão não vale.
            changed |= self._tasks.fail_task(
                view.link.task_id, reason=reason, expected_status="pending"
            )
        return self._repo.snapshot(snapshot.workflow.id) if changed else snapshot

    def _conclude(
        self,
        workflow: WorkflowRecord,
        target: WorkflowStatus,
        reason: str,
        snapshot: WorkflowSnapshot,
        *,
        failed: list[str] | None = None,
    ) -> WorkflowRecord:
        # CAS também pela versão: um replan aplicado depois do snapshot invalida a decisão.
        changed = self._repo.set_status(
            workflow.id,
            target,
            expected=[WorkflowStatus.RUNNING],
            expected_version=snapshot.workflow.plan_version,
            reason=reason,
        )
        if changed is None:
            return self._repo.get(workflow.id)
        record = changed[0]
        snapshot = WorkflowSnapshot(workflow=record, tasks=snapshot.tasks)
        self._notify(render_text(snapshot))
        attention = (
            f"{target.value}:v{record.plan_version}:{','.join(failed or [])}"
        )
        if self._consult is not None and self._repo.claim_consult(record.id, attention):
            answer = self._consult(record, attention, render_text(snapshot))
            # Só vale se nada mudou durante a consulta (cancelamento, replan, finish).
            if answer and self._repo.set_result(
                record.id,
                answer,
                expected_status=target,
                expected_version=record.plan_version,
                only_if_empty=True,
            ):
                self._notify(f"[workflow {record.id}] {record.coordinator}: {answer}")
            record = self._repo.get(record.id)
        return record

    @staticmethod
    def _reviewable(view: TaskView, active: list[str]) -> bool:
        """Há no pool algum agente além do executor para revisar a task."""
        executor = (view.assigned_to or "").strip().lower()
        return any(agent.strip().lower() != executor for agent in active)

    @staticmethod
    def _parse_time(value: str | None) -> datetime:
        if not value:
            return datetime.min.replace(tzinfo=timezone.utc)
        return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=timezone.utc)

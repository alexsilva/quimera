"""TaskReviewer — review isolado de tasks com dependências explícitas."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..prompt_kinds import PromptKind
from ..constants import TaskStatus
from ..runtime.models import TaskRecord
from ..app.task_events import TaskReviewReassigned


class _DispatchServicesProto(Protocol):
    def call_agent(
        self,
        agent_name: str,
        *,
        handoff: str | dict[str, object],
        handoff_only: bool,
        primary: bool,
        silent: bool,
        persist_history: bool,
        show_output: bool,
        prompt_kind: PromptKind,
    ) -> str | None:
        ...


class _SystemLayerProto(Protocol):
    def show_muted_message(self, message: str) -> None:
        ...


class _TaskRepositoryProto(Protocol):
    def transition_task(
        self,
        task_id: int,
        to_status: TaskStatus | str,
        *,
        result: str | None | object = None,
        notes: str | None | object = None,
        approved_by: str | None | object = None,
    ) -> bool:
        ...

    def requeue_task_after_review(
        self,
        task_id: int,
        failed_agent: str,
        result: str | None = None,
        notes: str | None = None,
    ) -> bool:
        ...

    def complete_task(self, task_id: int, result: str | None = None, reviewed_by: str | None = None) -> bool:
        ...

    def fail_task(self, task_id: int, reason: str | None = None) -> bool:
        ...


class _FailoverPolicyProto(Protocol):
    def has_review_failover(self, executor_agent: str | None, failed_reviewer: str) -> bool:
        ...


class _EventSinkProto(Protocol):
    def publish(self, event: object) -> None:
        ...


class TaskReviewer:
    """Revisa uma task concluída por outro agente, gerencia transições e saída."""

    def __init__(
        self,
        dispatch_services: _DispatchServicesProto,
        system_layer: _SystemLayerProto,
        repository: _TaskRepositoryProto,
        failover_policy: _FailoverPolicyProto,
        classify_task_review_result: Callable[[str | None], tuple[bool, str, str]],
        was_user_cancelled: Callable[[], bool],
        event_sink: _EventSinkProto | None = None,
    ) -> None:
        self.dispatch_services = dispatch_services
        self.system_layer = system_layer
        self.repository = repository
        self.failover_policy = failover_policy
        self.classify_task_review_result = classify_task_review_result
        self.was_user_cancelled = was_user_cancelled
        self.event_sink = event_sink

    def review(self, task: TaskRecord, agent_name: str) -> bool:
        """Revisa a task com o agente informado. Retorna True se aprovada."""
        task_id = task.id
        try:
            executor = task.assigned_to
            if executor == agent_name:
                ok = self.repository.transition_task(
                    task_id,
                    TaskStatus.PENDING_REVIEW,
                    result=task.result,
                    notes=task.notes,
                )
                if ok:
                    self.system_layer.show_muted_message(
                        f"[task {task_id}] {agent_name}: review rejeitado, aguardando outro agente"
                    )
                    if self.event_sink is not None:
                        try:
                            self.event_sink.publish(TaskReviewReassigned(
                                task_id=task_id, job_id=task.job_id,
                                reason="self-review rejected",
                                previous_reviewer=agent_name,
                            ))
                        except Exception:
                            pass
                else:
                    self.system_layer.show_muted_message(
                        f"[task {task_id}] {agent_name}: erro ao rejeitar review \u2014 transi\u00e7\u00e3o inv\u00e1lida"
                    )
                return False

            if executor:
                self.system_layer.show_muted_message(
                    f"[task {task_id}] {agent_name}: revisando execu\u00e7\u00e3o de {executor}"
                )
            else:
                self.system_layer.show_muted_message(f"[task {task_id}] {agent_name}: revisando task")

            task_result = task.result or ""
            description = task.description or ""
            body = task.body or description
            review_handoff = {
                "handoff_id": f"task-review-{task_id}",
                "task": description or f"Revisar task {task_id}",
                "context": (
                    f"Task ID: {task_id}\n"
                    f"Executor: {executor or 'desconhecido'}\n\n"
                    f"Task original:\n{description or '[sem descrição]'}\n\n"
                    f"Escopo enviado:\n{body}\n\n"
                    f"Resultado do executor:\n{task_result or '[sem resultado]'}"
                ),
                "expected": (
                    "Primeira linha com ACEITE, RETENTATIVA, REPLANEJAR ou REJEITAR. "
                    "Depois justifique com evidência concreta e objetiva."
                ),
                "priority": task.priority or "normal",
                "chain": ["task-reviewer", agent_name],
            }
            response = self.dispatch_services.call_agent(
                agent_name,
                handoff=review_handoff,
                handoff_only=True,
                primary=False,
                silent=True,
                persist_history=False,
                show_output=False,
                prompt_kind=PromptKind.TASK_REVIEWER,
            )

            if self.was_user_cancelled():
                self.system_layer.show_muted_message(
                    f"[task {task_id}] {agent_name}: cancelado pelo usu\u00e1rio"
                )
                self.repository.fail_task(task_id, reason="cancelled by user")
                return False

            self.system_layer.show_muted_message(f"[task {task_id}] {agent_name}:\n{response or ''}")
            accepted, verdict, review_text = self.classify_task_review_result(response)
            if not accepted:
                ok = self.repository.requeue_task_after_review(
                    task_id,
                    executor or agent_name,
                    result=task_result,
                    notes=review_text,
                )
                if not ok:
                    self.system_layer.show_muted_message(
                        f"[task {task_id}] {agent_name}: erro ao recolocar task em fila"
                    )
                else:
                    self.system_layer.show_muted_message(
                        f"[task {task_id}] {agent_name}: review pediu {verdict.lower()}, task voltou para pending"
                    )
                return False

            ok = self.repository.complete_task(
                task_id,
                result=task_result,
                reviewed_by=agent_name,
            )
            if not ok:
                self.system_layer.show_muted_message(
                    f"[task {task_id}] {agent_name}: erro ao concluir task ap\u00f3s review"
                )
                return False
            self.system_layer.show_muted_message(f"[task {task_id}] {agent_name}: review conclu\u00eddo")
            return True
        except Exception as exc:
            self.system_layer.show_muted_message(
                f"[task {task_id}] {agent_name}: review falhou: {exc}"
            )
            if self.failover_policy.has_review_failover(executor, agent_name):
                ok = self.repository.transition_task(
                    task_id,
                    TaskStatus.PENDING_REVIEW,
                    result=task.result,
                    notes=str(exc),
                )
                if not ok:
                    self.repository.fail_task(
                        task_id,
                        reason=f"review failed and fallback transition failed: {exc}",
                    )
                else:
                    if self.event_sink is not None:
                        try:
                            self.event_sink.publish(TaskReviewReassigned(
                                task_id=task_id, job_id=task.job_id,
                                reason=f"review exception with fallback: {exc}",
                                previous_reviewer=agent_name,
                            ))
                        except Exception:
                            pass
            else:
                self.repository.fail_task(
                    task_id,
                    reason=f"review failed without operational fallback: {exc}",
                )
            return False

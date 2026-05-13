"""Review de tasks com transições explícitas e fallback operacional."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..constants import TaskStatus
from ..runtime.models import TaskRecord
from .task_repository import TaskRepository


class _DispatchServicesProto(Protocol):
    """Interface mínima de dispatch usada pelo serviço de review."""

    def call_agent(
        self,
        agent_name: str,
        *,
        handoff: str,
        handoff_only: bool,
        primary: bool,
        silent: bool,
        persist_history: bool,
        show_output: bool,
    ) -> str | None:
        """Executa chamada ao agente."""


class _SystemLayerProto(Protocol):
    """Interface mínima de saída para mensagens de sistema."""

    def show_system_message(self, message: str) -> None:
        """Exibe mensagem de sistema."""


class _TaskRepositoryProto(Protocol):
    """Interface mínima de persistência usada no review."""

    def transition_task(
        self,
        task_id: int,
        to_status: TaskStatus | str,
        *,
        result: str | None | object = None,
        notes: str | None | object = None,
        approved_by: str | None | object = None,
    ) -> bool:
        """Transiciona task para outro estado."""

    def requeue_task_after_review(
        self,
        task_id: int,
        failed_agent: str,
        result: str | None = None,
        notes: str | None = None,
    ) -> bool:
        """Retorna task para pending após review."""

    def complete_task(self, task_id: int, result: str | None = None, reviewed_by: str | None = None) -> bool:
        """Conclui task."""

    def fail_task(self, task_id: int, reason: str | None = None) -> bool:
        """Marca task como failed."""


class _FailoverPolicyProto(Protocol):
    """Interface mínima de política de failover/review."""

    def has_review_failover(self, executor_agent: str | None, failed_reviewer: str) -> bool:
        """Retorna se há outro revisor operacional disponível."""


class TaskReviewService:
    """Executa review de tasks concluídas por outro agente."""

    def __init__(
        self,
        dispatch_services: _DispatchServicesProto,
        system_layer: _SystemLayerProto,
        repository: TaskRepository | _TaskRepositoryProto,
        failover_policy: _FailoverPolicyProto,
        classify_task_review_result: Callable[[str | None], tuple[bool, str, str]],
        was_user_cancelled: Callable[[], bool],
    ) -> None:
        self.dispatch_services = dispatch_services
        self.system_layer = system_layer
        self.repository = repository
        self.failover_policy = failover_policy
        self.classify_task_review_result = classify_task_review_result
        self.was_user_cancelled = was_user_cancelled

    def handler_for(self, agent_name: str):
        """Retorna handler de review para o agente informado."""

        def review_handler(task: TaskRecord) -> bool:
            try:
                task_id = task.id
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
                    else:
                        self.system_layer.show_muted_message(
                            f"[task {task_id}] {agent_name}: erro ao rejeitar review — transição inválida"
                        )
                    return False

                if executor:
                    self.system_layer.show_muted_message(
                        f"[task {task_id}] {agent_name}: revisando execução de {executor}"
                    )
                else:
                    self.system_layer.show_muted_message(f"[task {task_id}] {agent_name}: revisando task")

                task_result = task.result or ""
                description = task.description or ""
                body = task.body or description
                review_prompt = (
                    "Faça um review real da task abaixo.\n\n"
                    "Responda com um veredicto explícito na primeira linha: "
                    "ACEITE, RETENTATIVA, REPLANEJAR ou REJEITAR.\n"
                    "Depois justifique com evidência concreta e objetiva.\n\n"
                    f"Task ID: {task_id}\n"
                    f"Executor: {executor or 'desconhecido'}\n"
                    f"Descrição: {description}\n\n"
                    f"Escopo enviado:\n{body}\n\n"
                    f"Resultado do executor:\n{task_result}"
                )
                response = self.dispatch_services.call_agent(
                    agent_name,
                    handoff=review_prompt,
                    handoff_only=True,
                    primary=False,
                    silent=True,
                    persist_history=False,
                    show_output=False,
                )

                if self.was_user_cancelled():
                    self.system_layer.show_muted_message(
                        f"[task {task_id}] {agent_name}: cancelado pelo usuário"
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
                        f"[task {task_id}] {agent_name}: erro ao concluir task após review"
                    )
                    return False
                self.system_layer.show_muted_message(f"[task {task_id}] {agent_name}: review concluído")
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
                    self.repository.fail_task(
                        task_id,
                        reason=f"review failed without operational fallback: {exc}",
                    )
                return False

        return review_handler

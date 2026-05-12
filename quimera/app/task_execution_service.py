"""Execução de tasks com políticas explícitas de review e failover."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..runtime.parser import strip_tool_block
from .task_repository import TaskRepository


class _DispatchServicesProto(Protocol):
    """Interface mínima de dispatch usada pelo executor de tasks."""

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
    """Interface mínima de persistência usada na execução."""

    def fail_task(self, task_id: int, reason: str | None = None) -> bool:
        """Marca task como failed."""

    def requeue_task(self, task_id: int, failed_agent: str, reason: str | None = None) -> bool:
        """Retorna task para pending após falha."""

    def submit_for_review(self, task_id: int, result: str | None = None) -> bool:
        """Submete task para review."""

    def complete_task(self, task_id: int, result: str | None = None, reviewed_by: str | None = None) -> bool:
        """Conclui task."""


class _FailoverPolicyProto(Protocol):
    """Interface mínima de política de failover/review."""

    def review_agents_for(
        self,
        executor_agent: str | None = None,
        exclude_agents: set[str] | None = None,
    ) -> list[str]:
        """Lista agentes revisores elegíveis."""

    def can_failover(self, task_id: int, failed_agent: str) -> bool:
        """Indica se task pode ser reatribuída."""


class TaskExecutionService:
    """Executa tasks e aplica regras de transição/review."""

    def __init__(
        self,
        dispatch_services: _DispatchServicesProto,
        system_layer: _SystemLayerProto,
        repository: TaskRepository | _TaskRepositoryProto,
        failover_policy: _FailoverPolicyProto,
        classify_task_execution_result: Callable[[str | None], tuple[bool, str]],
        was_user_cancelled: Callable[[], bool],
        record_failure: Callable[[str], None] | None = None,
    ) -> None:
        self.dispatch_services = dispatch_services
        self.system_layer = system_layer
        self.repository = repository
        self.failover_policy = failover_policy
        self.classify_task_execution_result = classify_task_execution_result
        self.was_user_cancelled = was_user_cancelled
        self.record_failure = record_failure or (lambda _agent_name: None)

    def handler_for(self, agent_name: str):
        """Retorna handler de execução para o agente informado."""

        def task_handler(task_dict: dict) -> bool:
            try:
                task_id = task_dict["id"]
                description = task_dict.get("description", "")
                body = task_dict.get("body", "") or description
                if not body:
                    self.repository.fail_task(task_id, reason="empty body")
                    return False

                prompt = f"Execute a seguinte tarefa:\n\n{body}"
                review_agents = self.failover_policy.review_agents_for(agent_name)
                desc_preview = (description[:60] + "…") if len(description) > 60 else description
                self.system_layer.show_system_message(
                    f"[task {task_id}] {agent_name}: iniciando — {desc_preview}"
                )

                response = self.dispatch_services.call_agent(
                    agent_name,
                    handoff=prompt,
                    handoff_only=True,
                    primary=False,
                    silent=True,
                    persist_history=False,
                    show_output=False,
                )

                if self.was_user_cancelled():
                    self.system_layer.show_system_message(
                        f"[task {task_id}] {agent_name}: cancelado pelo usuário"
                    )
                    self.repository.fail_task(task_id, reason="cancelled by user")
                    return False

                if response is None:
                    self.system_layer.show_system_message(f"[task {task_id}] {agent_name}: sem resposta")
                    self.record_failure(agent_name)
                    if self.failover_policy.can_failover(task_id, agent_name):
                        self.repository.requeue_task(task_id, agent_name, reason="communication failed")
                    else:
                        self.repository.fail_task(task_id, reason="communication failed")
                    return False

                self.system_layer.show_system_message(
                    f"[task {task_id}] {agent_name}:\n{strip_tool_block(response).strip()}"
                )
                ok, task_result = self.classify_task_execution_result(response)
                if not ok:
                    self.system_layer.show_system_message(f"[task {task_id}] {agent_name}: bloqueada")
                    if self.failover_policy.can_failover(task_id, agent_name):
                        self.repository.requeue_task(task_id, agent_name, reason=task_result)
                    else:
                        self.repository.fail_task(task_id, reason=task_result)
                    return False

                if review_agents:
                    ok = self.repository.submit_for_review(task_id, result=task_result)
                    if not ok:
                        self.system_layer.show_system_message(
                            f"[task {task_id}] {agent_name}: erro ao submeter para review"
                        )
                        return False
                    self.system_layer.show_system_message(
                        f"[task {task_id}] {agent_name}: aguardando review de outro agente"
                    )
                else:
                    ok = self.repository.complete_task(task_id, result=task_result)
                    if not ok:
                        self.system_layer.show_system_message(
                            f"[task {task_id}] {agent_name}: erro ao concluir task"
                        )
                        return False
                    self.system_layer.show_system_message(f"[task {task_id}] {agent_name}: concluída")
                return True
            except Exception as exc:
                self.system_layer.show_system_message(f"[task {task_dict['id']}] {agent_name}: erro: {exc}")
                if self.failover_policy.can_failover(task_dict["id"], agent_name):
                    self.repository.requeue_task(task_dict["id"], agent_name, reason=str(exc))
                else:
                    self.repository.fail_task(task_dict["id"], reason=str(exc))
                return False

        return task_handler

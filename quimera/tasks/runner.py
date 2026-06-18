"""TaskRunner — execução isolada de tasks com dependências explícitas."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..prompt_kinds import PromptKind
from ..runtime.models import TaskRecord


class _DispatchServicesProto(Protocol):
    def delegate(
        self,
        target_agent: str,
        *,
        delegation: str | dict[str, object],
        delegation_only: bool,
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
    def fail_task(self, task_id: int, reason: str | None = None) -> bool:
        ...

    def requeue_task(self, task_id: int, failed_agent: str, reason: str | None = None) -> bool:
        ...

    def submit_for_review(self, task_id: int, result: str | None = None) -> bool:
        ...

    def complete_task(self, task_id: int, result: str | None = None, reviewed_by: str | None = None) -> bool:
        ...


class _FailoverPolicyProto(Protocol):
    def review_agents_for(
        self,
        executor_agent: str | None = None,
        exclude_agents: set[str] | None = None,
    ) -> list[str]:
        ...

    def can_failover(self, task_id: int, failed_agent: str) -> bool:
        ...


class TaskRunner:
    """Executa uma task com um agente, gerencia transições de estado e saída."""

    def __init__(
        self,
        dispatch_services: _DispatchServicesProto,
        system_layer: _SystemLayerProto,
        repository: _TaskRepositoryProto,
        failover_policy: _FailoverPolicyProto,
        classify_task_execution_result: Callable[[str | None], tuple[bool, str]],
        was_user_cancelled: Callable[[], bool],
        record_failure: Callable[[str], None] | None = None,
        before_agent_call: Callable[[str], None] | None = None,
        after_agent_call: Callable[[str], None] | None = None,
    ) -> None:
        self.dispatch_services = dispatch_services
        self.system_layer = system_layer
        self.repository = repository
        self.failover_policy = failover_policy
        self.classify_task_execution_result = classify_task_execution_result
        self.was_user_cancelled = was_user_cancelled
        self.record_failure = record_failure or (lambda _agent_name: None)
        self.before_agent_call = before_agent_call or (lambda _agent_name: None)
        self.after_agent_call = after_agent_call or (lambda _agent_name: None)

    def run(self, task: TaskRecord, agent_name: str) -> bool:
        """Executa a task com o agente informado. Retorna True se bem-sucedida."""
        task_id = task.id
        try:
            description = task.description or ""
            body = task.body or description
            if not body:
                self.repository.fail_task(task_id, reason="empty body")
                return False

            delegation = {
                "delegation_id": f"task-{task_id}",
                "task": description or f"Executar task {task_id}",
                "context": body,
                "expected": (
                    "Execute a task, faça a menor mudança segura, valide com evidência concreta "
                    "e reporte arquivos alterados, validação e próximo passo."
                ),
                "priority": task.priority or "normal",
                "chain": ["task-runner", agent_name],
            }
            review_agents = self.failover_policy.review_agents_for(agent_name)
            desc_preview = (description[:60] + "\u2026") if len(description) > 60 else description
            self.system_layer.show_muted_message(
                f"[task {task_id}] {agent_name}: iniciando \u2014 {desc_preview}"
            )

            self.before_agent_call(agent_name)
            try:
                response = self.dispatch_services.delegate(
                    agent_name,
                    delegation=delegation,
                    delegation_only=True,
                    primary=False,
                    persist_history=False,
                    show_output=False,
                    silent=True,
                    prompt_kind=PromptKind.TASK_EXECUTOR,
                )
            finally:
                self.after_agent_call(agent_name)

            if self.was_user_cancelled():
                self.system_layer.show_muted_message(
                    f"[task {task_id}] {agent_name}: cancelado pelo usu\u00e1rio"
                )
                self.repository.fail_task(task_id, reason="cancelled by user")
                return False

            if response is None:
                self.system_layer.show_muted_message(f"[task {task_id}] {agent_name}: sem resposta")
                self.record_failure(agent_name)
                if self.failover_policy.can_failover(task_id, agent_name):
                    self.repository.requeue_task(task_id, agent_name, reason="communication failed")
                else:
                    self.repository.fail_task(task_id, reason="communication failed")
                return False

            self.system_layer.show_muted_message(
                f"[task {task_id}] {agent_name}:\n{response.strip()}"
            )
            ok, task_result = self.classify_task_execution_result(response)
            if not ok:
                self.system_layer.show_muted_message(f"[task {task_id}] {agent_name}: bloqueada")
                if self.failover_policy.can_failover(task_id, agent_name):
                    self.repository.requeue_task(task_id, agent_name, reason=task_result)
                else:
                    self.repository.fail_task(task_id, reason=task_result)
                return False

            if review_agents:
                ok = self.repository.submit_for_review(task_id, result=task_result)
                if not ok:
                    self.system_layer.show_muted_message(
                        f"[task {task_id}] {agent_name}: erro ao submeter para review"
                    )
                    return False
                self.system_layer.show_muted_message(
                    f"[task {task_id}] {agent_name}: aguardando review de outro agente"
                )
            else:
                ok = self.repository.complete_task(task_id, result=task_result)
                if not ok:
                    self.system_layer.show_muted_message(
                        f"[task {task_id}] {agent_name}: erro ao concluir task"
                    )
                    return False
                self.system_layer.show_muted_message(
                    f"[task {task_id}] {agent_name}: concluída"
                )
            return True
        except Exception as exc:
            self.system_layer.show_muted_message(f"[task {task_id}] {agent_name}: erro: {exc}")
            if self.failover_policy.can_failover(task_id, agent_name):
                self.repository.requeue_task(task_id, agent_name, reason=str(exc))
            else:
                self.repository.fail_task(task_id, reason=str(exc))
            return False

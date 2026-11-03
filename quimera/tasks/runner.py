"""TaskRunner — execução isolada de tasks com dependências explícitas."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..domain.task_states import TaskStatus
from ..prompt_kinds import PromptKind
from ..runtime.models import TaskRecord
from .attempt_fence import AttemptFence
from .utils import describe_rework


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
    def fail_task(
        self, task_id: int, reason: str | None = None, *, expected_attempt: int | None = None
    ) -> bool:
        ...

    def requeue_task(
        self,
        task_id: int,
        failed_agent: str,
        reason: str | None = None,
        *,
        expected_attempt: int | None = None,
        exclude_agent: bool = True,
    ) -> bool:
        ...

    def submit_for_review(
        self, task_id: int, result: str | None = None, *, expected_attempt: int | None = None
    ) -> bool:
        ...

    def complete_task(
        self,
        task_id: int,
        result: str | None = None,
        reviewed_by: str | None = None,
        *,
        expected_attempt: int | None = None,
    ) -> bool:
        ...


class _TaskHooksProto(Protocol):
    """Ganchos opcionais para tasks de workflow (contexto, tentativas, handoff)."""

    def delegation_for(self, task: TaskRecord, agent: str, base: dict[str, object]) -> dict[str, object]:
        ...

    def begin(self, task: TaskRecord, agent: str) -> None:
        ...

    def finish(self, task: TaskRecord, agent: str, response: str) -> tuple[bool, str]:
        ...

    def abort(self, task: TaskRecord, agent: str, reason: str) -> None:
        ...

    def rework(self, task: TaskRecord, owner: str | None, reason: str) -> TaskStatus | None:
        ...

    def discarded(self, task: TaskRecord) -> bool:
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

    def is_available(self, agent_name: str) -> bool:
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
        before_agent_call: Callable[[str], None] | None = None,
        after_agent_call: Callable[[str], None] | None = None,
        task_hooks: _TaskHooksProto | None = None,
    ) -> None:
        self.dispatch_services = dispatch_services
        self.system_layer = system_layer
        self.repository = repository
        self.failover_policy = failover_policy
        self.classify_task_execution_result = classify_task_execution_result
        self.was_user_cancelled = was_user_cancelled
        self.before_agent_call = before_agent_call or (lambda _agent_name: None)
        self.after_agent_call = after_agent_call or (lambda _agent_name: None)
        self.task_hooks = task_hooks

    def handler_for(self, agent_name: str) -> Callable[[TaskRecord], bool]:
        """Retorna handler de execução compatível com TaskExecutor."""
        return lambda task: self.run(task, agent_name)

    def run(self, task: TaskRecord, agent_name: str) -> bool:
        """Executa a task com o agente informado. Retorna True se bem-sucedida."""
        task_id = task.id
        hooks = self.task_hooks if task.origin == "workflow" else None
        # Tasks de workflow só aceitam escrita da tentativa que as reivindicou.
        fence: AttemptFence = {"expected_attempt": task.attempt_count} if hooks is not None else {}
        try:
            description = task.description or ""
            body = task.body or description
            if not body:
                self.repository.fail_task(task_id, reason="empty body", **fence)
                return False

            delegation: dict[str, object] = {
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
            if hooks is not None:
                delegation = hooks.delegation_for(task, agent_name, delegation)
            desc_preview = (description[:60] + "\u2026") if len(description) > 60 else description
            self.system_layer.show_muted_message(
                f"[task {task_id}] {agent_name}: iniciando \u2014 {desc_preview}"
            )

            if hooks is not None:
                hooks.begin(task, agent_name)
                if hooks.discarded(task):
                    # Cancelada/substituída entre o claim e o dispatch: o agente não roda.
                    self._abort(hooks, task, agent_name, "interrompida pelo workflow")
                    return False
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
                self._abort(hooks, task, agent_name, "cancelled by user")
                self.repository.fail_task(task_id, reason="cancelled by user", **fence)
                return False

            if response is None and hooks is not None and hooks.discarded(task):
                # Interrompida por cancelamento/replanejamento: não é falha do agente.
                self._abort(hooks, task, agent_name, "interrompida pelo workflow")
                return False

            if hooks is not None and response is not None and not response.strip():
                # Resposta vazia não é entrega: em workflow conta como agente que não respondeu.
                response = None
            if response is None:
                # A falha do agente já foi contabilizada pelo AgentCallService do
                # dispatch, depois dos retries internos; contar aqui de novo
                # suspendia o agente com uma única task sem resposta.
                reason = (
                    "communication failed"
                    if self.failover_policy.is_available(agent_name)
                    else "suspenso durante a run (rate limit, cota ou falhas seguidas)"
                )
                self.system_layer.show_muted_message(f"[task {task_id}] {agent_name}: sem resposta")
                self._abort(hooks, task, agent_name, reason)
                self._hand_off(task, agent_name, reason, fence)
                return False

            self.system_layer.show_muted_message(
                f"[task {task_id}] {agent_name}:\n{response.strip()}"
            )
            if hooks is not None:
                # Em workflow quem julga a entrega é o handoff verificado: as palavras-chave
                # de bloqueio das tasks avulsas reprovavam relatórios válidos que citam
                # "cannot". Entrega reprovada volta para o mesmo agente refazer.
                ok, task_result = hooks.finish(task, agent_name, response)
                if not ok:
                    if hooks.discarded(task):
                        # Cancelada ou substituída durante a run: não há entrega a devolver.
                        self.system_layer.show_muted_message(
                            f"[task {task_id}] {agent_name}: {task_result}"
                        )
                        return False
                    status = hooks.rework(task, agent_name, task_result)
                    self.system_layer.show_muted_message(
                        f"[task {task_id}] {agent_name}: entrega devolvida ({task_result}); "
                        + describe_rework(status, agent_name)
                    )
                    return False
            else:
                ok, task_result = self.classify_task_execution_result(response)
                if not ok:
                    self.system_layer.show_muted_message(f"[task {task_id}] {agent_name}: bloqueada")
                    self._hand_off(task, agent_name, task_result, fence)
                    return False

            # Revisores contados no fim da run e entre os membros do pool: um
            # revisor suspenso agora volta e revisa; a foto do início dispensava a revisão.
            if self.failover_policy.review_agents_for(agent_name):
                ok = self.repository.submit_for_review(task_id, result=task_result, **fence)
                if not ok:
                    self.system_layer.show_muted_message(
                        f"[task {task_id}] {agent_name}: erro ao submeter para review"
                    )
                    return False
                self.system_layer.show_muted_message(
                    f"[task {task_id}] {agent_name}: aguardando review de outro agente"
                )
            else:
                ok = self.repository.complete_task(task_id, result=task_result, **fence)
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
            self._abort(hooks, task, agent_name, str(exc))
            self._hand_off(task, agent_name, str(exc), fence)
            return False

    def _hand_off(self, task: TaskRecord, agent: str, reason: str, fence: AttemptFence) -> None:
        """Falha do agente: a task passa a outro, ou falha quando ninguém mais pode executá-la.

        Agente que ficou indisponível na run (suspenso por rate limit ou falhas
        seguidas) não falhou nesta task: ela volta à fila para quem estiver
        disponível, sem excluí-lo, e espera se ninguém estiver. O que segue
        disponível e falhou não a reivindica mais.
        """
        if not self.failover_policy.is_available(agent):
            self.repository.requeue_task(
                task.id, agent, reason=f"{agent} indisponivel: {reason}", exclude_agent=False, **fence
            )
        elif self.failover_policy.can_failover(task.id, agent):
            self.repository.requeue_task(task.id, agent, reason=reason, **fence)
        else:
            self.repository.fail_task(task.id, reason=reason, **fence)

    @staticmethod
    def _abort(hooks: _TaskHooksProto | None, task: TaskRecord, agent: str, reason: str) -> None:
        if hooks is not None:
            hooks.abort(task, agent, reason)

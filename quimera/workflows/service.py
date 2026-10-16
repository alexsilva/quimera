"""WorkflowService: composição do coordenador com o ciclo de vida da aplicação.

Mantém um monitor leve que chama a reconciliação determinística do
``WorkflowCoordinator``: acordado por eventos de task e mudanças de estado e,
só enquanto houver workflow em ``running``, também por um poll espaçado (que
cobre eventos perdidos e leases expirados). Sem workflow ativo ele dorme. Um
reconcile que termina publica o resumo e só reacorda o monitor se chegou um
pedido enquanto ele estava ocupado: nunca por conta própria, senão o monitor
giraria sem parar enquanto houvesse workflow em execução. Nenhuma chamada de
modelo fica aberta aguardando workers: o agente principal só é consultado
quando o coordenador identifica uma decisão real.

Uma única sessão do Quimera coordena os workflows de um workspace: a que detém
o ``CoordinationLock`` (flock, liberado pelo sistema quando o processo morre).
As demais não adotam, não reconciliam e recusam operações de workflow, e
assumem a coordenação quando o lock fica livre.
"""
from __future__ import annotations

import fcntl
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import IO

from ..prompt_kinds import PromptKind
from .coordinator import WorkflowCoordinator
from .models import OPEN_WORKFLOW_STATES, WorkflowError, WorkflowRecord
from .presentation import overview

_logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 5.0
_MAX_PARALLEL_RECONCILES = 3

DecisionDelegate = Callable[..., str | None]


class CoordinationLock:
    """Lock exclusivo (flock, não bloqueante) que elege a sessão coordenadora do workspace."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> bool:
        """True quando este processo detém (ou passou a deter) o lock."""
        if self._handle is not None:
            return True
        handle = open(self._path, "a", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class WorkflowService:
    """Monitor + consulta ao agente principal em torno de um ``WorkflowCoordinator``."""

    def __init__(
        self,
        coordinator: WorkflowCoordinator,
        *,
        refresh_state: Callable[[], None] = lambda: None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        lock: CoordinationLock | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._lock = lock
        self._coordinating = lock is None
        self._refresh_state = refresh_state
        self._poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._last_overview: list[dict] = []
        self._busy: set[int] = set()
        self._pending: set[int] = set()  # pedidos chegados enquanto o workflow reconciliava
        self._busy_lock = threading.Lock()
        self._overview_lock = threading.Lock()
        self._has_running = False

    @property
    def coordinator(self) -> WorkflowCoordinator:
        """Coordenador desta sessão; recusa quando outra sessão coordena o workspace."""
        if not self._coordinating:
            raise WorkflowError(
                "os workflows deste workspace sao coordenados por outra sessao do Quimera; "
                "use aquela sessao ou encerre-a para esta assumir"
            )
        return self._coordinator

    @property
    def coordinating(self) -> bool:
        return self._coordinating

    # ── Ciclo de vida ──────────────────────────────────────────────────

    def start(self) -> None:
        """Assume a coordenação se o lock estiver livre e inicia o monitor."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped.clear()
        with self._busy_lock:
            self._busy.clear()  # reconciles cancelados num stop anterior não rodaram o finally
            self._pending.clear()
        self._take_coordination()
        self._pool = ThreadPoolExecutor(
            max_workers=_MAX_PARALLEL_RECONCILES, thread_name_prefix="workflow"
        )
        self._thread = threading.Thread(
            target=self._loop, name="quimera-workflow-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Encerra o monitor; workflows ativos continuam persistidos para recuperação."""
        self._stopped.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        if self._lock is not None:
            self._coordinating = False
            self._lock.release()

    def wake(self) -> None:
        """Acelera a próxima reconciliação (eventos de task); não é fonte de verdade."""
        self._wake.set()

    # ── Monitor ────────────────────────────────────────────────────────

    def _take_coordination(self) -> None:
        """Tenta o lock; ao obtê-lo, adota e recupera os workflows abertos do workspace."""
        if not self._coordinating and (self._lock is None or not self._lock.acquire()):
            return
        self._coordinator.recover()
        self._has_running = bool(self._coordinator.list_running())
        if self._stopped.is_set() and self._lock is not None:
            return  # stop() concorrente já liberou o lock: esta sessão não coordena
        # Só depois da recuperação as operações de workflow passam a valer nesta sessão.
        self._coordinating = True

    def _loop(self) -> None:
        while not self._stopped.is_set():
            # Sem a coordenação, o poll só serve para assumir o lock quando ele ficar livre.
            poll = self._has_running or not self._coordinating
            self._wake.wait(self._poll_seconds if poll else None)
            self._wake.clear()
            if self._stopped.is_set():
                break
            try:
                if not self._coordinating:
                    self._take_coordination()
                    if not self._coordinating:
                        continue
                self._dispatch_reconciles()
                self._publish_overview()
            except Exception:  # noqa: BLE001 - o monitor não pode morrer por uma falha pontual
                _logger.exception("falha no ciclo do monitor de workflows")

    def _publish_overview(self) -> None:
        """Atualiza o shared_state só quando o resumo dos workflows abertos muda."""
        with self._overview_lock:
            current = self.open_overview()
            if current != self._last_overview:
                self._last_overview = current
                self._refresh_state()

    def _dispatch_reconciles(self) -> None:
        pool = self._pool
        if pool is None:
            return
        running = self._coordinator.list_running()
        self._has_running = bool(running)
        for workflow in running:
            with self._busy_lock:
                if workflow.id in self._busy:
                    # O pedido chegou durante o reconcile: reavalia uma vez ao terminar.
                    self._pending.add(workflow.id)
                    continue
                self._busy.add(workflow.id)
            pool.submit(self._reconcile_one, workflow.id)

    def _reconcile_one(self, workflow_id: int) -> None:
        try:
            self._coordinator.reconcile(workflow_id)
        except Exception:  # noqa: BLE001 - registrado; a próxima rodada reavalia pelo banco
            _logger.exception("falha ao reconciliar workflow %s", workflow_id)
        finally:
            with self._busy_lock:
                self._busy.discard(workflow_id)
                again = workflow_id in self._pending
                self._pending.discard(workflow_id)
            if not self._stopped.is_set():
                # O reconcile pode ter mudado o estado (requeue, falha, conclusão):
                # o resumo sai daqui, sem reacordar o monitor para um novo ciclo.
                self._publish_overview()
            if again:
                self._wake.set()

    # ── Apresentação ───────────────────────────────────────────────────

    def open_overview(self) -> list[dict]:
        """Resumo dos workflows em andamento para o shared_state."""
        coordinator = self._coordinator
        return overview(
            [coordinator.snapshot(w.id) for w in coordinator.list_workflows(OPEN_WORKFLOW_STATES)]
        )


def build_decision_consult(
    delegate: DecisionDelegate,
    *,
    notify: Callable[[str], None],
) -> Callable[[WorkflowRecord, str, str], str | None]:
    """Cria a consulta ao agente principal usada pelo coordenador em pontos de decisão."""

    def consult(workflow: WorkflowRecord, attention: str, status_text: str) -> str | None:
        state = workflow.status.value
        delegation = {
            "delegation_id": f"workflow-{workflow.id}-decision",
            "task": f"Decisao do workflow {workflow.id} ({state})",
            "context": (
                f"{status_text}\n\n"
                "O runtime reconciliou o estado a partir do banco e precisa da sua decisao como "
                "coordenador. Use workflows_status (include_results=true) para conferir as "
                "evidencias; nao confie em afirmacoes sem evidencia."
            ),
            "expected": (
                "Se todos os criterios globais foram atendidos: chame workflows_finish "
                "(outcome=completed) e responda com a sintese final fundamentada nas evidencias. "
                "Se faltar trabalho ou o plano mudou: chame workflows_replan com a lista "
                "completa de tasks. Se estiver bloqueado ou faltar informacao: chame "
                "workflows_finish (outcome=blocked ou failed) explicando o motivo real, "
                "sem inventar resultado."
            ),
            "priority": "high",
            "chain": ["workflow", f"wf-{workflow.id}", workflow.coordinator],
        }
        try:
            return delegate(
                workflow.coordinator,
                delegation=delegation,
                delegation_only=True,
                primary=False,
                persist_history=False,
                show_output=False,
                silent=True,
                prompt_kind=PromptKind.DELEGATION,
            )
        except Exception as exc:  # noqa: BLE001 - fronteira de transporte/modelo, reportada ao usuário
            _logger.exception("consulta ao coordenador do workflow %s falhou", workflow.id)
            notify(
                f"[workflow {workflow.id}] consulta a {workflow.coordinator} falhou ({exc}); "
                "o estado persistido foi mantido para decisao manual."
            )
            return None

    return consult


def task_event_wake(service: WorkflowService) -> Callable[[object], None]:
    """Handler de eventos de task que apenas acorda o monitor."""

    def handler(_event: object) -> None:
        service.wake()

    return handler


__all__ = [
    "CoordinationLock",
    "DEFAULT_POLL_SECONDS",
    "WorkflowService",
    "build_decision_consult",
    "task_event_wake",
]

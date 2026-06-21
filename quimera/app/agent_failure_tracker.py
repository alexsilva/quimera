"""Rastreamento de falhas consecutivas de agentes."""
import threading
from collections import defaultdict
from typing import Callable

from .config import logger


class AgentFailureTracker:
    """Conta falhas consecutivas por agente e aplica política de remoção do pool.

    Responsabilidades:
    - Manter contadores de falha por agente
    - Remover agente do pool quando threshold é atingido
    - Delegar side-effects (métricas, bug filing) via callbacks injetados
    """

    FAILURE_THRESHOLD = 2

    def __init__(
        self,
        *,
        normalize_agent_name: Callable,
        agent_pool,
        release_agent_tasks: Callable[[str], None],
        record_metric: Callable[[str], None] | None = None,
        file_bug: Callable[..., None] | None = None,
        get_session_id: Callable[[], str] | None = None,
        notify_warning: Callable[[str], None] | None = None,
    ) -> None:
        self._failures: defaultdict = defaultdict(int)
        self._lock = threading.Lock()
        self._normalize = normalize_agent_name
        self._agent_pool = agent_pool
        self._release_agent_tasks = release_agent_tasks
        self._record_metric = record_metric
        self._file_bug = file_bug
        self._get_session_id = get_session_id
        self._notify_warning = notify_warning

    @property
    def failures(self) -> defaultdict:
        """Retorna o dicionário de contadores de falha."""
        return self._failures

    @property
    def lock(self) -> threading.Lock:
        """Retorna o lock usado para acesso ao dicionário de falhas."""
        return self._lock

    def record_success(self, agent) -> None:
        """Reseta o contador de falhas de um agente após resposta bem-sucedida."""
        name = self._normalize(agent)
        if not name:
            return
        with self._lock:
            if self._failures.get(name, 0) > 0:
                self._failures[name] = 0
                logger.debug("agent %s failure counter reset after success", name)

    def record_failure(self, agent) -> None:
        """Registra uma falha do agente e aplica política quando threshold é atingido."""
        name = self._normalize(agent)
        if not name:
            return
        with self._lock:
            self._failures[name] += 1
            failures = self._failures[name]
        if failures >= self.FAILURE_THRESHOLD:
            if name in self._agent_pool:
                self._agent_pool.remove(name)
                logger.debug("agent %s removed after %d failures", name, failures)
                if self._notify_warning is not None:
                    self._notify_warning(f"{name} foi removido após falhas repetidas")
                try:
                    self._release_agent_tasks(name)
                except Exception:
                    pass
        if self._record_metric is not None:
            self._record_metric(name)
        if failures == self.FAILURE_THRESHOLD and self._file_bug is not None:
            session_id = self._get_session_id() if self._get_session_id is not None else ""
            self._file_bug(
                session_id=session_id,
                category="agent_failure_burst",
                summary=f"Agente {name} acumulou falhas consecutivas",
                severity="medium",
                confidence=0.85,
                description=f"Falhas consecutivas atuais: {failures}",
                agent=name,
            )

"""Rastreamento de falhas consecutivas de agentes."""
import threading
from collections import defaultdict
from typing import Callable

from .config import logger


class AgentFailureTracker:
    """Conta falhas consecutivas por agente e o suspende do pool por um prazo.

    Responsabilidades:
    - Manter contadores de falha por agente
    - Suspender o agente (``AgentPool.suspend``) quando o threshold é atingido;
      o prazo dobra a cada suspensão seguida e tudo zera no primeiro sucesso
    - Delegar side-effects (métricas, bug filing) via callbacks injetados

    O agente suspenso continua no pool: o prefixo explícito segue funcionando
    e, vencido o prazo, ele volta à rotação sozinho. Isso cobre o caso que
    motivou o mecanismo (crédito ou limite de uso esgotado), que reseta em
    minutos ou horas, sem perder o agente pelo resto da sessão.
    """

    FAILURE_THRESHOLD = 2
    COOLDOWN_BASE_SECONDS = 5 * 60
    COOLDOWN_MAX_SECONDS = 60 * 60

    def __init__(
        self,
        *,
        normalize_agent_name: Callable,
        agent_pool,
        release_agent_tasks: Callable[[str], None],
        record_metric: Callable[[str], None] | None = None,
        file_bug: Callable[..., None] | None = None,
        get_session_id: Callable[[], str] | None = None,
    ) -> None:
        self._failures: defaultdict = defaultdict(int)
        self._strikes: defaultdict = defaultdict(int)
        self._lock = threading.Lock()
        self._normalize = normalize_agent_name
        self._agent_pool = agent_pool
        self._release_agent_tasks = release_agent_tasks
        self._record_metric = record_metric
        self._file_bug = file_bug
        self._get_session_id = get_session_id

    @property
    def failures(self) -> defaultdict:
        """Retorna o dicionário de contadores de falha."""
        return self._failures

    @property
    def lock(self) -> threading.Lock:
        """Retorna o lock usado para acesso ao dicionário de falhas."""
        return self._lock

    @classmethod
    def cooldown_seconds(cls, strike: int) -> int:
        """Prazo da ``strike``-ésima suspensão seguida: 5, 10, 20, 40 min, teto de 1 h."""
        return min(cls.COOLDOWN_BASE_SECONDS * 2 ** max(0, strike - 1), cls.COOLDOWN_MAX_SECONDS)

    def record_success(self, agent) -> None:
        """Zera os contadores do agente e encerra a suspensão após resposta bem-sucedida."""
        name = self._normalize(agent)
        if not name:
            return
        with self._lock:
            had_failures = self._failures.get(name, 0) > 0
            self._failures[name] = 0
            self._strikes[name] = 0
            resumed = name in self._agent_pool and self._agent_pool.resume(name)
        if resumed:
            logger.info("agent %s available again after success", name)
        elif had_failures:
            logger.debug("agent %s failure counter reset after success", name)

    def record_failure(self, agent) -> None:
        """Registra uma falha do agente e o suspende quando o threshold é atingido."""
        name = self._normalize(agent)
        if not name:
            return
        # Decisão e suspensão sob o mesmo lock: um sucesso concorrente que zere
        # o contador não pode ser atropelado por uma suspensão com valor antigo.
        with self._lock:
            self._failures[name] += 1
            failures = self._failures[name]
            cooldown = None
            if failures >= self.FAILURE_THRESHOLD and name in self._agent_pool:
                self._strikes[name] += 1
                cooldown = self.cooldown_seconds(self._strikes[name])
                self._agent_pool.suspend(
                    name, seconds=cooldown, reason=f"{failures} falhas seguidas"
                )
        if cooldown is not None:
            logger.warning("agent %s suspended for %ds after %d failures", name, cooldown, failures)
            try:
                self._release_agent_tasks(name)
            except Exception as exc:
                logger.warning("falha ao liberar as tasks do agente %s: %s", name, exc)
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

"""AgentCallService — chamada com retry para agentes.

Encapsula o loop de retry com backoff progressivo e detecção de
rate limit, sem depender de QuimeraApp. Dependências injetadas
como callables.
"""

import time

from .config import logger


class AgentCallService:
    """Encapsula retry com backoff para chamadas a agentes.

    Dependências injetadas como callables — sem acesso a QuimeraApp.
    """

    def __init__(
        self,
        max_retries: int = 2,
        retry_backoff: float = 1.0,
        rate_limit_backoff: float = 30.0,
        record_failure=None,
        record_success=None,
        is_rate_limited=None,
    ):
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._rate_limit_backoff = rate_limit_backoff
        self._record_failure = record_failure or (lambda agent: None)
        self._record_success = record_success or (lambda agent: None)
        self._is_rate_limited = is_rate_limited or (lambda: False)

    def call(
        self,
        agent: str,
        call_fn,
        resolve_fn,
        is_user_cancelled,
        max_retries: int | None = None,
    ):
        """Executa chamada com retry.

        Args:
            agent: Nome do agente alvo.
            call_fn: Callable(agent) → response str | None (chamada bruta).
            resolve_fn: Callable(agent, response) → result str | None (tool loop).
            is_user_cancelled: Callable() → bool (cancelamento do usuário).
        """
        last_error = None

        effective_max_retries = self._max_retries
        if isinstance(max_retries, int):
            effective_max_retries = max(1, max_retries)

        for attempt in range(1, effective_max_retries + 1):
            if is_user_cancelled():
                logger.info(
                    "[AGENT_CALL] agent=%s cancelled by user before retry %d/%d, aborting",
                    agent, attempt, effective_max_retries,
                )
                return None

            try:
                response = call_fn(agent)
                if response is None:
                    if is_user_cancelled():
                        logger.info("[AGENT_CALL] agent=%s cancelled by user, aborting", agent)
                        return None
                    if attempt < effective_max_retries:
                        backoff = self._compute_backoff(attempt)
                        logger.warning(
                            "[AGENT_CALL] retry %d/%d for agent=%s (no response)",
                            attempt, effective_max_retries, agent,
                        )
                        time.sleep(backoff)
                        continue
                    self._record_failure(agent)
                    return None

                result = resolve_fn(agent, response)
                if result is None:
                    if is_user_cancelled():
                        logger.info("[AGENT_CALL] agent=%s cancelled by user, aborting", agent)
                        return None
                    if attempt < effective_max_retries:
                        backoff = self._compute_backoff(attempt)
                        logger.warning(
                            "[AGENT_CALL] retry %d/%d for agent=%s (resolve failed)",
                            attempt, effective_max_retries, agent,
                        )
                        time.sleep(backoff)
                        continue
                    self._record_failure(agent)
                else:
                    self._record_success(agent)
                return result

            except Exception as exc:
                if is_user_cancelled():
                    logger.info("[AGENT_CALL] agent=%s cancelled by user, aborting", agent)
                    return None
                last_error = exc
                if attempt < effective_max_retries:
                    logger.warning(
                        "[AGENT_CALL] retry %d/%d for agent=%s after exception: %s",
                        attempt, effective_max_retries, agent, exc,
                    )
                    time.sleep(self._retry_backoff * attempt)
                    continue
                self._record_failure(agent)
                raise

        if last_error:
            logger.error("[AGENT_CALL] all retries exhausted for agent=%s: %s", agent, str(last_error))
        return None

    def _compute_backoff(self, attempt: int) -> float:
        if self._is_rate_limited():
            return self._rate_limit_backoff
        return self._retry_backoff * attempt

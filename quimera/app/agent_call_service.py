"""AgentCallService — chamada com retry para agentes.

Encapsula o loop de retry com backoff progressivo e detecção de
rate limit, sem depender de QuimeraApp. Dependências injetadas
como callables.
"""

import math
import time

from .config import logger

# Espera máxima por um rate limit antes de dar o agente como indisponível.
DEFAULT_MAX_RATE_LIMIT_WAIT = 120.0


class AgentCallService:
    """Encapsula retry com backoff para chamadas a agentes.

    Dependências injetadas como callables — sem acesso a QuimeraApp.
    """

    def __init__(
        self,
        max_retries: int = 2,
        retry_backoff: float = 1.0,
        rate_limit_backoff: float = 30.0,
        max_rate_limit_wait: float = DEFAULT_MAX_RATE_LIMIT_WAIT,
        record_failure=None,
        record_success=None,
        record_rate_limited=None,
        is_rate_limited=None,
        get_retry_after=None,
        before_retry=None,
        notify_warning=None,
        notify_retry=None,
        notify_error=None,
    ):
        """Rate limit cuja espera passa de ``max_rate_limit_wait`` segundos não é
        esperado: o agente fica indisponível (``record_rate_limited(agent,
        retry_after)``; sem o callback, ``record_failure``) e a chamada devolve
        None. Esperar o ``retry-after`` de uma cota esgotada (minutos, horas ou
        dias) prenderia o chamador num agente que não vai responder. Execuções
        de task em background usam 0: desistem no primeiro rate limit e a task
        segue com outro agente."""
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._rate_limit_backoff = rate_limit_backoff
        self._max_rate_limit_wait = max_rate_limit_wait
        self._record_failure = record_failure or (lambda agent: None)
        self._record_success = record_success or (lambda agent: None)
        self._record_rate_limited = record_rate_limited or (
            lambda agent, retry_after: self._record_failure(agent)
        )
        self._is_rate_limited = is_rate_limited or (lambda: False)
        self._get_retry_after = get_retry_after or (lambda: None)
        self._before_retry = before_retry or (lambda agent, attempt, reason: None)
        self._notify_warning = notify_warning or (lambda msg: None)
        self._notify_retry = notify_retry or (lambda *args, **kwargs: None)
        self._notify_error = notify_error or (lambda msg: None)

    def call(
        self,
        agent: str,
        call_fn,
        resolve_fn,
        is_user_cancelled,
        max_retries: int | None = None,
        is_fatal_error=None,
    ):
        """Executa chamada com retry.

        Args:
            agent: Nome do agente alvo.
            call_fn: Callable(agent) → response str | None (chamada bruta).
            resolve_fn: Callable(agent, response) → result str | None (tool loop).
            is_user_cancelled: Callable() → bool (cancelamento do usuário).
            is_fatal_error: Callable(exc) → bool opcional. Quando verdadeiro, a
                exceção é registrada como falha sem retry — erros fatais não
                mudam com tentativas adicionais.
        """
        last_error = None

        effective_max_retries = max(1, self._max_retries)
        if isinstance(max_retries, int):
            effective_max_retries = max(1, max_retries)

        for attempt in range(1, effective_max_retries + 1):
            if is_user_cancelled():
                logger.debug(
                    "[AGENT_CALL] agent=%s cancelled by user before retry %d/%d, aborting",
                    agent, attempt, effective_max_retries,
                )
                return None

            try:
                response = call_fn(agent)
                if is_user_cancelled():
                    return None
                if response is None:
                    if is_user_cancelled():
                        logger.debug("[AGENT_CALL] agent=%s cancelled by user, aborting", agent)
                        return None
                    if self._gave_up_on_rate_limit(agent):
                        return None
                    if attempt < effective_max_retries:
                        backoff = self._compute_backoff(attempt)
                        self._before_retry(agent, attempt, "no_response")
                        self._notify_retry(
                            agent,
                            reason="no_response",
                            attempt=attempt,
                            limit=effective_max_retries,
                        )
                        logger.debug(
                            "agent=%s no response, retrying %d/%d",
                            agent, attempt, effective_max_retries,
                        )
                        if not self._wait_backoff(backoff, is_user_cancelled):
                            return None
                        continue
                    self._record_exhausted(agent)
                    return None

                result = resolve_fn(agent, response)
                if is_user_cancelled():
                    return None
                if result is None:
                    if is_user_cancelled():
                        logger.debug("[AGENT_CALL] agent=%s cancelled by user, aborting", agent)
                        return None
                    if attempt < effective_max_retries:
                        backoff = self._compute_backoff(attempt)
                        self._before_retry(agent, attempt, "resolve_failed")
                        self._notify_retry(
                            agent,
                            reason="invalid_response",
                            attempt=attempt,
                            limit=effective_max_retries,
                        )
                        logger.debug(
                            "agent=%s response parsing failed, retrying %d/%d",
                            agent, attempt, effective_max_retries,
                        )
                        if not self._wait_backoff(backoff, is_user_cancelled):
                            return None
                        continue
                    self._record_failure(agent)
                else:
                    self._record_success(agent)
                return result

            except Exception as exc:
                if is_user_cancelled():
                    logger.debug("[AGENT_CALL] agent=%s cancelled by user, aborting", agent)
                    return None
                if callable(is_fatal_error) and is_fatal_error(exc):
                    last_error = exc
                    self._record_failure(agent)
                    user_message = getattr(exc, "user_message", None) or (
                        "O provedor rejeitou a execução."
                    )
                    self._notify_error(
                        f"{agent}: erro fatal, sem nova tentativa. {user_message}"
                    )
                    logger.debug(
                        "agent=%s fatal error, aborting without retry: %s",
                        agent, exc,
                    )
                    return None
                last_error = exc
                if self._gave_up_on_rate_limit(agent):
                    return None
                if attempt < effective_max_retries:
                    self._before_retry(agent, attempt, "exception")
                    self._notify_retry(
                        agent,
                        reason="comm_error",
                        attempt=attempt,
                        limit=effective_max_retries,
                        detail=str(exc),
                    )
                    logger.debug(
                        "agent=%s error communicating, retrying %d/%d: %s",
                        agent, attempt, effective_max_retries, exc,
                    )
                    if not self._wait_backoff(self._compute_backoff(attempt), is_user_cancelled):
                        return None
                    continue
                self._record_exhausted(agent)
                raise

        if last_error:
            self._notify_error(
                f"{agent}: could not reach after all retries: {str(last_error)}"
            )
            logger.debug(
                "agent=%s could not reach after all retries: %s", agent, str(last_error)
            )
        return None

    def _record_exhausted(self, agent: str) -> None:
        """Tentativas esgotadas: rate limit suspende pelo prazo do provedor; o resto é falha."""
        if self._is_rate_limited():
            self._record_rate_limited(agent, self._get_retry_after())
        else:
            self._record_failure(agent)

    def _gave_up_on_rate_limit(self, agent: str) -> bool:
        """Rate limit longo demais para esperar: registra o agente como indisponível."""
        if not self._is_rate_limited() or self._compute_backoff(1) <= self._max_rate_limit_wait:
            return False
        retry_after = self._get_retry_after()
        logger.debug("agent=%s rate limited (retry_after=%s), not waiting", agent, retry_after)
        self._record_rate_limited(agent, retry_after)
        return True

    @staticmethod
    def _wait_backoff(seconds: float, is_user_cancelled) -> bool:
        """Keep cancellation responsive even during long provider backoffs."""
        remaining = max(0.0, seconds)
        while remaining > 0:
            if is_user_cancelled():
                return False
            interval = min(0.1, remaining)
            time.sleep(interval)
            remaining -= interval
        return not is_user_cancelled()

    def _compute_backoff(self, attempt: int) -> float:
        if self._is_rate_limited():
            retry_after = self._get_retry_after()
            if isinstance(retry_after, (int, float)) and math.isfinite(retry_after) and retry_after > 0:
                return max(self._rate_limit_backoff, float(retry_after))
            return self._rate_limit_backoff
        return self._retry_backoff * attempt

"""Execução única sem humano: ``quimera --agents A B --prompt "..."``.

O prompt vai ao primeiro candidato; se ele falhar (sem resposta, erro do
provedor, resposta vazia), vai ao próximo, na ordem dada. É o mesmo turno do
chat (``ChatRoundOrchestrator``): prompt de primeiro falante, histórico da
sessão e tools pelo MCP da sessão. Não há loop de input, banner nem resumo de
sessão no fim, e a sessão é gravada como ``headless`` (``quimera.storage``).

Só a resposta final interessa a quem chamou; o progresso vai para o renderer,
que a CLI monta mudo (só o audit de ``--debug`` o recebe).
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..runtime.errors import format_dispatch_failure
from ..ui.commands import USER_ROLE

_logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ALL_FAILED = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 124
EXIT_INTERRUPTED = 130
EXIT_TERMINATED = 143

#: Depois de cancelar a tentativa em curso (timeout, Ctrl+C), quanto esperar
#: o agente devolver o controle antes de encerrar mesmo assim.
_CANCEL_GRACE_SECONDS = 5.0


@dataclass
class HeadlessAttempt:
    """Uma tentativa de um candidato."""

    agent: str
    ok: bool
    error: str | None
    duration_ms: int


@dataclass
class HeadlessResult:
    """Desfecho da execução, no formato de ``--output json``."""

    ok: bool = False
    agent: str | None = None
    response: str | None = None
    attempts: list[HeadlessAttempt] = field(default_factory=list)
    session_id: str | None = None
    error: str | None = None
    exit_code: int = EXIT_ALL_FAILED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _Progress:
    """Estado compartilhado entre a thread do turno e a que espera por ele."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.attempts: list[HeadlessAttempt] = []
        self.current_agent: str | None = None
        self.current_started: float = 0.0
        self.agent: str | None = None
        self.response: str | None = None
        self.crash: BaseException | None = None
        self.stopped = threading.Event()


def _failure_message(app, agent: str) -> str:
    """Diagnóstico da última falha do client (exit code, stderr, timeout...)."""
    consume = getattr(app.dispatch_services, "consume_last_run_failure", None)
    receipt = consume() if callable(consume) else None
    return format_dispatch_failure(agent, receipt)


def _run_candidates(app, message: str, candidates: list[str], progress: _Progress) -> None:
    """Executa o turno: candidatos em ordem até o primeiro com resposta."""
    session_services = app.session_services
    app.session_state_mgr.advance_turn()
    history_snapshot = session_services.persist_message(
        USER_ROLE, message, return_history_snapshot=True
    )
    prompt_binding: dict[str, Any] = {"request_override": message}
    if isinstance(history_snapshot, list) and history_snapshot:
        prompt_binding["history_snapshot"] = history_snapshot
    renderer = app.renderer
    previous: str | None = None
    for index, agent in enumerate(candidates):
        with progress.lock:
            # Sob o lock: um timeout entre dois candidatos não deixa o
            # próximo começar sem que ninguém o cancele.
            if progress.stopped.is_set():
                return
            progress.current_agent = agent
            progress.current_started = time.monotonic()
        if previous is not None:
            renderer.notify_agent_failover(previous, target=agent)
        error: str | None = None
        response: str | None = None
        try:
            raw = app.dispatch_services.delegate(
                agent,
                is_first_speaker=True,
                primary=index == 0,
                protocol_mode="standard",
                **prompt_binding,
            )
            response, _, _, _ = app.parse_response(raw)
        except Exception as exc:
            _logger.debug("headless: %s falhou", agent, exc_info=True)
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        if error is None and response is None:
            error = _failure_message(app, agent)
        elif error is None and not str(response).strip():
            error = f"Agent '{agent}' returned an empty response"
            response = None
        with progress.lock:
            if progress.stopped.is_set():
                # Timeout/Ctrl+C: quem espera já registrou esta tentativa.
                return
            duration_ms = int((time.monotonic() - progress.current_started) * 1000)
            progress.current_agent = None
            progress.attempts.append(
                HeadlessAttempt(agent, error is None, error, duration_ms)
            )
            if error is None:
                progress.agent = agent
                progress.response = response
        if error is None:
            session_services.persist_message(agent, response)
            return
        renderer.show_warning(f"{agent}: {error}")
        previous = agent


def _cancel_active_work(app) -> None:
    """Cancela o agente em curso e os subprocessos dele."""
    cancel_active_work = getattr(app.agent_client, "cancel_active_work", None)
    if callable(cancel_active_work):
        cancel_active_work()
    process_supervisor = getattr(app, "process_supervisor", None)
    terminate_all = getattr(process_supervisor, "terminate_all", None)
    if callable(terminate_all):
        terminate_all()


def _stop(app, worker: threading.Thread, progress: _Progress, reason: str) -> bool:
    """Interrompe o turno e registra a tentativa em curso como falha.

    Uma resposta já aceita antes do corte vale: devolve False e só espera a
    thread terminar de gravá-la.
    """
    with progress.lock:
        if progress.response is not None:
            answered = True
        else:
            answered = False
            progress.stopped.set()
    if answered:
        worker.join(timeout=_CANCEL_GRACE_SECONDS)
        return False
    with progress.lock:
        agent = progress.current_agent
        if agent is not None:
            duration_ms = int((time.monotonic() - progress.current_started) * 1000)
            progress.attempts.append(HeadlessAttempt(agent, False, reason, duration_ms))
            progress.current_agent = None
    _cancel_active_work(app)
    worker.join(timeout=_CANCEL_GRACE_SECONDS)
    if worker.is_alive():
        _logger.warning("headless: o agente não devolveu o controle após o cancelamento")
    return True


class _Terminated(Exception):
    """SIGTERM recebido: encerra como o Ctrl+C, fechando o app."""


def _install_sigterm_handler() -> tuple[bool, Any]:
    """Converte SIGTERM em exceção na thread principal.

    Devolve (instalado, handler anterior); fora da thread principal o Python
    não aceita handlers e nada muda.
    """
    if threading.current_thread() is not threading.main_thread():
        return False, None

    def _raise(_signum, _frame):
        raise _Terminated

    return True, signal.signal(signal.SIGTERM, _raise)


def run_headless_prompt(
    app,
    message: str,
    candidates: list[str],
    *,
    timeout: float | None = None,
) -> HeadlessResult:
    """Executa ``message`` nos ``candidates`` em ordem e fecha o app.

    ``timeout`` (segundos) limita o turno, somando as tentativas; a montagem
    do app e o encerramento ficam fora. Sempre fecha o app, sem resumo de
    sessão; o histórico fica gravado.
    """
    if not candidates:
        raise ValueError("nenhum agente candidato")
    progress = _Progress()

    def _target() -> None:
        try:
            _run_candidates(app, message, candidates, progress)
        except BaseException as exc:  # noqa: BLE001 - relatado pela thread que espera
            progress.crash = exc

    worker = threading.Thread(target=_target, name="quimera-headless", daemon=True)
    result = HeadlessResult(session_id=getattr(app.storage, "session_id", None))
    interrupted = False
    sigterm_installed, previous_sigterm = _install_sigterm_handler()

    def _interrupt(reason: str, exit_code: int) -> None:
        nonlocal interrupted
        interrupted = True
        try:
            stopped = _stop(app, worker, progress, reason)
        except (KeyboardInterrupt, _Terminated):
            # Segundo sinal enquanto o agente é cancelado: encerra sem esperar.
            stopped = True
        if stopped:
            result.error = reason
            result.exit_code = exit_code

    try:
        try:
            worker.start()
            worker.join(timeout=timeout)
            if worker.is_alive():
                reason = f"tempo limite de {timeout:g}s esgotado"
                if _stop(app, worker, progress, reason):
                    result.error = reason
                    result.exit_code = EXIT_TIMEOUT
        except KeyboardInterrupt:
            _interrupt("interrompido (Ctrl+C)", EXIT_INTERRUPTED)
        except _Terminated:
            _interrupt("encerrado (SIGTERM)", EXIT_TERMINATED)
        finally:
            try:
                app.close(interrupted=interrupted, summarize_session=False)
            except (KeyboardInterrupt, _Terminated):
                # Segundo sinal durante o encerramento: não vira traceback.
                _logger.warning("headless: encerramento interrompido")
                if result.error is None:
                    result.error = "interrompido durante o encerramento"
                    result.exit_code = EXIT_INTERRUPTED
    finally:
        if sigterm_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)

    with progress.lock:
        result.attempts = list(progress.attempts)
        if result.error is None:
            if progress.crash is not None:
                crash = progress.crash
                result.error = f"{type(crash).__name__}: {crash}"
            elif progress.response is not None:
                result.ok = True
                result.agent = progress.agent
                result.response = progress.response
                result.exit_code = EXIT_OK
            else:
                result.error = "nenhum agente candidato respondeu"
    return result

"""Broker centralizado para input humano: approval + ask_user.

Serializa todas as perguntas interativas em uma única fila FIFO,
processa uma por vez e aplica timeout com auto-resposta segura
quando o usuário não está disponível.

Cenários tratados:
- Usuário presente: fluxo interativo normal (setas/números/y/n).
- Usuário ausente (timeout): auto-nega approval; auto-seleciona
  primeira opção em ask_user; emite notificação visível.
- Múltiplas perguntas enfileiradas: exibe contagem ao processar cada item.
- Múltiplos agentes e background tasks: mesmo caminho para todos,
  elimina colisões de termios/raw-mode.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Literal

from ..domain.agent_run_events import AgentRunEvent, coerce_agent_run_sink
from .approval import format_approval_question

_logger = logging.getLogger(__name__)

_UNSET = object()

_DEFAULT_APPROVAL_TIMEOUT = 180.0   # 3 min
_DEFAULT_ASK_TIMEOUT = 180.0        # 3 min


@dataclass
class _InputRequest:
    kind: Literal["approval", "ask_user"]
    source: str
    question: str
    options: list[str]
    timeout: float
    default: Any
    on_approve_all: Callable[[], None] | None = None
    cancel_event: Any = None
    #: Reserva de stdin no gate sequencial (pipe); None sem reserva.
    reservation: Any = None
    _result: list[Any] = field(default_factory=lambda: [_UNSET], init=False)
    _done: threading.Event = field(default_factory=threading.Event, init=False)
    _expired: threading.Event = field(default_factory=threading.Event, init=False)
    _claimed: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    #: Após expirar sem linha reclamada, quanto esperar a leitura em curso
    #: abortar antes de resolver pelo default (a leitura direta do gate
    #: sequencial aborta na hora; outros gates têm timeout próprio).
    _EXPIRY_GRACE_SECONDS = 1.0
    #: Com linha já reclamada a resposta é dela: quanto esperar o consumidor
    #: concluir (eco, parse) antes de desistir e resolver pelo default.
    _CLAIMED_WAIT_SECONDS = 30.0

    def set_result(self, value: Any) -> bool:
        """Conclui o pedido; só a primeira conclusão vale. Libera a reserva."""
        with self._lock:
            if self._done.is_set():
                return False
            self._result[0] = value
            self._done.set()
        self._release_reservation()
        return True

    def _release_reservation(self) -> None:
        reservation = self.reservation
        if reservation is None:
            return
        self.reservation = None
        try:
            reservation.release()
        except Exception as exc:
            _logger.warning("liberar a reserva de stdin falhou: %s", exc)
            return

    def try_expire(self) -> bool:
        """Expira o pedido e acorda a leitura em curso; False se uma linha já é dele.

        A decisão é atômica com ``claim``: depois de reclamada uma linha o
        pedido não expira mais, e a resposta lida é a que vale.
        """
        with self._lock:
            if self._done.is_set() or self._claimed:
                return False
            self._expired.set()
        wake = getattr(self.reservation, "wake", None)
        if callable(wake):
            try:
                wake()
            except Exception as exc:
                _logger.warning("acordar a leitura direta após expiração falhou: %s", exc)
                return True
        return True

    def claim(self) -> bool:
        """Reclama a próxima linha para este pedido (chamado pelo gate sob o lock dele)."""
        with self._lock:
            if self._done.is_set() or self._expired.is_set() or self.is_cancelled():
                return False
            self._claimed = True
            return True

    def should_abort(self) -> bool:
        """True quando a leitura em curso não deve mais consumir linha."""
        return self._done.is_set() or self._expired.is_set() or self.is_cancelled()

    def is_cancelled(self) -> bool:
        """True quando a execução que originou o pedido já foi cancelada."""
        event = self.cancel_event
        if event is None:
            return False
        is_set = getattr(event, "is_set", None)
        if not callable(is_set):
            return False
        try:
            return bool(is_set())
        except Exception as exc:
            _logger.debug("is_cancelled: is_set falhou: %s", exc)
            return False

    def wait(self) -> Any:
        deadline = time.monotonic() + self.timeout
        while not self._done.wait(0.1):
            if self.is_cancelled():
                self.set_result(self.default)
                break
            if time.monotonic() >= deadline:
                if self.try_expire():
                    # Expirou sem linha reclamada: a leitura em curso aborta
                    # sem consumir; passada a graça, vale o default.
                    if not self._done.wait(self._EXPIRY_GRACE_SECONDS):
                        self.set_result(self.default)
                elif not self._done.wait(self._CLAIMED_WAIT_SECONDS):
                    # Linha reclamada no limite: a resposta é dela; só um
                    # consumidor travado por muito tempo cai no default.
                    self.set_result(self.default)
                break
        return self._result[0]

    def is_done(self) -> bool:
        return self._done.is_set()


class InputBroker:
    """Broker que serializa todos os prompts interativos em uma fila única.

    Crie uma instância por app e passe para ApprovalManager e para
    ToolExecutor.set_ask_user_fn.
    """

    def __init__(self, renderer=None, input_gate=None, agent_run_sink=None) -> None:
        self._renderer = renderer
        self._input_gate = input_gate
        self._agent_run_sink = coerce_agent_run_sink(agent_run_sink)
        self._suspend_spinner_fn = None
        self._resume_spinner_fn = None
        self._queue: queue.Queue[_InputRequest] = queue.Queue()
        self._consumer = threading.Thread(
            target=self._consumer_loop, daemon=True, name="input-broker"
        )
        self._consumer.start()

    def set_renderer(self, renderer) -> None:
        """Define o renderer de terminal para exibição de prompts."""
        self._renderer = renderer

    def set_input_gate(self, gate) -> None:
        """Define o InputGate para leitura segura de input em terminal interativo."""
        self._input_gate = gate

    def set_spinner_callbacks(self, suspend_spinner_fn, resume_spinner_fn) -> None:
        """Define callbacks para pausar/retomar loading externo durante input."""
        self._suspend_spinner_fn = suspend_spinner_fn
        self._resume_spinner_fn = resume_spinner_fn

    def _container_for(self, agent: str):
        """Controller de janela do agente, quando o renderer o expõe.

        Retorna (controller, renderer) ou (None, renderer). O controller é dono
        dos efeitos colaterais de input do agente: emoldura a pergunta sob o
        banner, limpa o transient daquele agente e faz flush antes de ceder o
        chão ao prompt.
        """
        renderer = self._renderer
        if renderer is not None:
            try:
                return renderer.agent_window_controller(agent), renderer
            except Exception as exc:
                _logger.debug("agent_window_controller indisponível no renderer: %s", exc)
        return None, renderer

    # ------------------------------------------------------------------
    # Public API chamada pelos produtores (approval, ask_user)
    # ------------------------------------------------------------------

    def request_approval(
        self,
        tool_name: str,
        summary: str,
        *,
        source: str = "agente",
        timeout: float | None = None,
        on_approve_all: Callable[[], None] | None = None,
        cancel_event: Any = None,
    ) -> bool:
        """Enfileira pedido de aprovação e bloqueia até resposta ou timeout.

        ``cancel_event`` (opcional) vincula o pedido ao ciclo de vida da
        execução que o originou: se o evento for sinalizado antes da resposta,
        o pedido é negado automaticamente sem ser exibido ao usuário. Isso
        evita prompts órfãos de execuções canceladas/finalizadas (ex.: steps
        de delegate paralelo cancelados por timeout) que apareceriam depois e,
        mesmo aprovados, falhariam em sequência.
        """
        if timeout is None:
            timeout = _DEFAULT_APPROVAL_TIMEOUT
        req = _InputRequest(
            kind="approval",
            source=source,
            question=format_approval_question(tool_name, summary),
            options=[],
            timeout=timeout,
            default=False,
            on_approve_all=on_approve_all,
            cancel_event=cancel_event,
        )
        if req.is_cancelled():
            return False
        self._enqueue(req)
        return bool(req.wait())

    def request_ask_user(
        self,
        question: str,
        options: list[str],
        *,
        source: str = "agente",
        timeout: float | None = None,
    ) -> tuple[int, str]:
        """Enfileira pergunta e bloqueia até resposta ou timeout.

        Sem ``options`` -> texto livre (retorna (-1, texto)). Com opções ->
        seleção/enquete (retorna (índice_0based, texto_da_opção)).
        """
        if timeout is None:
            timeout = _DEFAULT_ASK_TIMEOUT
        # Sem opções não há resposta segura para auto-preencher: devolve vazio.
        default_val: tuple[int, str] = (0, options[0]) if options else (-1, "")
        req = _InputRequest(
            kind="ask_user",
            source=source,
            question=question,
            options=list(options),
            timeout=timeout,
            default=default_val,
        )
        self._enqueue(req)
        return req.wait()  # type: ignore[return-value]

    def _enqueue(self, req: _InputRequest) -> None:
        """Publica o pedido; no gate sequencial (pipe) reserva o stdin antes.

        A reserva precede a fila e a renderização da pergunta: a linha
        seguinte do stdin já pertence ao pedido antes de o chat ocioso poder
        consumi-la. ``set_result`` (resposta, cancelamento, timeout) libera.
        """
        reserve = getattr(self._input_gate, "reserve_direct_input", None)
        if callable(reserve) and getattr(self._input_gate, "sequential", False) is True:
            req.reservation = reserve()
        self._queue.put(req)

    # ------------------------------------------------------------------
    # Consumidor — thread dedicada, serializa tudo
    # ------------------------------------------------------------------

    def _consumer_loop(self) -> None:
        while True:
            req = self._queue.get()
            if req.is_done():
                continue
            if req.is_cancelled():
                self._resolve_cancelled(req)
                continue
            if not self._consumer_can_handle(req):
                # Sem prompt_toolkit ativo, a thread do broker não deve negar nem
                # chamar InputGate(prompt). Devolve para a fila para a main thread
                # processar via process_pending_once() enquanto aguarda o driver.
                self._queue.put(req)
                time.sleep(0.05)
                continue
            self._process_request(req, allow_direct_gate=False)

    def _consumer_can_handle(self, req: _InputRequest) -> bool:
        gate = self._input_gate
        if gate is None:
            return True
        if getattr(gate, "sequential", False) is True:
            # Gate sequencial (pipe): as leituras diretas são thread-safe e
            # não disputam um prompt; a thread do broker atende na hora, sem
            # depender da main thread drenar a fila (com o chat ocioso ela
            # não drena, e um pedido de produtor em background expiraria).
            return True
        is_active = getattr(gate, "is_active", None)
        return bool(callable(is_active) and is_active())

    def process_pending_once(self) -> bool:
        """Processa uma pergunta pendente na thread chamadora.

        Deve ser chamado pela main thread enquanto ela aguarda um driver em
        background. Nesse modo é seguro usar InputGate(prompt), porque estamos
        no fluxo principal do shell, não na thread consumidora do broker.
        """
        if getattr(self._input_gate, "sequential", False) is True:
            # Gate sequencial: a thread do broker atende em ordem; um pump
            # paralelo tiraria um segundo pedido e disputaria a linha seguinte.
            return False
        try:
            req = self._queue.get_nowait()
        except queue.Empty:
            return False
        if req.is_done():
            return False
        if req.is_cancelled():
            self._resolve_cancelled(req)
            return False
        self._process_request(req, allow_direct_gate=True)
        return True

    def _resolve_cancelled(self, req: _InputRequest) -> None:
        """Resolve um pedido cuja execução foi cancelada, sem exibir prompt.

        Approval é negado (default False); ask_user segue a resposta segura
        padrão. O usuário não é interrompido por prompts de execuções mortas.
        """
        req.set_result(req.default)
        self._agent_run_sink.emit(
            AgentRunEvent(
                "human_action_cancelled",
                req.source,
                text=req.question,
                metadata={"kind": req.kind, "reason": "run_cancelled"},
            )
        )

    def _process_request(self, req: _InputRequest, *, allow_direct_gate: bool) -> None:
        if req.is_cancelled():
            self._resolve_cancelled(req)
            return
        pending = self._queue.qsize()
        if pending > 0:
            self._emit(f"\n  [{pending} pergunta(s) aguardando na fila]")
        self._agent_run_sink.emit(
            AgentRunEvent(
                "human_action_requested",
                req.source,
                text=req.question,
                metadata={
                    "kind": req.kind,
                    "options": list(req.options),
                    "timeout": req.timeout,
                    "pending": pending,
                },
            )
        )
        try:
            if req.kind == "approval":
                result = self._handle_approval(req, allow_direct_gate=allow_direct_gate)
            else:
                result = self._handle_ask_user(req, allow_direct_gate=allow_direct_gate)
            req.set_result(result)
            self._agent_run_sink.emit(
                AgentRunEvent(
                    "human_action_answered",
                    req.source,
                    text=str(result),
                    metadata={
                        "kind": req.kind,
                        "result": result,
                    },
                )
            )
        except Exception as exc:
            req.set_result(req.default)
            self._agent_run_sink.emit(
                AgentRunEvent(
                    "human_action_failed",
                    req.source,
                    text=str(exc),
                    metadata={"kind": req.kind},
                )
            )
            self._emit(f"  [broker de input: erro inesperado: {exc}]")

    def _handle_approval(self, req: _InputRequest, *, allow_direct_gate: bool = False) -> bool:
        start = time.monotonic()
        deadline = start + req.timeout
        prompt = "  Executar? [y/N/a=todas]: "

        # Quando pt está ativo: questão + input num único run_in_terminal para
        # evitar que o pt reexiba o CLI entre a exibição da pergunta e a leitura
        # (que apagaria a pergunta antes do usuário responder).
        gate = self._input_gate
        if gate is not None:
            is_active = getattr(gate, "is_active", None)
            if callable(is_active) and is_active():
                read_fn = getattr(gate, "read_approval_in_terminal", None)
                if callable(read_fn):
                    remaining = max(0.5, deadline - time.monotonic())
                    container, renderer = self._container_for(req.source)
                    if container is not None:
                        answer = container.ask_approval(
                            renderer, gate, req.question, prompt, timeout=remaining
                        )
                    else:
                        answer = read_fn(
                            req.question, prompt, timeout=remaining, ticket=req
                        )
                    if answer is None:
                        elapsed = time.monotonic() - start
                        self._emit(
                            f"  [sem resposta em {elapsed:.0f}s — negado automaticamente]"
                            f" ({req.source})"
                        )
                        return False
                    ans = answer.strip().lower()
                    if ans in {"a", "all", "todas"}:
                        if req.on_approve_all is not None:
                            try:
                                req.on_approve_all()
                            except Exception as exc:
                                _logger.warning("on_approve_all falhou: %s", exc, exc_info=True)
                        return True
                    return ans in {"y", "yes", "s", "sim"}

        # Sem pt ativo, não disputamos stdin diretamente. A leitura falha de
        # forma segura e o pedido segue pelo default/timeout.
        with self._approval_terminal_window(
            owner=req.source,
            metadata={"question": req.question, "owner": req.source},
        ):
            self._emit(req.question)
            answer = self._read_line(
                prompt, deadline=deadline, allow_direct_gate=allow_direct_gate,
                ticket=req,
            )
        if answer is None:
            elapsed = time.monotonic() - start
            self._emit(
                f"  [sem resposta em {elapsed:.0f}s — negado automaticamente]"
                f" ({req.source})"
            )
            return False
        key = answer.strip().lower()
        if key in {"a", "all", "todas"}:
            if req.on_approve_all is not None:
                try:
                    req.on_approve_all()
                except Exception as exc:
                    _logger.warning("on_approve_all falhou: %s", exc, exc_info=True)
            return True
        return key in {"y", "yes", "s", "sim"}

    def _handle_ask_user(
        self, req: _InputRequest, *, allow_direct_gate: bool = False
    ) -> tuple[int, str]:
        start = time.monotonic()
        deadline = start + req.timeout
        if req.options:
            result = self._read_selection(
                req.question, req.options, deadline=deadline, agent=req.source,
                allow_direct_gate=allow_direct_gate, ticket=req,
            )
        else:
            result = self._read_free_text(
                req.question, deadline=deadline, agent=req.source,
                allow_direct_gate=allow_direct_gate, ticket=req,
            )
        if result is None:
            idx, val = req.default
            elapsed = time.monotonic() - start
            if req.options:
                self._emit(
                    f"\n  [sem resposta em {elapsed:.0f}s —"
                    f" selecionado automaticamente: '{val}'] ({req.source})"
                )
            else:
                self._emit(
                    f"\n  [sem resposta em {elapsed:.0f}s —"
                    f" seguindo sem resposta] ({req.source})"
                )
            return idx, val
        return result

    def _read_free_text(
        self,
        question: str,
        *,
        deadline: float,
        agent: str = "agente",
        allow_direct_gate: bool = False,
        ticket=None,
    ) -> tuple[int, str] | None:
        """Lê uma resposta em texto livre com deadline. Retorna (-1, texto)."""
        remaining_s = max(0, int(deadline - time.monotonic()))
        # Via container: a pergunta vai embutida no prompt e só aparece dentro
        # da janela explícita de input, sem ser emitida no feed antes.
        gate = self._input_gate
        is_active = getattr(gate, "is_active", None)
        if gate is not None and callable(is_active) and is_active():
            container, renderer = self._container_for(agent)
            if container is not None:
                remaining = max(0.5, deadline - time.monotonic())
                prompt = f"{question}\n  Resposta (auto em {remaining_s}s): "
                answer = container.ask_input(renderer, gate, prompt, timeout=remaining)
                if answer is None:
                    return None
                return -1, answer.rstrip("\n\r")
        # Sem pt ativo: emite a pergunta sob janela explícita de input.
        with self._input_terminal_window(
            owner=agent,
            metadata={"question": question, "owner": agent},
        ):
            self._emit(f"\n{question}")
            answer = self._read_line(
                f"  Resposta (auto em {remaining_s}s): ", deadline=deadline,
                allow_direct_gate=allow_direct_gate, ticket=ticket,
            )
        if answer is None:
            return None
        return -1, answer.rstrip("\n\r")

    # ------------------------------------------------------------------
    # Primitivos de leitura com deadline
    # ------------------------------------------------------------------

    def _read_line(
        self, prompt: str, *, deadline: float, allow_direct_gate: bool = False, ticket=None
    ) -> str | None:
        """Lê uma linha somente pelo InputGate seguro.

        Não faça fallback para leitura direta de stdin aqui. O broker pode
        rodar em thread de background enquanto o prompt principal pertence ao
        ``prompt_toolkit``; disputar stdin diretamente nessa condição trava o
        shell inteiro. Usa apenas o caminho ``run_in_terminal`` do gate;
        chamar ``InputGate(prompt)`` daqui bloquearia porque o broker roda em
        thread dedicada, fora do loop principal do prompt.
        """
        gate = self._input_gate
        if gate is None:
            return None
        read_fn = getattr(gate, "read_input_in_terminal", None)
        remaining = max(0.5, deadline - time.monotonic())
        if callable(read_fn):
            answer = read_fn(prompt, timeout=remaining, ticket=ticket)
            if answer is not None:
                return answer
        if not allow_direct_gate or deadline - time.monotonic() <= 0:
            return None
        plain_read = getattr(gate, "read_plain_input", None)
        try:
            if callable(plain_read):
                return plain_read(prompt)
            if callable(gate):
                return gate(prompt)
        except (EOFError, KeyboardInterrupt, Exception):
            return None
        return None

    def _read_selection(
        self,
        question: str,
        options: list[str],
        *,
        deadline: float,
        agent: str = "agente",
        allow_direct_gate: bool = False,
        ticket=None,
    ) -> tuple[int, str] | None:
        """Seleção interativa com deadline. Usa input_gate se pt ativo."""
        gate = self._input_gate
        if gate is not None:
            remaining = max(0.5, deadline - time.monotonic())
            read_fn = getattr(gate, "read_selection_in_terminal", None)
            if callable(read_fn):
                container, renderer = self._container_for(agent)
                if container is not None:
                    result = container.ask_selection(
                        renderer, gate, question, options, timeout=remaining
                    )
                    if result is not None:
                        return result
                else:
                    result = read_fn(question, options, timeout=remaining, ticket=ticket)
                    if result is not None:
                        return result
        return self._line_select(
            question, options, deadline=deadline, agent=agent,
            allow_direct_gate=allow_direct_gate, ticket=ticket,
        )

    def _line_select(
        self,
        question: str,
        options: list[str],
        *,
        deadline: float,
        agent: str = "agente",
        allow_direct_gate: bool = False,
        ticket=None,
    ) -> tuple[int, str] | None:
        """Seleção numerada usando apenas _read_line/InputGate."""
        if self._input_gate is None:
            return None
        remaining_s = max(0, int(deadline - time.monotonic()))
        lines = [question]
        for i, opt in enumerate(options):
            lines.append(f"  {i + 1}. {opt}")
        lines.append(f"  (1-{len(options)} · auto em {remaining_s}s)")
        with self._selection_terminal_window(
            owner=agent,
            metadata={"question": question, "owner": agent, "options": list(options)},
        ):
            self._emit("\n".join(lines))
            while True:
                if deadline - time.monotonic() <= 0:
                    return None
                prompt = f"  Selecione (1-{len(options)}): "
                answer = self._read_line(
                    prompt, deadline=deadline, allow_direct_gate=allow_direct_gate,
                    ticket=ticket,
                )
                if answer is None:
                    return None
                stripped = answer.strip()
                if stripped.isdigit():
                    num = int(stripped) - 1
                    if 0 <= num < len(options):
                        return num, options[num]
                self._emit(f"  [{stripped!r} inválido — esperado 1-{len(options)}]")

    # ------------------------------------------------------------------
    # Utilitário de saída
    # ------------------------------------------------------------------

    @contextmanager
    def _with_interactive_terminal_window(self, window_factory: Callable[[], Any]):
        """Suspende o spinner dentro de uma janela interativa explícita.

        O renderer é dono do terminal (a TUI pelo driver Textual; em pipe
        não há terminal): nada aqui escreve escapes em stdout.
        """
        suspend_spinner = self._suspend_spinner_fn
        if callable(suspend_spinner):
            try:
                suspend_spinner()
            except Exception as exc:
                _logger.debug("suspender o spinner falhou: %s", exc)
        try:
            with window_factory():
                yield
        finally:
            resume_spinner = self._resume_spinner_fn
            if callable(resume_spinner):
                try:
                    resume_spinner()
                except Exception as exc:
                    _logger.debug("retomar o spinner falhou: %s", exc)

    def _approval_terminal_window(
        self,
        *,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        """Return an explicit approval window context preserving spinner callbacks."""
        renderer = self._renderer
        if renderer is None:
            return self._with_interactive_terminal_window(lambda: nullcontext())
        return self._with_interactive_terminal_window(
            lambda: renderer.approval_window(owner=owner, metadata=metadata or {})
        )

    def _input_terminal_window(
        self,
        *,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        """Return an explicit input window context preserving spinner callbacks."""
        renderer = self._renderer
        if renderer is None:
            return self._with_interactive_terminal_window(lambda: nullcontext())
        return self._with_interactive_terminal_window(
            lambda: renderer.input_window(owner=owner, metadata=metadata or {})
        )

    def _selection_terminal_window(
        self,
        *,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        """Return a selection window context preserving spinner callbacks."""
        renderer = self._renderer
        if renderer is None:
            return self._with_interactive_terminal_window(lambda: nullcontext())
        return self._with_interactive_terminal_window(
            lambda: renderer.selection_window(owner=owner, metadata=metadata or {})
        )

    def _emit(self, message: str) -> None:
        """Emite mensagem ao usuário mantendo cursor tracking do Rich correto.

        Quando pt está ativo: usa run_in_terminal_message para exibir acima
        do prompt sem corromper o layout do prompt_toolkit.
        Quando pt não está ativo: usa renderer.print_direct() para que o Rich
        rastreie a posição do cursor — evita que o Live reinicie sobrescrevendo
        o texto da pergunta quando o agente retoma.
        """
        gate = self._input_gate
        if gate is not None:
            is_active_fn = getattr(gate, "is_active", None)
            if callable(is_active_fn) and is_active_fn():
                run_above = getattr(gate, "run_in_terminal_message", None)
                if callable(run_above) and run_above(lambda: print(message)):
                    return
        renderer = self._renderer
        if renderer is not None:
            renderer.print_direct(message)
        else:
            print(message, flush=True)

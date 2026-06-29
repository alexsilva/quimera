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

import queue
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Literal

from ..app.agent_run_events import AgentRunEvent, coerce_agent_run_sink
from .approval import format_approval_question

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
    _result: list[Any] = field(default_factory=lambda: [_UNSET], init=False)
    _done: threading.Event = field(default_factory=threading.Event, init=False)

    def set_result(self, value: Any) -> None:
        self._result[0] = value
        self._done.set()

    def wait(self) -> Any:
        self._done.wait(self.timeout)
        if not self._done.is_set():
            self.set_result(self.default)
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
        self._renderer = renderer

    def set_input_gate(self, gate) -> None:
        self._input_gate = gate

    def set_spinner_callbacks(self, suspend_spinner_fn, resume_spinner_fn) -> None:
        """Define callbacks para pausar/retomar loading externo durante input."""
        self._suspend_spinner_fn = suspend_spinner_fn
        self._resume_spinner_fn = resume_spinner_fn

    def set_qapp(self, qapp) -> None:
        """Registra QuimeraApplication para overlay de approval/ask_user no modo split."""
        self._qapp = qapp

    def _container_for(self, agent: str):
        """Controller de janela do agente, quando o renderer o expõe.

        Retorna (controller, renderer) ou (None, renderer). O controller é dono
        dos efeitos colaterais de input do agente: emoldura a pergunta sob o
        banner, limpa o transient daquele agente e faz flush antes de ceder o
        chão ao prompt.
        """
        renderer = self._renderer
        get_controller = getattr(renderer, "_agent_window_controller", None)
        if callable(get_controller):
            try:
                return get_controller(agent), renderer
            except Exception:
                pass
        get = getattr(renderer, "_container", None)
        if callable(get):
            try:
                return get(agent), renderer
            except Exception:
                pass
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
    ) -> bool:
        """Enfileira pedido de aprovação e bloqueia até resposta ou timeout."""
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
        )
        self._queue.put(req)
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
        self._queue.put(req)
        return req.wait()  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Consumidor — thread dedicada, serializa tudo
    # ------------------------------------------------------------------

    def _consumer_loop(self) -> None:
        while True:
            req = self._queue.get()
            if req.is_done():
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
        if getattr(self, "_qapp", None) is not None:
            return True
        gate = self._input_gate
        if gate is None:
            return True
        is_active = getattr(gate, "is_active", None)
        return bool(callable(is_active) and is_active())

    def process_pending_once(self) -> bool:
        """Processa uma pergunta pendente na thread chamadora.

        Deve ser chamado pela main thread enquanto ela aguarda um driver em
        background. Nesse modo é seguro usar InputGate(prompt), porque estamos
        no fluxo principal do shell, não na thread consumidora do broker.
        """
        try:
            req = self._queue.get_nowait()
        except queue.Empty:
            return False
        if req.is_done():
            return False
        self._process_request(req, allow_direct_gate=True)
        return True

    def _process_request(self, req: _InputRequest, *, allow_direct_gate: bool) -> None:
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

        # Modo split-UI: delega ao overlay do QuimeraApplication.
        qapp = getattr(self, "_qapp", None)
        if qapp is not None:
            remaining = max(0.5, deadline - time.monotonic())
            answer = qapp.request_approval(req.question, timeout=remaining)
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
                    except Exception:
                        pass
                return True
            return ans in {"y", "yes", "s", "sim"}

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
                        answer = read_fn(req.question, prompt, timeout=remaining)
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
                            except Exception:
                                pass
                        return True
                    return ans in {"y", "yes", "s", "sim"}

        # Sem pt ativo, não disputamos stdin diretamente. A leitura falha de
        # forma segura e o pedido segue pelo default/timeout.
        with self._approval_terminal_window(
            owner=req.source,
            metadata={"question": req.question, "owner": req.source},
        ):
            self._emit(req.question)
            answer = self._read_line(prompt, deadline=deadline, allow_direct_gate=allow_direct_gate)
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
                except Exception:
                    pass
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
                allow_direct_gate=allow_direct_gate,
            )
        else:
            result = self._read_free_text(
                req.question, deadline=deadline, agent=req.source,
                allow_direct_gate=allow_direct_gate,
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
    ) -> tuple[int, str] | None:
        """Lê uma resposta em texto livre com deadline. Retorna (-1, texto)."""
        remaining_s = max(0, int(deadline - time.monotonic()))

        # Modo split-UI: overlay do QuimeraApplication.
        qapp = getattr(self, "_qapp", None)
        if qapp is not None:
            remaining = max(0.5, deadline - time.monotonic())
            answer = qapp.request_ask_user(question, options=None, timeout=remaining)
            if answer is None:
                return None
            return -1, answer.rstrip("\n\r")

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
                allow_direct_gate=allow_direct_gate,
            )
        if answer is None:
            return None
        return -1, answer.rstrip("\n\r")

    # ------------------------------------------------------------------
    # Primitivos de leitura com deadline
    # ------------------------------------------------------------------

    def _read_line(
        self, prompt: str, *, deadline: float, allow_direct_gate: bool = False
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
            answer = read_fn(prompt, timeout=remaining)
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
    ) -> tuple[int, str] | None:
        """Seleção interativa com deadline. Usa input_gate se pt ativo."""
        # Modo split-UI: overlay com opções numeradas.
        qapp = getattr(self, "_qapp", None)
        if qapp is not None:
            remaining = max(0.5, deadline - time.monotonic())
            answer = qapp.request_ask_user(question, options=options, timeout=remaining)
            if answer is None:
                return None
            stripped = answer.strip()
            if stripped.isdigit():
                num = int(stripped) - 1
                if 0 <= num < len(options):
                    return num, options[num]
            # Texto completo — tenta corresponder a uma opção
            for i, opt in enumerate(options):
                if stripped.lower() == opt.lower():
                    return i, opt
            return None

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
                    result = read_fn(question, options, timeout=remaining)
                    if result is not None:
                        return result
        return self._line_select(
            question, options, deadline=deadline, agent=agent,
            allow_direct_gate=allow_direct_gate,
        )

    def _line_select(
        self,
        question: str,
        options: list[str],
        *,
        deadline: float,
        agent: str = "agente",
        allow_direct_gate: bool = False,
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
            metadata={"question": question, "owner": agent},
        ):
            self._emit("\n".join(lines))
            while True:
                if deadline - time.monotonic() <= 0:
                    return None
                prompt = f"  Selecione (1-{len(options)}): "
                answer = self._read_line(
                    prompt, deadline=deadline, allow_direct_gate=allow_direct_gate
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
        """Suspende Rich Live dentro de uma janela interativa explícita.

        Para o Live display e mostra o cursor, evitando que o refresh
        do Live sobrescreva o texto da pergunta ou que o cursor oculto
        impeça o usuário de ver onde digitar.
        """
        suspend_spinner = self._suspend_spinner_fn
        if callable(suspend_spinner):
            try:
                suspend_spinner()
            except Exception:
                pass
        try:
            with window_factory():
                print("\033[?25h", end="", flush=True)
                yield
        finally:
            resume_spinner = self._resume_spinner_fn
            if callable(resume_spinner):
                try:
                    resume_spinner()
                except Exception:
                    pass

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
        Quando pt não está ativo: usa renderer._console.print() para que o Rich
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
        console = getattr(renderer, "_console", None) if renderer is not None else None
        if console is not None:
            console.print(message, markup=False, highlight=False)
        else:
            print(message, flush=True)

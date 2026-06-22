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
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

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
        self._done.wait()
        return self._result[0]


class InputBroker:
    """Broker que serializa todos os prompts interativos em uma fila única.

    Crie uma instância por app e passe para ConsoleApprovalHandler e para
    ToolExecutor.set_ask_user_fn.
    """

    def __init__(self, renderer=None, input_gate=None) -> None:
        self._renderer = renderer
        self._input_gate = input_gate
        self._queue: queue.Queue[_InputRequest] = queue.Queue()
        self._consumer = threading.Thread(
            target=self._consumer_loop, daemon=True, name="input-broker"
        )
        self._consumer.start()

    def set_renderer(self, renderer) -> None:
        self._renderer = renderer

    def set_input_gate(self, gate) -> None:
        self._input_gate = gate

    def _container_for(self, agent: str):
        """Container (janela) do agente, quando o renderer o expõe.

        Retorna (container, renderer) ou (None, renderer). O container é o dono
        do output/perguntas do agente: emoldura a pergunta sob o banner, limpa o
        transient daquele agente e faz flush antes de ceder o chão ao prompt.
        """
        renderer = self._renderer
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
            question=f"\nAprovar {tool_name}\n{summary}",
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
            pending = self._queue.qsize()
            if pending > 0:
                self._emit(f"\n  [{pending} pergunta(s) aguardando na fila]")
            try:
                if req.kind == "approval":
                    result = self._handle_approval(req)
                else:
                    result = self._handle_ask_user(req)
                req.set_result(result)
            except Exception as exc:
                req.set_result(req.default)
                self._emit(f"  [broker de input: erro inesperado: {exc}]")

    def _handle_approval(self, req: _InputRequest) -> bool:
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

        # Sem pt ativo: leitura por linha (cooked mode, sem termios). Mesmo
        # input usado para escrever no chat — o usuário digita y/n/a + Enter.
        self._emit(req.question)
        answer = self._read_line(prompt, deadline=deadline)
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

    def _handle_ask_user(self, req: _InputRequest) -> tuple[int, str]:
        start = time.monotonic()
        deadline = start + req.timeout
        if req.options:
            result = self._read_selection(
                req.question, req.options, deadline=deadline, agent=req.source
            )
        else:
            result = self._read_free_text(
                req.question, deadline=deadline, agent=req.source
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
    ) -> tuple[int, str] | None:
        """Lê uma resposta em texto livre com deadline. Retorna (-1, texto)."""
        remaining_s = max(0, int(deadline - time.monotonic()))
        # Via container: a pergunta vai embutida no prompt e só aparece depois
        # do request_floor, sem ser emitida no feed antes (evita atropelamento).
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
        # Fallback (sem pt ativo): emite a pergunta e lê por linha.
        self._emit(f"\n{question}")
        answer = self._read_line(
            f"  Resposta (auto em {remaining_s}s): ", deadline=deadline
        )
        if answer is None:
            return None
        return -1, answer.rstrip("\n\r")

    # ------------------------------------------------------------------
    # Primitivos de leitura com deadline
    # ------------------------------------------------------------------

    def _read_line(self, prompt: str, *, deadline: float) -> str | None:
        """Lê uma linha com deadline. Usa input_gate se pt ativo, senão select."""
        gate = self._input_gate
        if gate is not None:
            is_active = getattr(gate, "is_active", None)
            if callable(is_active) and is_active():
                remaining = max(0.5, deadline - time.monotonic())
                read_fn = getattr(gate, "read_input_in_terminal", None)
                if callable(read_fn):
                    return read_fn(prompt, timeout=remaining)
        # pt não ativo: select + readline direto
        import select as _sel
        sys.stdout.write(prompt)
        sys.stdout.flush()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            ready, _, _ = _sel.select([sys.stdin], [], [], remaining)
        except Exception:
            return None
        if not ready:
            return None
        try:
            return sys.stdin.readline()
        except (Exception, KeyboardInterrupt):
            return None

    def _read_selection(
        self,
        question: str,
        options: list[str],
        *,
        deadline: float,
        agent: str = "agente",
    ) -> tuple[int, str] | None:
        """Seleção interativa com deadline. Usa input_gate se pt ativo."""
        gate = self._input_gate
        if gate is not None:
            is_active = getattr(gate, "is_active", None)
            if callable(is_active) and is_active():
                remaining = max(0.5, deadline - time.monotonic())
                read_fn = getattr(gate, "read_selection_in_terminal", None)
                if callable(read_fn):
                    container, renderer = self._container_for(agent)
                    if container is not None:
                        return container.ask_selection(
                            renderer, gate, question, options, timeout=remaining
                        )
                    return read_fn(question, options, timeout=remaining)
        # pt não ativo: seleção numerada por linha (cooked mode, sem termios)
        return self._line_select(question, options, deadline=deadline)

    def _line_select(
        self,
        question: str,
        options: list[str],
        *,
        deadline: float,
    ) -> tuple[int, str] | None:
        """Fallback readline sem raw mode (quando termios indisponível ou stdin não-tty)."""
        if not sys.stdin.isatty():
            return None
        remaining_s = max(0, int(deadline - time.monotonic()))
        lines = [question]
        for i, opt in enumerate(options):
            lines.append(f"  {i + 1}. {opt}")
        lines.append(f"  (1-{len(options)} · auto em {remaining_s}s)")
        self._emit("\n".join(lines))
        while True:
            if deadline - time.monotonic() <= 0:
                return None
            prompt = f"  Selecione (1-{len(options)}): "
            answer = self._read_line(prompt, deadline=deadline)
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

    def _close_live_if_active(self) -> None:
        """Fecha o Rich Live display (streaming) para evitar conflito com I/O direto.

        Chama suspend_output + resume_output no renderer: fecha o Live, drena
        eventos pendentes da fila (conteúdo gerado antes da tool call) e deixa
        o terminal limpo para I/O interativo sem interferência.
        Deve ser chamado uma vez por request, antes de qualquer I/O interativo.
        """
        renderer = self._renderer
        if renderer is None:
            return
        try:
            renderer.suspend_output(timeout=1.0)
        except Exception:
            pass
        try:
            renderer.resume_output(timeout=1.0)
        except Exception:
            pass

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

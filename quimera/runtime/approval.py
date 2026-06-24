"""Componentes de `quimera.runtime.approval`."""
from __future__ import annotations

import inspect
import select
import threading
import sys
from abc import ABC, abstractmethod
from contextlib import nullcontext

from .approval_broker import ApprovalBroker, TrustedToolExecutionContext


def _emit_approval_message(renderer, message: str) -> None:
    """Emite mensagem de approval e força flush quando houver renderer."""
    if renderer is not None:
        show_approval = getattr(renderer, "show_approval", None)
        if callable(show_approval):
            show_approval(message)
        else:
            renderer.show_system(message)
        flush = getattr(renderer, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception:
                pass
        return
    print(message)


def _extract_renderer(base_handler) -> object | None:
    """Extrai renderer real sem materializar atributos dinâmicos de mocks."""
    try:
        inspect.getattr_static(base_handler, "_renderer")
    except AttributeError:
        return None
    try:
        return getattr(base_handler, "_renderer", None)
    except Exception:
        return None


def _approval_window_context(renderer, *, title: str = "Aprovação", metadata=None):
    """Return a context manager for approval terminal ownership."""
    if renderer is None:
        return nullcontext()
    return renderer.approval_window(title=title, metadata=metadata or {})


def _static_callable_attr(obj, name: str):
    """Retorna callable somente quando o atributo existe estaticamente.

    Evita falsos positivos com MagicMock, que fabrica atributos sob demanda.
    """
    try:
        inspect.getattr_static(obj, name)
    except AttributeError:
        return None
    try:
        value = getattr(obj, name)
    except Exception:
        return None
    return value if callable(value) else None


class ApprovalHandler(ABC):
    """Define o contrato de aprovação usado pelo runtime de ferramentas."""

    @abstractmethod
    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Decide se uma ferramenta pode ser executada."""
        raise NotImplementedError

    def approve_request(self, request) -> bool:
        """Aprova um ApprovalRequest governado pelo ApprovalBroker.

        Handlers continuam sem calcular risco: o request já vem pronto do
        broker, com summary, risk, route, path, command e contexto auditável.
        A implementação padrão preserva compatibilidade chamando approve().
        """
        return self.approve(tool_name=request.tool_name, summary=request.summary)


class _ApprovalCancelled(Exception):
    """Sinal interno para interrupção cooperativa do prompt de aprovação."""


class ConsoleApprovalHandler(ApprovalHandler):
    """Confirmação simples no terminal via input() bloqueante."""

    def __init__(
        self,
        input_fn=None,
        renderer=None,
        suspend_fn=None,
        resume_fn=None,
        cancel_event=None,
        cancel_poll_interval: float = 0.1,
        input_gate=None,
    ):
        """Inicializa com dependências injetáveis.

        Args:
            input_fn: Função de input (fallback: builtins.input).
                      Se None, usa input() dinamicamente para
                      compatibilidade com @patch('builtins.input').
            renderer: TerminalRenderer opcional para exibir prompts.
            suspend_fn: Callback chamado antes de input() bloqueante
                        para suspender estado não-bloqueante do app.
            resume_fn: Callback chamado após input() bloqueante
                       para restaurar estado não-bloqueante do app.
            input_gate: InputGate opcional. Quando fornecido, substitui
                        input_fn e coordena com o renderer sem suspend/resume.
        """
        self._input_fn = input_fn
        self._renderer = renderer
        self._suspend_fn = suspend_fn
        self._resume_fn = resume_fn
        self._suspend_spinner_fn = {}
        self._resume_spinner_fn = {}
        self._suspend_spinner_fn_default = None
        self._resume_spinner_fn_default = None
        self._approve_all_callback = None
        self._cancel_event = cancel_event
        self._cancel_poll_interval = max(float(cancel_poll_interval), 0.01)
        self._input_gate = input_gate
        self._input_broker = None
        self._interactive_lock = threading.Lock()

    def set_spinner_callbacks(self, suspend_spinner_fn, resume_spinner_fn):
        """Define callbacks para pausar/retomar o spinner do Rich.

        Esses callbacks são injetados por _call_api (client.py) para evitar
        que o refresh do Rich Console.status() compita com input() bloqueante
        durante a aprovação. Chamados em _approve_interactive.

        Armazena tanto por thread (compatibilidade) quanto como fallback global,
        pois em thread=0 os callbacks são registrados pela main thread mas o
        approve() é chamado da thread do driver — IDs diferentes.
        """
        thread_id = threading.get_ident()
        self._suspend_spinner_fn[thread_id] = suspend_spinner_fn
        self._resume_spinner_fn[thread_id] = resume_spinner_fn
        self._suspend_spinner_fn_default = suspend_spinner_fn
        self._resume_spinner_fn_default = resume_spinner_fn
        broker_setter = getattr(self._input_broker, "set_spinner_callbacks", None)
        if callable(broker_setter):
            broker_setter(suspend_spinner_fn, resume_spinner_fn)

    def set_approve_all_callback(self, callback):
        """Define callback chamado quando o usuário digita 'a' (approve all).

        O callback recebe zero argumentos e deve ativar o modo
        'approve all' no handler de aprovação (ex: PreApprovalHandler).
        """
        self._approve_all_callback = callback

    def set_cancel_event(self, cancel_event) -> None:
        """Define um cancel_event opcional para interromper input bloqueante."""
        self._cancel_event = cancel_event

    def set_input_broker(self, broker) -> None:
        """Conecta ao InputBroker centralizado para serializar aprovações."""
        self._input_broker = broker
        broker_setter = getattr(broker, "set_spinner_callbacks", None)
        if callable(broker_setter):
            broker_setter(
                self._suspend_spinner_fn_default,
                self._resume_spinner_fn_default,
            )

    def process_pending_input_once(self) -> bool:
        """Processa uma pergunta pendente do InputBroker na thread atual."""
        broker = self._input_broker
        process = getattr(broker, "process_pending_once", None)
        if callable(process):
            return bool(process())
        return False

    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Tenta aprovação interativa, roteando pelo InputBroker quando disponível.

        Com broker: serializa na fila centralizada com timeout e auto-resposta
        segura — garante que approval e ask_user nunca conflitem em raw mode.
        Sem broker: comportamento original via _approve_interactive.
        """
        if self._input_broker is not None:
            return self._input_broker.request_approval(
                tool_name,
                summary,
                source="aprovação",
                on_approve_all=self._approve_all_callback,
            )
        return self._approve_interactive(tool_name, summary)

    def approve_request(self, request) -> bool:
        """Renderiza exatamente o preview governado produzido pelo broker."""
        return self.approve(tool_name=request.tool_name, summary=request.summary)

    @staticmethod
    def _is_stdin_interactive() -> bool:
        """Verifica se stdin é um terminal interativo no contexto atual."""
        try:
            return sys.stdin is not None and sys.stdin.isatty()
        except Exception:
            return False

    def _approve_interactive(self, tool_name: str, summary: str) -> bool:
        """Aprovação interativa via input_gate ou input_fn (usado em testes/REPL).

        Serializada via _interactive_lock: se outra thread já estiver no meio
        de um prompt de aprovação, esta chamada retorna False imediatamente
        sem bloquear o executor, evitando prompts de aprovação concorrentes.

        Quando input_gate está disponível E a chamada vem da thread principal,
        delega a ele — o InputGate coordena com o renderer via RichPromptSession,
        dispensando suspend/resume.
        Em threads de background (ex: servidor MCP), usa o caminho básico com
        input() e suspend/resume do renderer para evitar conflito com prompt_toolkit.
        """
        # Lock bloqueante: se outra thread já está num prompt, aguarda.
        self._interactive_lock.acquire(blocking=True)

        try:
            if self._cancel_event and self._cancel_event.is_set():
                return False
            self._show(f"\nAprovar {tool_name}\n{summary}")

            is_main = threading.current_thread() is threading.main_thread()
            # input_gate usa prompt_toolkit: seguro na thread principal.
            use_input_gate = self._input_gate is not None and is_main
            # Threads de background com pt ativo: delegar ao pt via run_in_terminal.
            gate_is_active = getattr(self._input_gate, "is_active", None)
            input_gate_active = (
                self._input_gate is not None
                and callable(gate_is_active)
                and gate_is_active()
            )
            use_input_gate_xthread = not is_main and input_gate_active

            if use_input_gate:
                if self._cancel_event and self._cancel_event.is_set():
                    return False
                thread_id = threading.get_ident()
                suspend_fn = (
                    self._suspend_spinner_fn.get(thread_id)
                    or self._suspend_spinner_fn_default
                )
                if suspend_fn:
                    suspend_fn()
                try:
                    answer = (
                        self._input_gate("  Executar? [y/N/a=todas]: ")
                        .strip()
                        .lower()
                    )
                except (EOFError, KeyboardInterrupt):
                    self._show(
                        "  stdin não disponível — negando automaticamente"
                    )
                    return False
                finally:
                    thread_id = threading.get_ident()
                    resume_fn = (
                        self._resume_spinner_fn.get(thread_id)
                        or self._resume_spinner_fn_default
                    )
                    if resume_fn:
                        resume_fn()
            elif use_input_gate_xthread:
                # Background thread + prompt_toolkit ativo: usa run_in_terminal
                # para suspender o app, restaurar o terminal e ler do usuário sem
                # conflitar com o raw mode do pt nem duplicar a saída.
                raw = self._input_gate.read_input_in_terminal(
                    "  Executar? [y/N/a=todas]: "
                )
                if raw is None:
                    self._show(
                        "  sem resposta — negando automaticamente"
                    )
                    return False
                answer = raw.strip().lower()
            else:
                # Background thread sem prompt_toolkit ativo: usa o caminho
                # padrão com suspend/resume. InputGate inativo não é prova de
                # raw mode residual; no fluxo normal do app isso pode ocorrer
                # durante tool approval fora do prompt.
                renderer = self._renderer
                input_fn = self._input_fn if self._input_fn is not None else input
                if self._suspend_fn:
                    self._suspend_fn()
                with _approval_window_context(renderer, title="Aprovação"):
                    thread_id = threading.get_ident()
                    suspend_fn = (
                        self._suspend_spinner_fn.get(thread_id)
                        or self._suspend_spinner_fn_default
                    )
                    if suspend_fn:
                        suspend_fn()
                    try:
                        if self._input_fn is None and self._cancel_event is not None:
                            answer = self._read_builtin_input_with_cancel(
                                "  Executar? [y/N/a=todas]: "
                            ).strip().lower()
                        else:
                            answer = input_fn(
                                "  Executar? [y/N/a=todas]: "
                            ).strip().lower()
                    except _ApprovalCancelled:
                        return False
                    except EOFError:
                        self._show(
                            "  stdin não disponível — negando automaticamente"
                        )
                        return False
                    finally:
                        thread_id = threading.get_ident()
                        resume_fn = (
                            self._resume_spinner_fn.get(thread_id)
                            or self._resume_spinner_fn_default
                        )
                        if resume_fn:
                            resume_fn()
                        if self._resume_fn:
                            self._resume_fn()
            if answer in {"a", "all", "todas"}:
                if self._approve_all_callback:
                    self._approve_all_callback()
                return True
            return answer in {"y", "yes", "s", "sim"}
        finally:
            self._interactive_lock.release()

    def _show(self, message: str) -> None:
        _emit_approval_message(self._renderer, message)

    def _is_cancelled(self) -> bool:
        event = self._cancel_event
        if event is None:
            return False
        is_set = getattr(event, "is_set", None)
        if callable(is_set):
            try:
                return bool(is_set())
            except Exception:
                return False
        return False

    def _read_builtin_input_with_cancel(self, prompt: str) -> str:
        """Lê uma linha com polling para permitir cancelamento cooperativo."""
        stdin = sys.stdin
        if stdin is None:
            raise EOFError

        fileno = getattr(stdin, "fileno", None)
        isatty = getattr(stdin, "isatty", None)
        if not callable(fileno) or (callable(isatty) and not isatty()):
            if self._is_cancelled():
                raise _ApprovalCancelled
            return input(prompt)

        sys.stdout.write(prompt)
        sys.stdout.flush()
        while True:
            if self._is_cancelled():
                # Garante quebra de linha para não colidir visualmente com o próximo prompt.
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise _ApprovalCancelled
            ready, _, _ = select.select([stdin], [], [], self._cancel_poll_interval)
            if not ready:
                continue
            line = stdin.readline()
            if line == "":
                raise EOFError
            return line


class AutoApprovalHandler(ApprovalHandler):
    """Aprovação automática sem interação — usar apenas em contextos controlados (REPL/testes)."""

    def __init__(self, approve_all: bool = True) -> None:
        """Inicializa uma instância de AutoApprovalHandler."""
        self._approve_all = approve_all

    def approve(self, *, tool_name: str, summary: str) -> bool:
        """Retorna a política de aprovação automática configurada."""
        status = "aprovado" if self._approve_all else "negado"
        print(f"  [auto-{status}] {tool_name}")
        return self._approve_all


# Limite de segurança para o laço de drenagem do stdin (_drain_stdin).
# Impede busy-loop infinito caso o select reporte readable continuamente sem
# nunca sinalizar EOF (stdin mockado em teste ou pipe patológico em runtime).
_DRAIN_MAX_ITERATIONS = 1000


class NonBlockingConsoleApprovalHandler(ApprovalHandler):
    """Aprovação não-bloqueante com timeout via select —
    ideal para uso em loop principal."""

    def __init__(self, timeout_seconds: float = 5.0, renderer=None) -> None:
        self._timeout = timeout_seconds
        self._renderer = renderer

    def approve(self, *, tool_name: str, summary: str) -> bool:
        _emit_approval_message(self._renderer, f"\nAprovar {tool_name}")
        _emit_approval_message(self._renderer, f"  {summary}")
        _emit_approval_message(
            self._renderer,
            f"  Digite 'y' em até {self._timeout:.0f}s "
            f"para aprovar, ou qualquer tecla para negar...",
        )

        answer = self._read_with_timeout(
            self._timeout, show_prompt=self._renderer is None
        )
        if answer is None:
            _emit_approval_message(
                self._renderer, "  timeout — negando automaticamente"
            )
            return False
        approved = answer.strip().lower() in {"y", "yes", "s", "sim"}
        status = "aprovado" if approved else "negado"
        _emit_approval_message(self._renderer, f"  {status}")
        return approved

    def _read_with_timeout(
        self, timeout: float, *, show_prompt: bool = True
    ) -> str | None:
        try:
            stdin = sys.stdin
            if stdin is None:
                return None
            self._drain_stdin()
            if show_prompt:
                sys.stdout.write("  > ")
                sys.stdout.flush()
            ready, _, _ = select.select([stdin], [], [], timeout)
            if not ready:
                return None
            return stdin.readline()
        except Exception:
            return None

    @staticmethod
    def _drain_stdin() -> None:
        """Descarta linhas pendentes no stdin sem alterar o modo do terminal.

        Best-effort, sem termios/raw-mode: em modo cooked o select só reporta
        readable após Enter, então só linhas completas são drenadas. Mantém o
        terminal intocado para não conflitar com o input do prompt_toolkit.

        Limitado a um número máximo de iterações para nunca virar busy-loop:
        se o stdin ficar continuamente "pronto" sem nunca sinalizar EOF (ex.:
        stdin mockado em teste, ou um pipe patológico), o laço encerra mesmo
        assim em vez de prender a CPU a 100% e travar o processo.
        """
        try:
            for _ in range(_DRAIN_MAX_ITERATIONS):
                ready, _, _ = select.select([sys.stdin], [], [], 0.0)
                if not ready:
                    break
                if sys.stdin.readline() == "":
                    break
        except Exception:
            return


class PreApprovalHandler(ApprovalHandler):
    """Handler que permite pré-aprovar a próxima ferramenta antes dela ser chamada.

    Funciona como um semáforo: quando ``pre_approve()`` é chamado (ex: via
    comando /approve), a próxima chamada de ``approve()`` retorna True
    automaticamente. Após consumida, a pré-aprovação é resetada e chamadas
    subsequentes delegam ao handler base.
    """

    def __init__(self, base_handler: ApprovalHandler) -> None:
        self._base = base_handler
        self._renderer = _extract_renderer(base_handler)
        self._pre_approved = False
        self._approve_all = False
        self._approve_all_permanent = False
        self._approve_all_silent = False
        self._thread_approve_all: set[int] = set()
        self._scope_approve_all: set[str] = set()
        self._silent_thread_approve_all: set[int] = set()
        self._silent_scope_approve_all: set[str] = set()
        self._thread_scope_keys: dict[int, str] = {}
        self._lock = threading.Lock()

    def pre_approve(self) -> None:
        """Pré-aprova a próxima ferramenta (consumida uma única vez)."""
        with self._lock:
            self._pre_approved = True

    def reset(self) -> None:
        """Reseta a pré-aprovação sem consumir."""
        with self._lock:
            self._pre_approved = False

    def set_approve_all(self, enabled: bool = True, permanent: bool = False, silent: bool = False) -> None:
        """Ativa/desativa modo 'approve all' — aprova todas as ferramentas sem perguntar.

        Args:
            enabled: True para ativar, False para desativar.
            permanent: Se True, o modo sobrevive ao fim do ciclo de tool hops.
                       Se False (padrão), é resetado automaticamente ao fim do ciclo.
            silent: Se True, não emite logs de aprovação no chat.
        """
        with self._lock:
            self._approve_all = enabled
            self._approve_all_permanent = permanent if enabled else False
            self._approve_all_silent = silent if enabled else False
            if enabled:
                self._pre_approved = False

    def set_thread_approve_all(
        self,
        enabled: bool = True,
        scope_key: str | None = None,
        silent: bool = False,
    ) -> None:
        thread_id = threading.get_ident()
        with self._lock:
            if enabled:
                resolved_scope = (
                    scope_key
                    or self._thread_scope_keys.get(thread_id)
                    or f"thread:{thread_id}"
                )
                self._thread_scope_keys[thread_id] = resolved_scope
                self._thread_approve_all.add(thread_id)
                self._scope_approve_all.add(resolved_scope)
                if silent:
                    self._silent_thread_approve_all.add(thread_id)
                    self._silent_scope_approve_all.add(resolved_scope)
                else:
                    self._silent_thread_approve_all.discard(thread_id)
                    self._silent_scope_approve_all.discard(resolved_scope)
            else:
                self._thread_approve_all.discard(thread_id)
                self._silent_thread_approve_all.discard(thread_id)
                resolved_scope = scope_key or self._thread_scope_keys.pop(
                    thread_id, None
                )
                if resolved_scope is not None:
                    self._scope_approve_all.discard(resolved_scope)
                    self._silent_scope_approve_all.discard(resolved_scope)

    def get_thread_approval_scope(self) -> str | None:
        thread_id = threading.get_ident()
        with self._lock:
            return self._thread_scope_keys.get(thread_id)

    def bind_thread_approval_scope(
        self, scope_key: str | None
    ) -> str | None:
        thread_id = threading.get_ident()
        with self._lock:
            previous = self._thread_scope_keys.get(thread_id)
            if scope_key is None:
                self._thread_scope_keys.pop(thread_id, None)
            else:
                self._thread_scope_keys[thread_id] = scope_key
            return previous

    def _consume_local_approval(self, tool_name: str, summary: str):
        """Aplica pre-approve/approve-all local sem chamar handler base."""
        thread_id = threading.get_ident()
        with self._lock:
            if thread_id in self._thread_approve_all:
                if thread_id not in self._silent_thread_approve_all:
                    _emit_approval_message(
                        self._renderer,
                        f"  [approve-all] {tool_name} :: {summary}",
                    )
                return True
            scope_key = self._thread_scope_keys.get(thread_id)
            if scope_key is not None and scope_key in self._scope_approve_all:
                if scope_key not in self._silent_scope_approve_all:
                    _emit_approval_message(
                        self._renderer,
                        f"  [approve-all] {tool_name} :: {summary}",
                    )
                return True
            if self._approve_all:
                if not self._approve_all_silent:
                    _emit_approval_message(
                        self._renderer,
                        f"  [approve-all] {tool_name} :: {summary}",
                    )
                return True
            if self._pre_approved:
                self._pre_approved = False
                _emit_approval_message(
                    self._renderer,
                    f"  [pré-aprovado] {tool_name} :: {summary}",
                )
                return True
        return None

    def approve_request(self, request) -> bool:
        """Aprova request governado sem degradar summary/risco do broker."""
        local = self._consume_local_approval(request.tool_name, request.summary)
        if local is not None:
            return bool(local)
        approve_request = _static_callable_attr(self._base, "approve_request")
        if callable(approve_request):
            return bool(approve_request(request))
        return self._base.approve(
            tool_name=request.tool_name, summary=request.summary
        )

    def approve(self, *, tool_name: str, summary: str) -> bool:
        local = self._consume_local_approval(tool_name, summary)
        if local is not None:
            return bool(local)
        return self._base.approve(tool_name=tool_name, summary=summary)

    def reset_approve_all_after_cycle(self) -> None:
        thread_id = threading.get_ident()
        with self._lock:
            self._thread_approve_all.discard(thread_id)
            self._silent_thread_approve_all.discard(thread_id)
            scope_key = self._thread_scope_keys.pop(thread_id, None)
            if scope_key is not None:
                self._scope_approve_all.discard(scope_key)
                self._silent_scope_approve_all.discard(scope_key)
            if self._approve_all and not self._approve_all_permanent:
                self._approve_all = False
                self._approve_all_silent = False


class ApprovalManager(ApprovalHandler):
    """Composition root local para aprovação de ferramentas.

    O manager não reimplementa governança: ele monta o pipeline local de
    aprovação humana (input/spinner/cancel/approve-all) e usa
    ``ApprovalBroker`` como engine canônica de autorização, auditoria,
    escopos e serialização. Assim, handlers continuam pequenos e o broker
    concentra a decisão robusta usada também pelos boundaries MCP.
    """

    def __init__(
        self,
        config,
        *,
        base_handler: ApprovalHandler | None = None,
        renderer=None,
        input_gate=None,
        input_broker=None,
        input_fn=None,
        suspend_fn=None,
        resume_fn=None,
        cancel_event=None,
        cancel_poll_interval: float = 0.1,
    ) -> None:
        self.config = config
        self._renderer = renderer

        if base_handler is None:
            self._console_handler = ConsoleApprovalHandler(
                input_fn=input_fn,
                renderer=renderer,
                suspend_fn=suspend_fn,
                resume_fn=resume_fn,
                cancel_event=cancel_event,
                cancel_poll_interval=cancel_poll_interval,
                input_gate=input_gate,
            )
            self._console_handler.set_input_broker(input_broker)
        else:
            self._console_handler = base_handler
            setter = getattr(self._console_handler, "set_input_broker", None)
            if callable(setter):
                setter(input_broker)

        self._pre_handler = PreApprovalHandler(self._console_handler)

        setter = getattr(self._console_handler, "set_approve_all_callback", None)
        if callable(setter):
            setter(lambda: self._pre_handler.set_approve_all(True))

        self._broker = ApprovalBroker(config, self._pre_handler)

    # ── Handler mode (interface ApprovalHandler) ────────────────────

    def approve(self, *, tool_name: str, summary: str) -> bool:
        return self._pre_handler.approve(tool_name=tool_name, summary=summary)

    def approve_request(self, request) -> bool:
        """Aprova ApprovalRequest já enriquecido pelo ApprovalBroker."""
        return self._pre_handler.approve_request(request)

    # ── Authorization governance (ApprovalBroker engine) ─────────────

    @property
    def audit_log(self) -> list[dict]:
        return self._broker.audit_log

    @property
    def governance(self):
        """Alias público compatível para o ApprovalBroker canônico."""
        return self._broker

    @property
    def approval_broker(self):
        """Engine canônica de governança exposta explicitamente."""
        return self._broker

    def build_context(self, call):
        return self._broker.build_context(call)

    def classify(self, call):
        return self._broker.classify(call)

    def create_authorization_request(
        self, call, *, permission_error=None, reason=None
    ):
        return self._broker.create_request(
            call, permission_error=permission_error, reason=reason
        )

    def create_request(self, call, *, permission_error=None, reason=None):
        """Alias legado para create_authorization_request()."""
        return self.create_authorization_request(
            call, permission_error=permission_error, reason=reason
        )

    def authorize_call(
        self,
        call,
        *,
        needs_policy_approval,
        permission_error=None,
    ) -> bool:
        return self._broker.approve(
            call,
            needs_policy_approval=needs_policy_approval,
            permission_error=permission_error,
        )

    def approve_call(
        self,
        call,
        *,
        needs_policy_approval,
        permission_error=None,
    ) -> bool:
        """Alias legado: authorization governada de ToolCall."""
        return self.authorize_call(
            call,
            needs_policy_approval=needs_policy_approval,
            permission_error=permission_error,
        )

    def would_prompt_for_call(
        self,
        call,
        *,
        needs_policy_approval,
        permission_error=None,
    ) -> bool:
        return self._broker.should_request_approval(
            call,
            needs_policy_approval=needs_policy_approval,
            permission_error=permission_error,
        )

    def should_request_approval(
        self,
        call,
        *,
        needs_policy_approval,
        permission_error=None,
    ) -> bool:
        """Alias legado para would_prompt_for_call()."""
        return self.would_prompt_for_call(
            call,
            needs_policy_approval=needs_policy_approval,
            permission_error=permission_error,
        )

    def guard_execution(self, call):
        return self._broker.execution_guard(call)

    def execution_guard(self, call):
        """Alias legado para guard_execution()."""
        return self.guard_execution(call)

    def __getattr__(self, name: str):
        """Delega acessos legados de governança para o broker incorporado."""
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._broker, name)

    def approve_scope(self, scope):
        self._broker.approve_scope(scope)

    def approve_equivalent(self, request, *, ttl_seconds=None, uses=1):
        kwargs = {"uses": uses}
        if ttl_seconds is not None:
            kwargs["ttl_seconds"] = ttl_seconds
        return self._broker.approve_equivalent(request, **kwargs)

    def create_route(
        self,
        call,
        context,
        *,
        path=None,
        command=None,
    ):
        return self._broker.create_route(
            call, context, path=path, command=command
        )

    # ── PreHandler (approve-all / pre-approve) ──────────────────────

    def pre_approve(self) -> None:
        self._pre_handler.pre_approve()

    def reset(self) -> None:
        self._pre_handler.reset()

    def set_approve_all(
        self,
        enabled: bool = True,
        permanent: bool = False,
        silent: bool = False,
    ) -> None:
        self._pre_handler.set_approve_all(
            enabled, permanent=permanent, silent=silent
        )

    def set_thread_approve_all(
        self,
        enabled: bool = True,
        scope_key: str | None = None,
        silent: bool = False,
    ) -> None:
        self._pre_handler.set_thread_approve_all(
            enabled, scope_key=scope_key, silent=silent
        )

    def get_thread_approval_scope(self) -> str | None:
        return self._pre_handler.get_thread_approval_scope()

    def bind_thread_approval_scope(
        self, scope_key: str | None
    ) -> str | None:
        return self._pre_handler.bind_thread_approval_scope(scope_key)

    def reset_approve_all_after_cycle(self) -> None:
        self._pre_handler.reset_approve_all_after_cycle()

    # ── ConsoleHandler (spinner / cancel / input broker) ────────────

    def set_spinner_callbacks(self, suspend_spinner_fn, resume_spinner_fn):
        setter = getattr(self._console_handler, "set_spinner_callbacks", None)
        if callable(setter):
            setter(suspend_spinner_fn, resume_spinner_fn)

    def set_cancel_event(self, cancel_event) -> None:
        setter = getattr(self._console_handler, "set_cancel_event", None)
        if callable(setter):
            setter(cancel_event)

    def set_input_broker(self, broker) -> None:
        setter = getattr(self._console_handler, "set_input_broker", None)
        if callable(setter):
            setter(broker)

    def process_pending_input_once(self) -> bool:
        process = getattr(self._console_handler, "process_pending_input_once", None)
        if callable(process):
            return bool(process())
        return False

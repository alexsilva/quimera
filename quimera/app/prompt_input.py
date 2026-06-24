"""Gate de input: PromptSession singleton, toolbar e coordenação com terminal.

Evita patch_stdout() flushando o renderer antes do prompt, eliminando o conflito
com Rich.Live que quebrava a toolbar. RichPromptSession é um wrapper thin que
coordena o ciclo antes/depois do prompt sem substituir o PromptSession."""
from __future__ import annotations

import asyncio
import atexit
import contextvars
from contextlib import nullcontext
import shutil
import sys
import threading
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.rule import Rule

from ..config import DEFAULT_USER_NAME


def _input_window_context(renderer, *, metadata=None):
    """Return a context manager for input terminal ownership."""
    if renderer is None:
        return nullcontext()
    return renderer.input_window(metadata=metadata or {})


def _selection_window_context(renderer, *, metadata=None):
    """Return a context manager for selection terminal ownership."""
    if renderer is None:
        return nullcontext()
    return renderer.selection_window(metadata=metadata or {})


def _approval_window_context(renderer, *, metadata=None):
    """Return a context manager for approval terminal ownership."""
    if renderer is None:
        return nullcontext()
    return renderer.approval_window(metadata=metadata or {})


class PromptFormatter:
    """Formata o prompt visível ao humano."""

    @staticmethod
    def format_user_prompt(user_name: str | None, mode_name: str | None = None) -> str:
        """Formata prompt humano, exibindo `[mode]` apenas fora do modo default."""
        normalized_name = str(user_name or "").strip()
        if not normalized_name:
            normalized_name = DEFAULT_USER_NAME
        if normalized_name not in {">", ">>>"}:
            normalized_name = normalized_name.rstrip(":").rstrip(">").strip() or DEFAULT_USER_NAME

        normalized_mode = str(mode_name or "").strip().lower() or "default"
        if normalized_mode in {"default", "execute"}:
            if normalized_name in {">", ">>>"}:
                return f"{normalized_name} "
            return f"{normalized_name}: "
        if normalized_name in {">", ">>>"}:
            return f"{normalized_name} [{normalized_mode}]: "
        return f"{normalized_name} [{normalized_mode}]: "

class _SlashCommandCompleter(Completer):
    """Completa comandos slash usando o resolver da aplicação."""

    def __init__(self, command_resolver, argument_resolver=None):
        self._command_resolver = command_resolver
        self._argument_resolver = argument_resolver

    def get_completions(self, document, complete_event):
        text_before_cursor = (document.text_before_cursor or "").lstrip()

        for prefix in ("s/", "r/"):
            if text_before_cursor.startswith(prefix):
                partial = text_before_cursor[len(prefix):]
                if callable(self._argument_resolver):
                    try:
                        suggestions = (
                            self._argument_resolver(prefix.rstrip("/"), partial) or []
                        )
                    except Exception:
                        suggestions = []
                    for suggestion in suggestions:
                        full = f"{prefix}{suggestion}"
                        if full.startswith(text_before_cursor):
                            yield Completion(
                                full, start_position=-len(text_before_cursor)
                            )
                return

        if not text_before_cursor.startswith("/"):
            return

        if " " in text_before_cursor:
            parts = text_before_cursor.split(" ", 1)
            command = parts[0]
            partial = parts[1] if len(parts) > 1 else ""
            if callable(self._argument_resolver):
                try:
                    suggestions = self._argument_resolver(command, partial) or []
                except Exception:
                    suggestions = []
                for suggestion in suggestions:
                    if suggestion.startswith(partial):
                        yield Completion(suggestion, start_position=-len(partial))
            return

        prefix = text_before_cursor
        resolver = self._command_resolver
        if not callable(resolver):
            return
        try:
            commands = sorted(set(resolver() or []))
        except Exception:
            commands = []
        for command in commands:
            if command.startswith(prefix):
                yield Completion(command, start_position=-len(prefix))


class InputGate:
    """Gate único de input com PromptSession singleton, toolbar e coordenação com terminal.

    - Quando prompt_toolkit estiver disponível: exibe toolbar contextual
       e placeholder cinza no campo vazio.
     - Se a sessão não estiver disponível, faz fallback para input() padrão.
    - Antes de exibir o prompt, drena eventos pendentes do renderer,
      garantindo que o output acima do prompt esteja estável.
    - Evita patch_stdout(): o renderer é flushado antes do prompt, então
      não há output concorrente durante o prompt, preservando a toolbar."""

    def __init__(self, renderer=None, toolbar_context_resolver=None, history_file=None, command_resolver=None, argument_resolver=None):
        self._session: PromptSession | None = None
        self._renderer = renderer
        self._toolbar_context_resolver = toolbar_context_resolver
        self._command_resolver = command_resolver
        self._argument_resolver = argument_resolver
        self._theme_cycle_handler = None
        self._history_file = Path(history_file).expanduser() if history_file else None
        self._active_lock = threading.Lock()
        self._active = False
        self._owner_thread_id: int | None = None
        self._running_context: contextvars.Context | None = None
        self._clock_condition = threading.Condition()
        self._clock_active = False
        self._clock_thread = threading.Thread(
            target=self._run_toolbar_clock, daemon=True, name="toolbar-clock"
        )
        self._clock_thread.start()

        history = InMemoryHistory()
        if self._history_file is not None:
            try:
                self._history_file.parent.mkdir(parents=True, exist_ok=True)
                history = FileHistory(str(self._history_file))
            except Exception:
                history = InMemoryHistory()
        self._session = PromptSession(history=history)

    def _set_active_state(self, active: bool) -> None:
        """Atualiza estado do prompt ativo de forma thread-safe."""
        with self._active_lock:
            self._active = active
            self._owner_thread_id = threading.get_ident() if active else None

    def set_toolbar_context_resolver(self, resolver) -> None:
        """Define callback para resolver contexto dinâmico da toolbar."""
        self._toolbar_context_resolver = resolver

    def set_command_resolver(self, resolver) -> None:
        """Define callback para resolver comandos de autocomplete."""
        self._command_resolver = resolver

    def set_argument_resolver(self, resolver) -> None:
        """Define callback para resolver argumentos de comandos no autocomplete."""
        self._argument_resolver = resolver

    def set_theme_cycle_handler(self, handler) -> None:
        """Define callback chamado ao pressionar Ctrl+T para trocar tema."""
        self._theme_cycle_handler = handler

    def _build_toolbar(self):
        """Toolbar com fragmentos estilo VS Code, usando FormattedText + Style.

        Cada item vira um fragmento ``(style, text)`` com classe ``toolbar.btn.*``,
        renderizado como chip retangular com fundo ``#3e3e3e`` sobre o fundo
        ``#252526`` da toolbar. A ``Style.from_dict`` define as cores por classe
        usando notaçao ponto (``toolbar.btn.accent``), que prompt_toolkit expande
        automaticamente para ``toolbar`` → ``toolbar.btn`` → ``toolbar.btn.accent``.
        """
        resolver = self._toolbar_context_resolver
        if not callable(resolver):
            return None

        def _toolbar():
            def _clip(value: str, max_len: int) -> str:
                if len(value) <= max_len:
                    return value
                return value[:max_len - 1].rstrip() + "…"

            try:
                context = resolver() or {}
            except Exception:
                context = {}

            responder = str(context.get("responder", "")).strip()
            model = str(context.get("model", "")).strip()
            branch = str(context.get("branch", "")).strip()
            elapsed = str(context.get("elapsed", "")).strip()
            active_agents = str(context.get("active_agents", "")).strip()
            parallel = str(context.get("parallel", "")).strip()
            open_bugs = str(context.get("open_bugs", "")).strip()
            mode = str(context.get("mode", "")).strip()
            turns = str(context.get("turns", "")).strip()
            session_id = str(context.get("session", "")).strip()
            theme = str(context.get("theme", "")).strip()

            visible_values = [
                responder, model, branch, elapsed, active_agents, parallel,
                open_bugs, mode, turns, session_id, theme,
            ]
            if not any(visible_values):
                return []

            def _btn(text: str, style_cls: str) -> tuple:
                return (f"class:toolbar.{style_cls}", f" {text} ")

            left = []
            if responder:
                left.append(_btn(_clip(responder, 24), "btn.accent"))
            if model:
                left.append(_btn(_clip(model, 24), "btn.model"))
            if branch:
                left.append(_btn(f"\u2387 {_clip(branch, 20)}", "btn.info"))
            if elapsed:
                left.append(_btn(f"\u29d6 {elapsed}", "btn.info"))
            if active_agents:
                left.append(_btn(f"\u2699 {_clip(active_agents, 30)}", "btn.info"))
            if parallel:
                left.append(_btn(f"\u26a1 {parallel}", "btn.info"))

            right = []
            if open_bugs:
                right.append(_btn(f"\u2717 {open_bugs}", "btn.err"))
            if turns:
                right.append(_btn(f"\u21ba {turns}", "btn.dim"))
            if mode:
                right.append(_btn(f"\u25c8 {mode}", "btn.dim"))
            if theme:
                right.append(_btn(f"\u2728 {_clip(theme, 12)}", "btn.dim"))
            if session_id:
                right.append(_btn(f"\U0001f517 {_clip(session_id, 22)}", "btn.dim"))

            term_w = shutil.get_terminal_size(fallback=(80, 24)).columns

            left_visible = sum(len(t) for _, t in left) if left else 0
            right_visible = sum(len(t) for _, t in right) if right else 0
            if right:
                padding = max(1, term_w - left_visible - right_visible)
            else:
                padding = 0

            fragments = [("", " ")]
            fragments.extend(left)
            if right:
                fragments.append(("", " " * (padding - 1)))
            fragments.extend(right)

            return fragments

        return _toolbar

    def _build_placeholder(self):
        """Placeholder cinza exibido quando o campo de input está vazio."""
        return HTML('<style fg="#606060">mensagem...</style>')

    def _build_completer(self):
        """Completer de comandos slash para o PromptSession."""
        if not callable(self._command_resolver):
            return None
        return _SlashCommandCompleter(self._command_resolver, self._argument_resolver)

    def _build_key_bindings(self):
        """Monta key bindings adicionais para troca de tema."""
        handler = self._theme_cycle_handler
        if not callable(handler):
            return None
        kb = KeyBindings()

        def _cycle_theme(event):
            try:
                handler()
            except Exception:
                pass
            event.app.invalidate()

        # "c-t" já existe no modo Emacs padrão (transpose-chars). Marcar
        # como eager garante que o atalho de tema tenha prioridade.
        kb.add("c-t", eager=True)(_cycle_theme)
        # Fallbacks para terminais que capturam Ctrl+T (ex.: nova aba).
        kb.add("escape", "t", eager=True)(_cycle_theme)  # Alt+T
        kb.add("f6", eager=True)(_cycle_theme)

        return kb

    def _flush_renderer(self) -> None:
        """Drena eventos pendentes do renderer antes de exibir o prompt."""
        renderer = self._renderer
        if renderer is None:
            return
        flush = getattr(renderer, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception:
                pass

    def _print_rule(self) -> None:
        """Imprime divisor horizontal que delimita o bloco de input."""
        console = Console(highlight=False)
        console.print(Rule(style="dim"))

    def _run_toolbar_clock(self, interval: float = 30.0) -> None:
        """Thread persistente que invalida o prompt a cada segundo enquanto ativo.

        Dorme indefinidamente quando não há prompt ativo e acorda via
        _clock_condition quando __call__ sinaliza início/fim do prompt.
        """
        while True:
            with self._clock_condition:
                self._clock_condition.wait_for(lambda: self._clock_active)
                self._clock_condition.wait(timeout=interval)
                should_invalidate = self._clock_active
            if should_invalidate:
                self.redisplay()

    def __call__(self, prompt: str) -> str:
        """Lê input do usuário.

        Flusha o renderer antes de exibir o prompt, eliminando a necessidade
        de patch_stdout(). Usa session.prompt() diretamente com toolbar,
        placeholder e completer — tudo nativo do PromptSession, funcionando.
        Se a sessão não estiver disponível, faz fallback para input() padrão.
        Se o prompt_toolkit já estiver rodando (ex: em outra thread), captura
        o AssertionError e cai no input() padrão.
        """
        self._set_active_state(True)
        with self._clock_condition:
            self._clock_active = True
            self._clock_condition.notify_all()
        try:
            self._flush_renderer()
            self._print_rule()

            if self._session is not None:
                toolbar_style = Style.from_dict({
                    "bottom-toolbar": "bg:#252526",
                    "toolbar.btn": "bg:#3e3e3e",
                    "toolbar.btn.accent": "fg:#5fc3ff bold",
                    "toolbar.btn.model": "fg:#9cdcfe",
                    "toolbar.btn.info": "fg:#d4d4d4",
                    "toolbar.btn.dim": "fg:#9e9e9e",
                    "toolbar.btn.err": "fg:#fc7b5f bold",
                })

                def _capture_context() -> None:
                    # Chamado de dentro do run_async do prompt_toolkit, onde
                    # _current_app_session já está definido no contextvars.
                    # Salva o contexto para que run_in_terminal_message possa
                    # agendar callbacks com a identidade correta do app.
                    self._running_context = contextvars.copy_context()

                result = self._session.prompt(
                    prompt,
                    bottom_toolbar=self._build_toolbar(),
                    placeholder=self._build_placeholder(),
                    completer=self._build_completer(),
                    key_bindings=self._build_key_bindings(),
                    style=toolbar_style,
                    complete_while_typing=False,
                    vi_mode=False,
                    pre_run=_capture_context,
                )
                return result
            # Fallback para input() padrão quando prompt_toolkit não está disponível
            result = input(prompt)
            return result
        finally:
            with self._clock_condition:
                self._clock_active = False
                self._clock_condition.notify_all()
            self._running_context = None
            self._set_active_state(False)

    def read_plain_input(self, prompt: str) -> str:
        """Le uma resposta curta sem regua, toolbar, placeholder ou completer."""
        self._set_active_state(True)
        with self._clock_condition:
            self._clock_active = False
            self._clock_condition.notify_all()
        try:
            self._flush_renderer()
            if self._session is not None:
                return self._session.prompt(
                    prompt,
                    complete_while_typing=False,
                    vi_mode=False,
                )
            return input(prompt)
        finally:
            self._running_context = None
            self._set_active_state(False)

    def read_selection_in_terminal(
        self, question: str, options: list[str], timeout: float = 300.0
    ) -> tuple[int, str] | None:
        """Seleção numerada por linha via run_in_terminal (cooked mode, sem termios).

        Exibe a pergunta com opções numeradas e lê a resposta como uma linha —
        o mesmo input usado para escrever no chat. O usuário digita o número
        (1-N) ou o texto exato da opção e confirma com Enter.

        Retorna (index, value) ou None se Ctrl+C / timeout / stdin não-tty.
        Seguro de chamar de threads de background enquanto prompt_toolkit está ativo.
        """
        import time as _time
        session = self._session
        if session is None:
            return None
        app = getattr(session, "app", None)
        if app is None or not getattr(app, "_is_running", False):
            return None
        loop = getattr(app, "loop", None)
        if loop is None or loop.is_closed() or not loop.is_running():
            return None

        deadline = _time.monotonic() + timeout
        result: list[tuple[int, str] | None] = [None]
        done = threading.Event()

        def _select_sync() -> None:
            import select as _sel
            renderer = self._renderer
            try:
                with _selection_window_context(renderer, metadata={"question": question}):
                    self._flush_renderer()
                    error: str | None = None
                    while True:
                        remaining = deadline - _time.monotonic()
                        if remaining <= 0:
                            return
                        lines = [question]
                        for i, opt in enumerate(options):
                            lines.append(f"  {i + 1}. {opt}")
                        if error:
                            lines.append(f"  ! {error}")
                        remaining_s = max(0, int(remaining))
                        lines.append(
                            f"  Selecione (1-{len(options)} ou texto"
                            f" \xb7 auto em {remaining_s}s): "
                        )
                        sys.stdout.write("\n".join(lines))
                        sys.stdout.flush()

                        try:
                            ready, _, _ = _sel.select([sys.stdin], [], [], remaining)
                            if not ready:
                                return
                        except Exception:
                            return
                        try:
                            raw_line = sys.stdin.readline()
                        except (EOFError, OSError):
                            return
                        if not raw_line:
                            return
                        # Cooked mode (ECHO ativo via in_terminal): a linha digitada
                        # já aparece no terminal; não re-ecoar para não duplicar.
                        raw = raw_line.rstrip("\n\r")
                        if raw.isdigit():
                            num = int(raw) - 1
                            if 0 <= num < len(options):
                                result[0] = (num, options[num])
                                return
                        for i, opt in enumerate(options):
                            if opt.lower() == raw.lower():
                                result[0] = (i, opt)
                                return
                        error = f"'{raw}' inválido — use 1-{len(options)} ou o texto exato"
            finally:
                done.set()

        async def _coro() -> None:
            await run_in_terminal(_select_sync, in_executor=True)

        try:
            future = asyncio.run_coroutine_threadsafe(_coro(), loop)
        except Exception:
            return None

        future.add_done_callback(lambda _: done.set())
        done.wait(timeout=timeout + 1.0)
        return result[0]

    def get_line_buffer(self) -> str:
        """Retorna o buffer atual de edição quando disponível."""
        session = self._session
        if session is None:
            return ""
        app = getattr(session, "app", None)
        if app is None:
            return ""
        current_buffer = getattr(app, "current_buffer", None)
        if current_buffer is None:
            return ""
        text = getattr(current_buffer, "text", "")
        return text or ""

    def redisplay(self) -> None:
        """Solicita redraw do prompt em sessões prompt_toolkit ativas."""
        session = self._session
        if session is None:
            return
        app = getattr(session, "app", None)
        if app is None:
            return
        invalidate = getattr(app, "invalidate", None)
        if callable(invalidate):
            invalidate()

    def is_active(self) -> bool:
        """Returns True if the prompt_toolkit session is currently waiting for input."""
        with self._active_lock:
            if self._active:
                return True
        if self._session is None:
            return False
        app = getattr(self._session, "app", None)
        if app is None:
            return False
        running = getattr(app, "_is_running", False)
        if isinstance(running, bool):
            return running
        return False

    def get_owner_thread_id(self) -> int | None:
        """Retorna a thread que atualmente possui o prompt interativo."""
        with self._active_lock:
            return self._owner_thread_id

    def run_in_terminal_message(self, callback) -> bool:
        """Agenda callback acima do prompt ativo quando prompt_toolkit está rodando."""
        if not callable(callback):
            return False
        session = self._session
        if session is None:
            return False
        app = getattr(session, "app", None)
        if app is None or not getattr(app, "_is_running", False):
            return False
        loop = getattr(app, "loop", None)
        if loop is None or loop.is_closed():
            return False

        def _schedule() -> None:
            try:
                coro = run_in_terminal(callback, render_cli_done=False, in_executor=False)
                asyncio.ensure_future(coro)
            except Exception:
                pass

        ctx = self._running_context
        try:
            loop.call_soon_threadsafe(_schedule, context=ctx)
        except Exception:
            return False
        return True

    def read_input_in_terminal(
        self, prompt: str, timeout: float = 300.0, render_card_fn=None
    ) -> str | None:
        """Lê uma linha via run_in_terminal — seguro de chamar de qualquer thread.

        Suspende o prompt_toolkit ativo, restaura o terminal para cooked mode,
        exibe o prompt e lê a resposta do usuário. Seguro de chamar de threads
        de background (ex: servidor MCP) enquanto a main thread está no prompt.

        Se ``render_card_fn`` for fornecida, é chamada com o Console do renderer
        dentro de ``input_window`` para exibir o contexto da pergunta como Rich
        renderable permanente antes do prompt de input cru.

        Returns:
            String com a resposta do usuário, ou None se timeout/erro ou loop parado.
        """
        session = self._session
        if session is None:
            return None
        app = getattr(session, "app", None)
        if app is None or not getattr(app, "_is_running", False):
            return None
        loop = getattr(app, "loop", None)
        if loop is None or loop.is_closed():
            return None
        # Verifica se o loop realmente está rodando (não apenas não-fechado).
        # TOCTOU: entre is_active() True e aqui, o usuário pode ter pressionado
        # Enter — o loop para mas is_closed() ainda é False. Sem essa checagem,
        # run_coroutine_threadsafe enfileira o coroutine e done.wait() trava por
        # `timeout` segundos porque o loop nunca vai processar a fila.
        if not loop.is_running():
            return None

        result: list[str | None] = [None]
        done = threading.Event()

        def _read_sync() -> None:
            import select as _sel
            renderer = self._renderer
            try:
                with _input_window_context(renderer, metadata={"prompt": prompt}):
                    self._flush_renderer()
                    # Exibe card Rich (com contexto) ou cai no prompt cru.
                    console = getattr(renderer, "_console", None) if renderer is not None else None
                    if render_card_fn is not None and console is not None:
                        try:
                            render_card_fn(console)
                            # Após o card, exibe apenas o marcador de input.
                            sys.stdout.write("> ")
                        except Exception:
                            sys.stdout.write(prompt)
                    else:
                        sys.stdout.write(prompt)
                    sys.stdout.flush()
                    try:
                        ready, _, _ = _sel.select([sys.stdin], [], [], timeout)
                        if not ready:
                            return
                    except Exception:
                        return
                    try:
                        raw_line = sys.stdin.readline()
                    except (EOFError, OSError):
                        return
                    # Não re-ecoamos a linha: o terminal está em cooked mode
                    # (ECHO ativo via in_terminal), então o que o usuário digita
                    # já aparece. Escrever de novo duplicaria a linha.
                    if raw_line:
                        result[0] = raw_line.rstrip("\n\r")
                    else:
                        result[0] = ""
            except (EOFError, OSError):
                result[0] = ""
            finally:
                done.set()

        async def _coro() -> None:
            await run_in_terminal(_read_sync, in_executor=True)

        try:
            future = asyncio.run_coroutine_threadsafe(_coro(), loop)
        except Exception:
            return None

        # Guarda TOCTOU residual: se o loop parar após is_running() mas antes de
        # executar _coro(), o future é cancelado pelo asyncio. O callback garante
        # que done seja setado sem depender de um timeout arbitrário.
        future.add_done_callback(lambda _: done.set())

        done.wait(timeout=timeout)
        return result[0]

    def read_approval_in_terminal(
        self,
        question: str,
        prompt: str,
        timeout: float = 300.0,
        render_card_fn=None,
    ) -> str | None:
        """Exibe question+prompt e lê a resposta por linha (cooked mode, sem termios).

        Garante que a pergunta e o prompt de aprovação aparecem e permanecem
        visíveis até o usuário responder — sem que o pt reexiba o CLI entre
        a exibição da pergunta e a leitura da resposta.

        Lê uma linha com o mesmo input usado para escrever no chat: o usuário
        digita y/n/a (ou yes/sim/todas) e confirma com Enter.

        Se ``render_card_fn`` for fornecida, é chamada com o Console do renderer
        dentro de ``approval_window`` e imprime o card de aprovação como Rich
        renderable permanente (com contexto e estilo visual). Caso contrário,
        imprime ``question`` como texto cru.

        Retorna a resposta normalizada (lowercase) ou None se timeout/erro/EOF.
        """
        import time as _time
        session = self._session
        if session is None:
            return None
        app = getattr(session, "app", None)
        if app is None or not getattr(app, "_is_running", False):
            return None
        loop = getattr(app, "loop", None)
        if loop is None or loop.is_closed() or not loop.is_running():
            return None

        deadline = _time.monotonic() + timeout
        result: list[str | None] = [None]
        done = threading.Event()

        def _approval_sync() -> None:
            import select as _sel
            renderer = self._renderer
            try:
                with _approval_window_context(renderer, metadata={"question": question}):
                    self._flush_renderer()
                    remaining_s = max(0, int(deadline - _time.monotonic()))
                    # O card é impresso dentro de approval_window: o renderer está
                    # suspenso e o chamador detém o chão, portanto console.print()
                    # vai direto ao terminal sem conflitar com o Live.
                    console = getattr(renderer, "_console", None) if renderer is not None else None
                    if render_card_fn is not None and console is not None:
                        try:
                            render_card_fn(console)
                        except Exception:
                            sys.stdout.write(question + "\n")
                            sys.stdout.flush()
                    else:
                        sys.stdout.write(question + "\n")
                    sys.stdout.write(prompt.rstrip() + f"  [auto em {remaining_s}s] ")
                    sys.stdout.flush()

                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        return

                    try:
                        ready, _, _ = _sel.select([sys.stdin], [], [], remaining)
                        if not ready:
                            return
                    except Exception:
                        return
                    try:
                        raw_line = sys.stdin.readline()
                    except (EOFError, OSError):
                        return
                    if not raw_line:
                        return
                    # Cooked mode (ECHO ativo via in_terminal): a resposta digitada
                    # já aparece no terminal; não re-ecoar para não duplicar.
                    result[0] = raw_line.rstrip("\n\r").strip().lower()
            finally:
                done.set()

        async def _coro() -> None:
            await run_in_terminal(_approval_sync, in_executor=True)

        try:
            future = asyncio.run_coroutine_threadsafe(_coro(), loop)
        except Exception:
            return None

        future.add_done_callback(lambda _: done.set())
        done.wait(timeout=timeout + 1.0)
        return result[0]

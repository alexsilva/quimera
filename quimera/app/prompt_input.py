"""Gate de input: PromptSession singleton, toolbar e coordenação com terminal.

Evita patch_stdout() flushando o renderer antes do prompt, eliminando o conflito
com Rich.Live que quebrava a toolbar. RichPromptSession é um wrapper thin que
coordena o ciclo antes/depois do prompt sem substituir o PromptSession."""
from __future__ import annotations

import asyncio
import atexit
import html
import threading
from pathlib import Path

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import run_in_terminal
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    _PT_AVAILABLE = True
except ImportError:
    _PT_AVAILABLE = False
    Completer = object
    run_in_terminal = None


class _SlashCommandCompleter(Completer):
    """Completa comandos slash usando o resolver da aplicação."""

    def __init__(self, command_resolver, argument_resolver=None):
        self._command_resolver = command_resolver
        self._argument_resolver = argument_resolver

    def get_completions(self, document, complete_event):
        text_before_cursor = (document.text_before_cursor or "").lstrip()
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
    - Quando não estiver disponível: fallback transparente para input().
    - Antes de exibir o prompt, drena eventos pendentes do renderer,
      garantindo que o output acima do prompt esteja estável.
    - Evita patch_stdout(): o renderer é flushado antes do prompt, então
      não há output concorrente durante o prompt, preservando a toolbar."""

    def __init__(self, renderer=None, toolbar_context_resolver=None, history_file=None, command_resolver=None, argument_resolver=None):
        self._session: PromptSession | None = None
        self._readline_history = None
        self._renderer = renderer
        self._toolbar_context_resolver = toolbar_context_resolver
        self._command_resolver = command_resolver
        self._argument_resolver = argument_resolver
        self._theme_cycle_handler = None
        self._history_file = Path(history_file).expanduser() if history_file else None
        self._lock = threading.Lock()
        if not _PT_AVAILABLE:
            self._init_readline()
            return

        history = InMemoryHistory()
        if self._history_file is not None:
            try:
                self._history_file.parent.mkdir(parents=True, exist_ok=True)
                history = FileHistory(str(self._history_file))
            except Exception:
                history = InMemoryHistory()
        self._session = PromptSession(history=history)

    def _init_readline(self) -> None:
        """Configura histórico readline como fallback."""
        try:
            import readline as _readline
        except ImportError:
            return
        if self._history_file is not None:
            try:
                self._history_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _readline.read_history_file(str(self._history_file))
                except FileNotFoundError:
                    pass
                atexit.register(_readline.write_history_file, str(self._history_file))
            except Exception:
                pass

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
        """Monta o conteúdo da toolbar contextual.

        Retorna uma callable para que o prompt_toolkit reavalie a toolbar a
        cada redesenho — necessário para que Ctrl+T atualize o nome do tema
        em tempo real via event.app.invalidate().
        """
        if not _PT_AVAILABLE:
            return None
        resolver = self._toolbar_context_resolver
        if not callable(resolver):
            return None

        def _toolbar():
            def _clip(value: str, max_len: int) -> str:
                if len(value) <= max_len:
                    return value
                return value[: max_len - 3].rstrip() + "..."

            try:
                context = resolver() or {}
            except Exception:
                context = {}

            responder = str(context.get("responder", "")).strip()
            model = str(context.get("model", "")).strip()
            theme = str(context.get("theme", "")).strip()
            parallel = str(context.get("parallel", "")).strip()
            turns = str(context.get("turns", "")).strip()
            open_bugs = str(context.get("open_bugs", "")).strip()
            active_agents = str(context.get("active_agents", "")).strip()
            mode = str(context.get("mode", "")).strip()
            branch = str(context.get("branch", "")).strip()
            elapsed = str(context.get("elapsed", "")).strip()
            session_id = str(context.get("session", "")).strip()
            if not any([responder, model, theme, parallel, turns,
                        open_bugs, active_agents, mode, branch, elapsed, session_id]):
                return ""

            parts = []
            # Primary: who responds + model
            if responder:
                parts.append(f"<b>{html.escape(responder)}</b>")
            if model:
                parts.append(html.escape(_clip(model, 48)))
            # Activity: agents running + parallel slots
            if active_agents:
                parts.append(f"<style fg='#88cc88'>{html.escape(_clip(active_agents, 48))}</style>")
            if parallel:
                parts.append(f"<b>{html.escape(parallel)}</b>")
            # Issues
            if open_bugs:
                parts.append(f"<style fg='#cc4444'>⚠ {html.escape(open_bugs)}</style>")
            # Context: mode, branch, time, counters
            if mode:
                parts.append(f"<i>{html.escape(mode)}</i>")
            if branch:
                parts.append(f"<style fg='#888888'>br:{html.escape(branch)}</style>")
            if elapsed:
                parts.append(f"<style fg='#888888'>{html.escape(elapsed)}</style>")
            if turns:
                parts.append(f"<style fg='#888888'>{html.escape(turns)}</style>")
            if theme:
                parts.append(f"<style fg='#888888'>tema:{html.escape(_clip(theme, 18))}</style>")
            if session_id:
                parts.append(f"<style fg='#888888'>sess:{html.escape(session_id)}</style>")
            return HTML(" | ".join(parts))

        return _toolbar

    def _build_placeholder(self):
        """Placeholder cinza exibido quando o campo de input está vazio."""
        if not _PT_AVAILABLE:
            return None
        return HTML('<style fg="#606060">mensagem...</style>')

    def _build_completer(self):
        """Completer de comandos slash para o PromptSession."""
        if not _PT_AVAILABLE:
            return None
        if not callable(self._command_resolver):
            return None
        return _SlashCommandCompleter(self._command_resolver, self._argument_resolver)

    def _build_key_bindings(self):
        """Monta key bindings adicionais para troca de tema."""
        if not _PT_AVAILABLE:
            return None
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

    def __call__(self, prompt: str) -> str:
        """Lê input do usuário.

        Flusha o renderer antes de exibir o prompt, eliminando a necessidade
        de patch_stdout(). Usa session.prompt() diretamente com toolbar,
        placeholder e completer — tudo nativo do PromptSession, funcionando.
        """
        self._flush_renderer()

        if self._session is not None:
            return self._session.prompt(
                prompt,
                bottom_toolbar=self._build_toolbar(),
                placeholder=self._build_placeholder(),
                completer=self._build_completer(),
                key_bindings=self._build_key_bindings(),
                complete_while_typing=False,
                vi_mode=False,
            )

        return input(prompt)

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
        if self._session is None or not _PT_AVAILABLE:
            return False
        app = getattr(self._session, "app", None)
        if app is None:
            return False
        return bool(getattr(app, "_is_running", False))

    def run_in_terminal_message(self, callback) -> bool:
        """Agenda callback acima do prompt ativo quando prompt_toolkit está rodando."""
        if not callable(callback):
            return False
        session = self._session
        if session is None or not _PT_AVAILABLE or run_in_terminal is None:
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

        try:
            loop.call_soon_threadsafe(_schedule)
        except Exception:
            return False
        return True

"""Gate de input: PromptSession singleton, toolbar e coordenação com terminal.

Evita patch_stdout() flushando o renderer antes do prompt, eliminando o conflito
com Rich.Live que quebrava a toolbar. RichPromptSession é um wrapper thin que
coordena o ciclo antes/depois do prompt sem substituir o PromptSession."""
from __future__ import annotations

import atexit
import html
import threading
from pathlib import Path

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    _PT_AVAILABLE = True
except ImportError:
    _PT_AVAILABLE = False
    Completer = object


class _SlashCommandCompleter(Completer):
    """Completa comandos slash usando o resolver da aplicação."""

    def __init__(self, command_resolver):
        self._command_resolver = command_resolver

    def get_completions(self, document, complete_event):
        text_before_cursor = (document.text_before_cursor or "").lstrip()
        if not text_before_cursor.startswith("/"):
            return
        if " " in text_before_cursor:
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

    def __init__(self, renderer=None, toolbar_context_resolver=None, history_file=None, command_resolver=None):
        self._session: PromptSession | None = None
        self._readline_history = None
        self._renderer = renderer
        self._toolbar_context_resolver = toolbar_context_resolver
        self._command_resolver = command_resolver
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
            try:
                context = resolver() or {}
            except Exception:
                context = {}

            responder = str(context.get("responder", "")).strip()
            model = str(context.get("model", "")).strip()
            cwd = str(context.get("cwd", "")).strip()
            theme = str(context.get("theme", "")).strip()
            if not responder and not model and not cwd and not theme:
                return ""

            parts = []
            if theme:
                parts.append(f"<b>tema:{html.escape(theme)}</b>")
            if responder:
                parts.append(f"responde:{html.escape(responder)}")
            if model:
                parts.append(html.escape(model))
            if cwd:
                parts.append(html.escape(cwd))
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
        return _SlashCommandCompleter(self._command_resolver)

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

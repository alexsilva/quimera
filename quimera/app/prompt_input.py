"""Gate de input: PromptSession singleton, toolbar e coordenação com Rich.Live."""
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
    from prompt_toolkit.patch_stdout import patch_stdout
    _PT_AVAILABLE = True
except ImportError:
    _PT_AVAILABLE = False
    Completer = object

try:
    import readline as _readline
    _RL_AVAILABLE = True
except ImportError:
    _RL_AVAILABLE = False


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
    """Gate único de input com PromptSession singleton, toolbar e coordenação com Rich.Live.

    - Quando prompt_toolkit estiver disponível: exibe toolbar contextual
      e placeholder cinza no campo vazio.
    - Quando não estiver disponível: fallback transparente para input().
    - Antes de exibir o prompt, drena eventos pendentes do Rich.Live (renderer.flush()),
      garantindo que o output acima do prompt esteja estável.
    """

    def __init__(self, renderer=None, toolbar_context_resolver=None, history_file=None, command_resolver=None):
        """Inicializa uma instância de InputGate."""
        self._renderer = renderer
        self._toolbar_context_resolver = toolbar_context_resolver
        self._command_resolver = command_resolver
        self._history_file = Path(history_file).expanduser() if history_file else None
        self._lock = threading.Lock()
        if _PT_AVAILABLE:
            history = InMemoryHistory()
            if self._history_file is not None:
                try:
                    self._history_file.parent.mkdir(parents=True, exist_ok=True)
                    history = FileHistory(str(self._history_file))
                except Exception:
                    history = InMemoryHistory()
            self._session: "PromptSession | None" = PromptSession(
                history=history
            )
        else:
            self._session = None
            if _RL_AVAILABLE and self._history_file is not None:
                try:
                    self._history_file.parent.mkdir(parents=True, exist_ok=True)
                    _readline.read_history_file(str(self._history_file))
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
                atexit.register(_readline.write_history_file, str(self._history_file))

    def set_toolbar_context_resolver(self, resolver) -> None:
        """Define callback para resolver contexto dinâmico da toolbar."""
        self._toolbar_context_resolver = resolver

    def set_command_resolver(self, resolver) -> None:
        """Define callback para resolver comandos de autocomplete."""
        self._command_resolver = resolver

    def _build_toolbar(self):
        """Monta o conteúdo da toolbar contextual."""
        if not _PT_AVAILABLE:
            return None
        resolver = self._toolbar_context_resolver
        if not callable(resolver):
            return None

        try:
            context = resolver() or {}
        except Exception:
            context = {}

        responder = str(context.get("responder", "")).strip()
        model = str(context.get("model", "")).strip()
        cwd = str(context.get("cwd", "")).strip()
        if not responder and not model and not cwd:
            return None

        parts = []
        if responder:
            parts.append(f"responde: {html.escape(responder)}")
        if model:
            parts.append(f"model: {html.escape(model)}")
        if cwd:
            parts.append(html.escape(cwd))
        return HTML(" | ".join(parts))

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

    def _flush_renderer(self) -> None:
        """Drena eventos pendentes do Rich.Live antes de exibir o prompt."""
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

        Flushes o renderer antes de exibir o prompt (T-001: coordenação com Rich.Live).
        Usa PromptSession com toolbar e placeholder quando disponível (T-003).
        """
        self._flush_renderer()

        if self._session is not None:
            with patch_stdout():
                return self._session.prompt(
                    prompt,
                    bottom_toolbar=self._build_toolbar(),
                    placeholder=self._build_placeholder(),
                    completer=self._build_completer(),
                    complete_while_typing=False,
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

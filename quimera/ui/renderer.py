"""Renderização de terminal para a UI do Quimera."""
import collections
import logging
import os
import queue as _queue_module
import re
import shutil
import sys as _sys_module
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

from quimera.runtime.streaming import apply_stream_diff, normalize_stream_diff
from .audit import RenderAuditLogger

_UNICODE_CONTROL_RE = re.compile(
    r"[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u061C\u200B-\u200F\u202A-\u202E\u2060-\u2069\uFEFF]"
)
_RENDER_MODES = {"plain", "markdown", "auto"}
_PREVIEW_LIMIT = 160
_SEQUENTIAL_STATUS_REFRESH_PER_SECOND = 4
_SCROLLING_WINDOW_SIZE = 10


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    ansi_real = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')
    text = ansi_real.sub('', text)
    ansi_orphaned = re.compile(r'\[[0-9;?]+[A-Za-z]')
    text = ansi_orphaned.sub('', text)
    text = _UNICODE_CONTROL_RE.sub('', text)
    return text


def _normalize_render_mode(render_mode: str | None) -> str:
    mode = str(render_mode or "auto").strip().lower()
    if mode in _RENDER_MODES:
        return mode
    return "auto"


def _normalize_completed_content(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def _normalize_stream_diff(diff) -> list[dict[str, str]]:
    """Normaliza o payload incremental aceito pelo renderer."""
    return normalize_stream_diff(diff, transform_text=strip_ansi)


def _apply_stream_diff(content: str, diff: list[dict[str, str]]) -> str:
    """Aplica operações incrementais de texto no buffer atual."""
    return apply_stream_diff(content, diff)


def _is_interactive_terminal() -> bool:
    """Check if we're running in an interactive terminal (not piped/captured)."""
    return sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'


def _public_ui_module():
    module = _sys_module.modules.get("quimera.ui")
    if module is not None:
        return module
    return _sys_module.modules[__name__]


def _extract_text_from_renderable(value: Any) -> str:
    """Extract human-readable text from Rich renderables without exposing internal repr."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if hasattr(value, "title") and hasattr(value, "characters"):
        return _extract_text_from_renderable(value.title)
    if hasattr(value, "columns") and hasattr(value, "rows"):
        parts = []
        for column in value.columns:
            parts.append(_extract_text_from_renderable(getattr(column, "header", "")))
            for cell in getattr(column, "_cells", ()):
                parts.append(_extract_text_from_renderable(cell))
        return " ".join(p for p in parts if p)
    if hasattr(value, "plain"):
        return str(value.plain)
    if hasattr(value, "renderables"):
        parts = []
        for child in value.renderables:
            parts.append(_extract_text_from_renderable(child))
        return " ".join(p for p in parts if p)
    if hasattr(value, "__rich_text__"):
        return str(value.__rich_text__())
    markup = getattr(value, "markup", None)
    if isinstance(markup, str):
        return markup
    panel_renderable = getattr(value, "renderable", None)
    if panel_renderable is not None:
        parts = []
        title = getattr(value, "title", None) or ""
        if title:
            parts.append(_extract_text_from_renderable(title))
        parts.append(_extract_text_from_renderable(panel_renderable))
        return " ".join(p for p in parts if p)
    return str(value)


def _preview_text(value: Any, limit: int = _PREVIEW_LIMIT) -> str:
    text = strip_ansi(_extract_text_from_renderable(value)).replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _preview_chunk(chunk: Any) -> str:
    if isinstance(chunk, dict):
        text = chunk.get("text")
        if text:
            return _preview_text(text)
        diff = chunk.get("diff")
        if diff:
            try:
                normalized = _normalize_stream_diff(diff)
            except Exception:
                normalized = []
            parts = []
            for item in normalized:
                if not isinstance(item, dict):
                    continue
                if item.get("op") not in {"append", "replace"}:
                    continue
                part_text = item.get("text")
                if part_text:
                    parts.append(str(part_text))
            if parts:
                return _preview_text(" | ".join(parts))
            return _preview_text(diff)
    return _preview_text(chunk)


_TAG_HIGHLIGHT_RE = re.compile(r'(</?[\w-]+(?:\s+[^>]*?)?\s*/?>)')


def _highlight_tags(text: str) -> "Text":
    result = Text()
    for token in _TAG_HIGHLIGHT_RE.split(text):
        if not token:
            continue
        if _TAG_HIGHLIGHT_RE.fullmatch(token):
            result.append(token, style="bold magenta")
        else:
            result.append(token)
    return result


try:
    from rich import box as rich_box
    from rich.console import Console, Group
    from rich.markdown import Markdown
    from rich.markup import escape as markup_escape
    from rich.panel import Panel
    from rich.live import Live
    from rich.padding import Padding
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

import quimera.themes as themes
from quimera.themes import ROLE_STYLES, DEFAULT_DENSITY

# Sentinela para parar o writer thread
_STOP = object()


# ---------------------------------------------------------------------------
# Eventos tipados
# ---------------------------------------------------------------------------

@dataclass
class PrintEvent:
    renderable: Any
    kwargs: dict = field(default_factory=dict)
    kind: str = "generic"


@dataclass
class LiveStartEvent:
    agent: str


@dataclass
class LiveUpdateChunkEvent:
    agent: str
    chunk: Any


@dataclass
class LiveStopEvent:
    agent: str
    final_content: str
    render_mode: str = "auto"


@dataclass
class LiveAbortEvent:
    agent: str


@dataclass
class NoopEvent:
    done: threading.Event
    force_flush: bool = False


@dataclass
class ToolbarTickEvent:
    """Dispara refresh da toolbar para atualizar contador de tempo."""


@dataclass
class OutputControlEvent:
    suspend: bool
    done: threading.Event | None = None


@dataclass
class TransientWindowEvent:
    """Substitui a janela transient no modo prompt ativo com substituição in-place."""
    text: str
    count: int
    buf_version: int = 0


@dataclass
class TransientClearEvent:
    """Limpa a janela transient no modo prompt ativo."""
    buf_version: int = 0


@dataclass
class TerminalResizeEvent:
    """Terminal foi redimensionado — reseta contador de linhas do overlay."""


@dataclass
class PendingInputEvent:
    """Sinaliza que um container aguarda input do usuário (aprovação ou pergunta).

    Atualiza os campos ``pending_kind`` / ``pending_question`` do AgentContainer e
    dispara um refresh do Live para exibir o badge inline enquanto o agente está em
    streaming. Quando ``kind`` for vazio, limpa o estado pendente.
    """

    agent: str
    kind: str     # "approval" | "ask" | ""
    question: str = ""


# ---------------------------------------------------------------------------
# Container por agente (janela vertical)
# ---------------------------------------------------------------------------

@dataclass
class AgentContainer:
    """Container (janela vertical) de um agente — dono do próprio output e perguntas.

    Modelo Windows-OS: cada agente ativo é uma janela empilhada na vertical.
    O container gerencia ativamente:

      output    -> stream ao vivo (iniciar/atualizar/finalizar/abortar) +
                   buffer rolling de progresso transitório.
      perguntas -> input/aprovação/seleção emoldurados sob o banner do agente
                   (commit do transient + request_floor + render + read + release).

    O stream ao vivo atualiza `stream_content` diretamente no container via
    writer thread; é a fonte de verdade para leitura externa (ex.: ao compor
    uma pergunta durante o streaming).
    """

    agent: str
    label: str
    style: str
    streaming: bool = False
    stream_content: str = ""
    stream_theme_name: str = ""
    transient_active: bool = False
    elapsed: float | None = None
    transient: list[str] = field(default_factory=list)
    pending_kind: str = ""      # "" | "approval" | "ask"
    pending_question: str = ""  # resumo da pergunta/tool para exibição inline no Live

    def compose_question(self, question: str, options: list[str] | None = None) -> str:
        """Monta o corpo textual da pergunta exibida sob o banner do agente."""
        lines = [strip_ansi(str(question or "")).strip()]
        for i, opt in enumerate(options or []):
            lines.append(f"  {i + 1}. {strip_ansi(str(opt))}")
        return "\n".join(line for line in lines if line)

    # -- Output management ------------------------------------------------

    def start_stream(self, renderer, theme_name: str) -> None:
        """Inicia streaming de output para este agente."""
        if self.streaming:
            return
        self.streaming = True
        self.stream_content = ""
        self.stream_theme_name = theme_name
        renderer._queue.put(LiveStartEvent(self.agent))

    def update_stream(self, renderer, chunk: Any) -> None:
        """Enfileira chunk de streaming."""
        renderer._queue.put(LiveUpdateChunkEvent(self.agent, chunk))

    def finish_stream(self, renderer, final_content: str, render_mode: str = "auto") -> None:
        """Finaliza streaming e persiste conteúdo completo."""
        self.streaming = False
        clean = strip_ansi(str(final_content or ""))
        mode = _normalize_render_mode(render_mode)
        with renderer._lock:
            renderer._completed_streams[self.agent] = clean
        renderer._queue.put(LiveStopEvent(self.agent, clean, mode))

    def abort_stream(self, renderer) -> None:
        """Aborta streaming sem marcar como completo."""
        self.streaming = False
        renderer._queue.put(LiveAbortEvent(self.agent))

    # -- Transient management --------------------------------------------

    def push_transient(self, message: str) -> bool:
        """Adiciona mensagem ao buffer rolling do container. Retorna True se mudou."""
        clean = strip_ansi(str(message or "")).strip("\r\n")
        if not clean:
            return False
        if self.transient and self.transient[-1] == clean:
            return False
        self.transient.append(clean)
        self.transient = self.transient[-_SCROLLING_WINDOW_SIZE:]
        return True

    def clear_transient_buffer(self) -> None:
        """Esvazia o buffer transitório e desativa flag."""
        self.transient.clear()
        self.transient_active = False

    # -- Question management ----------------------------------------------

    def ask_input(self, renderer, input_gate, prompt: str, timeout: float = 300.0) -> str | None:
        """Input livre de texto dentro deste container.

        Commit do transient, request_floor, render pergunta com banner,
        leitura da resposta, release_floor.
        """
        renderer.clear_agent_transient(self.agent)
        composed = self.compose_question(prompt)
        if self.streaming:
            renderer.set_agent_pending_input(self.agent, "ask", composed)
        renderer.flush_quick()

        try:
            return input_gate.read_input_in_terminal(
                composed + "\n", timeout
            )
        finally:
            if self.streaming:
                renderer.clear_agent_pending_input(self.agent)

    def ask_approval(self, renderer, input_gate, question: str,
                     prompt: str = "", timeout: float = 300.0) -> str | None:
        """Aprovação y/n/a dentro deste container.

        Fluxo:
        1. Se streaming ativo: exibe badge inline no Live via ``set_agent_pending_input``.
        2. Antes de ``request_floor``: imprime o card de aprovação permanentemente no
           scrollback usando ``console.print()`` diretamente (enquanto o renderer está
           suspenso e o chão foi cedido ao chamador). O card permanece visível mesmo
           após o Live fechar.
        3. ``request_floor`` → Live fecha → apenas o prompt ``[y/N/a]`` é exibido em
           modo raw.
        4. Limpa o badge pendente ao finalizar.
        """
        renderer.clear_agent_transient(self.agent)
        composed = self.compose_question(question)
        if self.streaming:
            renderer.set_agent_pending_input(self.agent, "approval", composed)
        renderer.flush_quick()

        try:
            return input_gate.read_approval_in_terminal(
                composed, prompt, timeout
            )
        finally:
            if self.streaming:
                renderer.clear_agent_pending_input(self.agent)

    def ask_selection(self, renderer, input_gate, question: str,
                      options: list[str], timeout: float = 300.0) -> tuple[int, str] | None:
        """Seleção numerada dentro deste container.

        O reader (`read_selection_in_terminal`) já renderiza as opções numeradas;
        compomos apenas o texto da pergunta (sem opções) para não duplicá-las.
        """
        renderer.clear_agent_transient(self.agent)
        renderer.flush_quick()
        return input_gate.read_selection_in_terminal(self.compose_question(question), options, timeout)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_agent_name(agent: Any) -> str:
    """Normaliza identificador de agente para uso seguro na UI."""
    if isinstance(agent, str):
        candidate = strip_ansi(agent).strip()
    elif agent is None:
        candidate = ""
    else:
        candidate = strip_ansi(str(agent)).strip()
    return candidate or "unknown"


def _agent_style(agent: str, get_plugin_style=None):
    """Retorna (color, label) para o agente; fallback para white/capitalize."""
    agent_name = _coerce_agent_name(agent)
    if get_plugin_style:
        result = get_plugin_style(agent_name.lower())
        if result:
            return result
    return ("white", f"🤖  {agent_name.capitalize()}")


class _NullStatus:
    """Proxy seguro que substitui nullcontext(None) quando não há spinner ativo."""

    def update(self, text: str = "") -> None:  # noqa: ARG002
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _AuditConsoleFile:
    """Duplica a escrita do console para o terminal real e para o audit logger."""

    def __init__(self, wrapped, audit_logger: RenderAuditLogger):
        self._wrapped = wrapped
        self._audit_logger = audit_logger

    def write(self, data):
        written = self._wrapped.write(data)
        try:
            self._audit_logger.write_ansi(data)
        except Exception:
            _log.exception("audit logger: failed to record ANSI output")
        return written

    def flush(self):
        return self._wrapped.flush()

    def isatty(self):
        return self._wrapped.isatty()

    def fileno(self):
        return self._wrapped.fileno()

    @property
    def encoding(self):
        return getattr(self._wrapped, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._wrapped, "errors", "replace")


# ---------------------------------------------------------------------------
# Overlay transitório
# ---------------------------------------------------------------------------

class _TransientOverlay:
    """Gerencia o overlay de progresso acima do prompt (modo prompt ativo).

    Layout alvo:
        [histórico permanente]
        ┄ overlay N linhas ┄    ← este módulo
        > input do usuário      ← prompt_toolkit
        [bottom_toolbar]        ← prompt_toolkit

    Invariante fundamental:
        _lines[0] == número de linhas ATUALMENTE na tela.
        As closures de replace SEMPRE apagam as linhas anteriores, mesmo
        quando a closure é stale (versão obsoleta), evitando ghosting onde
        o overlay antigo permanece visível e o novo é impresso logo abaixo.
    """

    def __init__(self, shared_lines: list | None = None):
        # Lista mutável compartilhada entre closures — estado real da tela.
        # Quando o Compositor fornece a lista, o contador fica visível a outras
        # threads (ex.: o detentor do "chão" que precisa limpar o overlay).
        self._lines = shared_lines if shared_lines is not None else [0]

    @property
    def lines_on_screen(self) -> int:
        return self._lines[0]

    def reset(self):
        """Reseta contador (ex: terminal redimensionado)."""
        self._lines[0] = 0

    def build_replace(self, text: str, version: int, get_version_fn, audit_fn=None):
        """Constrói closure para substituir o overlay via run_in_terminal.

        A closure SEMPRE apaga linhas anteriores (mesmo se stale) para evitar
        que o overlay antigo fique na tela enquanto o novo é impresso abaixo.
        Só escreve novo conteúdo quando a versão ainda é atual.
        """
        lines = self._lines

        def _replace():
            p = lines[0]
            _th = shutil.get_terminal_size(fallback=(80, 24)).lines
            # Cursor-up limitado: deixa margem de 3 linhas para prompt+toolbar+espaço
            clamped_p = min(p, max(0, _th - 3))
            current_ver = get_version_fn()

            if audit_fn:
                audit_fn(
                    "transient_replace",
                    buf_version=version,
                    prev_lines=p,
                    cursor_up=clamped_p,
                    term_lines=_th,
                    stale=(version < current_ver),
                )

            # SEMPRE apaga overlay anterior — previne ghosting em closures stale
            if clamped_p > 0:
                sys.stdout.write(f"\033[{clamped_p}A\033[J")
            lines[0] = 0

            # Closure stale: overlay foi apagado, mas não escreve conteúdo novo
            if version < current_ver:
                if clamped_p > 0:
                    sys.stdout.flush()
                return

            # Limita overlay a 1/3 do terminal para não ultrapassar a tela
            max_visible = max(1, (_th - 3) // 3)
            visible_lines = text.split('\n')
            if len(visible_lines) > max_visible:
                visible_lines = visible_lines[-max_visible:]
            actual_text = '\n'.join(visible_lines)
            actual_count = len(visible_lines)

            sys.stdout.write(f"\033[2m{actual_text}\033[0m")
            sys.stdout.write('\n')
            sys.stdout.flush()
            lines[0] = actual_count

        return _replace

    def build_clear(self, version: int, get_version_fn, audit_fn=None):
        """Constrói closure para limpar o overlay via run_in_terminal."""
        lines = self._lines

        def _clear():
            if version < get_version_fn():
                return
            p = lines[0]
            lines[0] = 0
            if audit_fn:
                audit_fn("transient_clear", buf_version=version, prev_lines=p)
            if p > 0:
                sys.stdout.write(f"\033[{p}A\033[J")
                sys.stdout.flush()

        return _clear

    def build_print_above(self, renderable, kwargs, console, bump_version_fn, audit_fn=None):
        """Constrói closure para imprimir permanentemente, limpando o overlay antes.

        Ao ser executada (via run_in_terminal), a closure:
        1. Apaga o overlay existente
        2. Bumpa a versão — invalida closures _replace pendentes
        3. Imprime o conteúdo permanente via Rich Console
        """
        lines = self._lines

        def _clear_and_print():
            p = lines[0]
            lines[0] = 0
            # Invalida closures _replace já agendadas antes deste print
            bump_version_fn()
            if p > 0:
                sys.stdout.write(f"\033[{p}A\033[J")
            console.print(renderable, **kwargs)

        return _clear_and_print


# ---------------------------------------------------------------------------
# TerminalRenderer
# ---------------------------------------------------------------------------

class TerminalRenderer:
    """Camada exclusiva de apresentação no terminal. Nunca toca em persistência."""

    supports_agent_feed = True

    def __init__(
        self,
        theme: str | None = None,
        get_plugin_style=None,
        density: str | None = None,
        audit_logger: RenderAuditLogger | None = None,
    ):
        """Inicializa uma instância de TerminalRenderer."""
        self._audit_logger = audit_logger
        console_file = sys.stdout
        if self._audit_logger is not None:
            console_file = _AuditConsoleFile(console_file, self._audit_logger)
        public_ui = _public_ui_module()
        if public_ui._RICH_AVAILABLE:
            self._console = public_ui.Console(
                force_terminal=_is_interactive_terminal(),
                no_color=False,
                file=console_file,
            )
        else:
            self._console = None
        self._theme = themes.get(theme or themes.DEFAULT_THEME)
        self._density = density if density in themes.DENSITY_OPTIONS else DEFAULT_DENSITY
        self._get_plugin_style = get_plugin_style
        self._live = None
        self._statuses = {}

        # Tempo decorrido por agente (atualizado via _on_tick, lido na toolbar)
        self._agent_elapsed: dict[str, float] = {}

        # Streams completados: agent -> final_content (cache de dedup do render final)
        self._completed_streams = {}
        # Registro de containers por agente — dono do estado de output e perguntas.
        # Consolida o que antes eram dicionários/sets paralelos: stream ativo,
        # progresso transitório, buffer rolling e tempo decorrido.
        self._containers: dict[Any, AgentContainer] = {}
        # Último evento persistente impresso, para evitar espaçamento redundante.
        self._last_persistent_kind: str | None = None
        self._last_persistent_agent: str | None = None
        # Lock protege _completed_streams, _containers, _statuses e versão
        self._lock = threading.RLock()

        # Contador de linhas do overlay transitório na tela, compartilhado com
        # o _TransientOverlay do writer thread. Legível por quem detém o "chão"
        # (request_floor) para limpar o overlay de forma síncrona.
        self._overlay_lines = [0]

        # Versão monotônica do overlay — usada para invalidar closures stale
        self._transient_buf_version = 0
        # Último texto combinado enfileirado — dedup para não enfileirar TWEs idênticos
        self._last_combined_text: str | None = None

        # Flag: writer thread tem um Live ativo
        self._stream_live_active = threading.Event()

        # Hooks de integração com prompt_toolkit
        self._is_prompt_active_fn = None  # () -> bool
        self._run_above_prompt_fn = None  # (callable) -> bool
        self._output_suspended = threading.Event()

        self._queue: _queue_module.Queue = _queue_module.Queue(maxsize=512)
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()
        if self._audit_logger is not None:
            self._audit_logger.start_queue_sampler(self._queue)

        # SIGWINCH: sinaliza resize para o writer thread resetar contador do overlay
        try:
            import signal as _signal
            _prev_sigwinch = _signal.getsignal(_signal.SIGWINCH)

            def _on_sigwinch(signum, frame):
                try:
                    self._queue.put_nowait(TerminalResizeEvent())
                except _queue_module.Full:
                    pass
                if callable(_prev_sigwinch) and _prev_sigwinch not in (
                    _signal.SIG_DFL, _signal.SIG_IGN
                ):
                    _prev_sigwinch(signum, frame)

            _signal.signal(_signal.SIGWINCH, _on_sigwinch)
        except (AttributeError, OSError):
            pass  # SIGWINCH não disponível nesta plataforma

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def set_prompt_integration(self, is_active_fn, run_above_fn) -> None:
        """Integra o renderer com prompt_toolkit para evitar corrupção visual.

        is_active_fn: callable que retorna True quando o prompt_toolkit está ativo (>>>).
        run_above_fn: callable(callback) -> bool que executa callback acima do prompt ativo.
        """
        self._is_prompt_active_fn = is_active_fn
        self._run_above_prompt_fn = run_above_fn

    def close(self, timeout: float = 5.0) -> None:
        """Encerra o writer thread graciosamente, aguardando eventos pendentes."""
        self._queue.put(_STOP)
        self._writer_thread.join(timeout=timeout)
        if self._audit_logger is not None:
            self._audit_logger.close()

    def log_debug_event(self, event: str, **payload) -> None:
        """Expõe auditoria estruturada para camadas superiores."""
        if self._audit_logger is None:
            return
        self._audit_logger.log_event(event, **payload)

    def __del__(self):
        try:
            if self._writer_thread.is_alive():
                self._queue.put_nowait(_STOP)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------

    def _writer_loop(self):
        """Writer thread único: processa todos os eventos de UI sequencialmente.

        Dois modos de exibição:
        - LIVE (agente respondendo): Rich.Live ativo, prompt_toolkit suspenso
          [histórico] → [streaming + infobar] ← Rich.Live gerencia

        - PROMPT (aguardando input): prompt_toolkit ativo, overlay via run_in_terminal
          [histórico] → [overlay N linhas] → [input] → [toolbar]
          overlay gerenciado por _TransientOverlay com closures run_in_terminal
        """
        _stream_containers: dict[str, AgentContainer] = {}
        _ul: list = [None]  # Live unificado ativo (ou None)
        _local_pending: collections.deque = collections.deque()
        _deferred_post_prompt: collections.deque = collections.deque()

        # Overlay transitório — único ponto de verdade para o estado da tela.
        # Compartilha o contador de linhas com a instância (self._overlay_lines)
        # para que request_floor possa limpá-lo sincronamente de outra thread.
        _overlay = _TransientOverlay(self._overlay_lines)

        def _prompt_active() -> bool:
            try:
                return bool(self._is_prompt_active_fn and self._is_prompt_active_fn())
            except Exception:
                return False

        def _get_version() -> int:
            with self._lock:
                return self._transient_buf_version

        def _bump_version() -> int:
            with self._lock:
                self._transient_buf_version += 1
                return self._transient_buf_version

        def _audit(event_name: str, **payload) -> None:
            self.log_debug_event(event_name, **payload)

        # -- Live helpers --------------------------------------------------

        def _get_renderable():
            if not _stream_containers:
                return Text("")
            parts = []
            for c in _stream_containers.values():
                stream_block = self._build_stream_renderable(
                    c.stream_theme_name, c.label, c.style, c.stream_content
                )
                if c.pending_kind:
                    pending_card = self._build_pending_card_renderable(c)
                    parts.append(Group(stream_block, pending_card))
                else:
                    parts.append(stream_block)
            main = Group(*parts) if len(parts) > 1 else parts[0]
            if self._density == "compact":
                return main
            if len(parts) > 1:
                labels = " · ".join(
                    _agent_toolbar_label(agent, c.label)
                    for agent, c in _stream_containers.items()
                )
                toolbar_text = f"[dim]{labels} · Ctrl+C para cancelar[/dim]"
            else:
                only_agent, only_c = next(iter(_stream_containers.items()))
                label_text = _agent_toolbar_label(only_agent, only_c.label)
                toolbar_text = (
                    f"[bold {only_c.style}]{label_text}[/] "
                    f"[dim]· Ctrl+C para cancelar[/dim]"
                )
            infobar = Rule(toolbar_text, characters="·", style="dim")
            return Group(main, infobar)

        def _agent_toolbar_label(agent_name: str, base_label: str) -> str:
            elapsed = self._get_agent_elapsed(agent_name)
            if elapsed is not None:
                return f"{markup_escape(base_label)} [{int(elapsed)}s]"
            return markup_escape(base_label)

        def _ensure_live():
            if _ul[0] is None and self._console:
                # Não inicia Live quando prompt_toolkit está ativo — os dois
                # controladores de terminal conflitam e corrompem o display.
                if _prompt_active() or self._output_suspended.is_set():
                    return
                _ul[0] = _public_ui_module().Live(
                    _get_renderable(),
                    console=self._console,
                    refresh_per_second=8,
                    transient=True,
                    auto_refresh=False,
                )
                _ul[0].start()
                self._stream_live_active.set()

        def _refresh():
            if _ul[0] is not None and not self._output_suspended.is_set():
                _ul[0].update(_get_renderable(), refresh=True)

        def _close_live():
            """Encerra o Live ativo sem refresh vazio residual."""
            if _ul[0] is not None:
                _ul[0].stop()
                _ul[0] = None
                self._stream_live_active.clear()

        def _stop_if_empty():
            if not _stream_containers:
                _close_live()

        # -- Print helpers -------------------------------------------------

        def _cprint(renderable, **kwargs):
            """Imprime via Live ativo, run_above (se prompt ativo) ou direto ao console."""
            if self._output_suspended.is_set():
                _deferred_post_prompt.append((renderable, kwargs))
                return
            if _ul[0] is not None:
                _ul[0].console.print(renderable, **kwargs)
                return
            if self._console is None:
                return
            run_above = self._run_above_prompt_fn
            if run_above is not None:
                cb = _overlay.build_print_above(renderable, kwargs, self._console, _bump_version, _audit)
                if run_above(cb):
                    _flush_deferred()
                    return
            _deferred_post_prompt.append((renderable, kwargs))

        def _flush_deferred(force=False):
            """Drena prints deferidos enquanto prompt estava ativo ou saída suspensa."""
            if self._output_suspended.is_set():
                return
            run_above = self._run_above_prompt_fn
            while _deferred_post_prompt:
                _r, _k = _deferred_post_prompt.popleft()
                if _ul[0] is not None:
                    _ul[0].console.print(_r, **_k)
                elif force and self._console:
                    self._console.print(_r, **_k)
                elif run_above is not None:
                    cb = _overlay.build_print_above(_r, _k, self._console, _bump_version, _audit)
                    if not run_above(cb):
                        _deferred_post_prompt.appendleft((_r, _k))
                        break
                else:
                    _deferred_post_prompt.appendleft((_r, _k))
                    break

        # -- Event loop ----------------------------------------------------

        def _next_event():
            if _local_pending:
                return _local_pending.popleft()
            return self._queue.get()

        while True:
            event = _next_event()
            if event is _STOP:
                _flush_deferred(force=True)
                _close_live()
                break

            try:
                if isinstance(event, PrintEvent):
                    preview = _preview_text(event.renderable)
                    if preview:
                        _audit(
                            "print",
                            kind=event.kind,
                            prompt_active=_prompt_active(),
                            preview=preview,
                        )
                    _cprint(event.renderable, **event.kwargs)

                elif isinstance(event, LiveStartEvent):
                    _audit("stream_start", agent=event.agent, prompt_active=_prompt_active())
                    with self._lock:
                        container = self._containers.get(event.agent)
                    if container:
                        _stream_containers[event.agent] = container
                        if container.stream_content.strip():
                            _ensure_live()
                            _refresh()

                elif isinstance(event, LiveUpdateChunkEvent):
                    # Coalescing: drena chunks consecutivos de todos os agentes para um único _refresh()
                    batches = collections.defaultdict(list)
                    batches[event.agent].append(event.chunk)
                    while True:
                        try:
                            next_ev = self._queue.get_nowait()
                        except _queue_module.Empty:
                            break
                        if isinstance(next_ev, LiveUpdateChunkEvent):
                            batches[next_ev.agent].append(next_ev.chunk)
                        else:
                            _local_pending.appendleft(next_ev)
                            break

                    for _a, chunks in batches.items():
                        container = _stream_containers.get(_a)
                        if container:
                            for chunk in chunks:
                                if isinstance(chunk, dict):
                                    container.stream_content = _apply_stream_diff(
                                        container.stream_content,
                                        _normalize_stream_diff(chunk.get("diff"))
                                    )
                                    text = chunk.get("text")
                                    if text and not chunk.get("diff"):
                                        container.stream_content += strip_ansi(str(text))
                                else:
                                    container.stream_content += strip_ansi(str(chunk))
                            _audit(
                                "stream_chunk",
                                agent=_a,
                                chunk_count=len(chunks),
                                preview=_preview_chunk(chunks[-1]),
                                previews=[_preview_chunk(c) for c in chunks[:5]],
                                previews_truncated=len(chunks) > 5,
                            )
                    if batches:
                        has_visible_content = any(
                            container.stream_content.strip()
                            for container in _stream_containers.values()
                        )
                        if has_visible_content:
                            _ensure_live()
                        _refresh()

                elif isinstance(event, LiveStopEvent):
                    _audit(
                        "stream_stop",
                        agent=event.agent,
                        render_mode=event.render_mode,
                        preview=_preview_text(event.final_content),
                    )
                    container = _stream_containers.pop(event.agent, None)
                    if container:
                        if _stream_containers:
                            _refresh()
                        _stop_if_empty()
                        final_block = self._render_turn_block(
                            container.stream_theme_name, container.label, container.style,
                            content=event.final_content,
                            include_header=True,
                            include_footer_rule=True,
                            render_mode=event.render_mode,
                        )
                        _cprint(final_block)

                elif isinstance(event, LiveAbortEvent):
                    _audit("stream_abort", agent=event.agent)
                    _stream_containers.pop(event.agent, None)
                    if _stream_containers:
                        _refresh()
                    _stop_if_empty()

                elif isinstance(event, NoopEvent):
                    _flush_deferred(force=event.force_flush)
                    event.done.set()

                elif isinstance(event, ToolbarTickEvent):
                    # Drena todos os ticks acumulados para um único _refresh()
                    while True:
                        try:
                            next_ev = self._queue.get_nowait()
                        except _queue_module.Empty:
                            break
                        if isinstance(next_ev, ToolbarTickEvent):
                            pass  # descarta ticks extras, refresca uma vez só
                        else:
                            _local_pending.appendleft(next_ev)
                            break
                    _refresh()

                elif isinstance(event, OutputControlEvent):
                    if event.suspend:
                        self._output_suspended.set()
                        _close_live()
                    else:
                        self._output_suspended.clear()
                        _flush_deferred(force=True)
                        # Restaura o Live se o agente ainda está em streaming.
                        # stream_content acumulado durante a suspensão (ex.: ask_user)
                        # já está no container; reativar aqui evita tela em branco até
                        # o próximo chunk. Só inicia se há conteúdo visível para não
                        # abrir Live vazio desnecessariamente.
                        if _stream_containers and any(
                            container.stream_content.strip()
                            for container in _stream_containers.values()
                        ):
                            _ensure_live()
                            _refresh()
                    if event.done is not None:
                        event.done.set()

                elif isinstance(event, TerminalResizeEvent):
                    # Resize invalida a contagem de linhas do overlay —
                    # o cursor-up usaria um valor obsoleto e corromperia o display.
                    _overlay.reset()

                elif isinstance(event, PendingInputEvent):
                    # Atualiza o estado pendente do container e dispara refresh do Live
                    # para exibir (ou limpar) o badge de aprovação inline.
                    with self._lock:
                        container = self._containers.get(event.agent)
                        if container is not None:
                            container.pending_kind = event.kind
                            container.pending_question = event.question
                    if event.agent in _stream_containers:
                        _refresh()

                elif isinstance(event, TransientWindowEvent):
                    # Floor cedido (request_floor): um prompt/leitor é dono do
                    # terminal. NÃO desenhar overlay por cima — era exatamente
                    # isso que atropelava o prompt de pergunta/aprovação.
                    if self._output_suspended.is_set():
                        continue
                    # Coalescing: drena eventos consecutivos — o mais recente substitui os anteriores
                    _coalesced = 0
                    while True:
                        try:
                            _next = self._queue.get_nowait()
                        except _queue_module.Empty:
                            break
                        if isinstance(_next, TransientWindowEvent):
                            event = _next
                            _coalesced += 1
                        else:
                            _local_pending.appendleft(_next)
                            break

                    if _coalesced > 0:
                        _audit("transient_coalesced", count=_coalesced, buf_version=event.buf_version)

                    with self._lock:
                        current_ver = self._transient_buf_version

                    # Evento stale: verifica se ainda há overlay na tela para limpar
                    if event.buf_version < current_ver:
                        if _overlay.lines_on_screen > 0:
                            run_above = self._run_above_prompt_fn
                            if run_above is not None:
                                run_above(_overlay.build_clear(current_ver, _get_version, _audit))
                        continue

                    if not event.text:
                        continue

                    run_above = self._run_above_prompt_fn
                    cb = _overlay.build_replace(event.text, event.buf_version, _get_version, _audit)
                    if run_above is not None:
                        run_above(cb)
                    elif self._console:
                        self._console.print(event.text)

                elif isinstance(event, TransientClearEvent):
                    # Floor cedido: nada a (re)desenhar; o detentor do chão já
                    # limpou o overlay em request_floor.
                    if self._output_suspended.is_set():
                        continue
                    with self._lock:
                        current_ver = self._transient_buf_version
                    if event.buf_version < current_ver:
                        continue

                    run_above = self._run_above_prompt_fn
                    cb = _overlay.build_clear(event.buf_version, _get_version, _audit)
                    if run_above is not None:
                        run_above(cb)

            except Exception:
                _log.exception("writer thread: erro ao processar evento %r", event)

    def flush(self, timeout: float = 5.0):
        """Aguarda o writer thread processar todos os eventos pendentes."""
        done = threading.Event()
        self._queue.put(NoopEvent(done, force_flush=True))
        if not done.wait(timeout=timeout):
            raise TimeoutError(f"TerminalRenderer.flush timed out after {timeout} seconds")

    def flush_quick(self, timeout: float = 0.15) -> bool:
        """Tenta drenar rapidamente sem bloquear o thread do prompt."""
        try:
            self.flush(timeout=timeout)
            return True
        except TimeoutError:
            return False

    def suspend_output(self, timeout: float = 2.0) -> bool:
        """Suspende temporariamente prints no terminal (ex.: editor externo ativo)."""
        self._output_suspended.set()
        done = threading.Event()
        self._queue.put(OutputControlEvent(suspend=True, done=done))
        return done.wait(timeout=timeout)

    def resume_output(self, timeout: float = 2.0) -> bool:
        """Retoma prints no terminal e drena saídas deferidas."""
        done = threading.Event()
        self._queue.put(OutputControlEvent(suspend=False, done=done))
        resumed = done.wait(timeout=timeout)
        if not resumed:
            self._output_suspended.clear()
        return resumed

    # ------------------------------------------------------------------
    # Floor (chão do terminal) — transição atômica de posse do stdout
    # ------------------------------------------------------------------
    #
    # O writer thread é o Compositor: único dono do stdout. Quando um leitor
    # interativo (pergunta/aprovação/seleção) precisa do terminal, ele "pede o
    # chão": o Compositor congela o feed (fecha o Live, para de pintar o overlay
    # transitório) e o leitor limpa os artefatos remanescentes antes de desenhar
    # sua própria janela. Ao terminar, devolve o chão e o feed volta a fluir.
    #
    # Isto formaliza a transição que antes estava implícita e quebrada
    # (suspend_output + overlay continuando a pintar por cima do prompt).

    def request_floor(self, timeout: float = 2.0) -> bool:
        """Cede o chão do terminal ao chamador (que vira o dono temporário do stdout).

        DEVE ser chamado de dentro de run_in_terminal — o chamador é, naquele
        instante, dono do terminal. Suspende o feed (Live + overlay transitório)
        e limpa as linhas de overlay que ainda estejam na tela, deixando o chão
        limpo para o prompt do leitor.
        """
        ok = self.suspend_output(timeout=timeout)
        # Após a suspensão, o writer não desenha mais overlay (handlers checam
        # _output_suspended). Limpamos sincronamente o que restou na tela.
        self._clear_overlay_sync()
        return ok

    def release_floor(self, timeout: float = 2.0) -> bool:
        """Devolve o chão ao Compositor: retoma o feed e drena prints deferidos."""
        return self.resume_output(timeout=timeout)

    def _clear_overlay_sync(self) -> None:
        """Apaga as linhas do overlay transitório direto no stdout.

        Seguro porque é chamado pelo detentor do chão (dentro de run_in_terminal,
        com o feed já suspenso). Invalida closures de replace pendentes via bump
        de versão para que não repintem o overlay ao retomar.
        """
        n = self._overlay_lines[0]
        if n > 0:
            try:
                sys.stdout.write(f"\033[{n}A\033[J")
                sys.stdout.flush()
            except Exception:
                pass
            self._overlay_lines[0] = 0
        with self._lock:
            self._transient_buf_version += 1
        self.log_debug_event("floor_request", prev_lines=n)

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    @property
    def theme_name(self) -> str:
        """Retorna o nome do tema ativo."""
        return self._theme.name

    def cycle_theme(self) -> str:
        """Avança para o próximo tema; retorna o nome do novo tema."""
        import quimera.themes as _themes_mod
        all_names = _themes_mod.names()
        try:
            idx = all_names.index(self._theme.name)
        except ValueError:
            idx = 0
        next_name = all_names[(idx + 1) % len(all_names)]
        self._theme = _themes_mod.get(next_name)
        return next_name

    def _agent_style(self, agent: str):
        """Retorna (color, label) para o agente."""
        return _public_ui_module()._agent_style(agent, self._get_plugin_style)

    def _container(self, agent) -> AgentContainer:
        """Get-or-create do container do agente (protegido por _lock reentrante)."""
        with self._lock:
            container = self._containers.get(agent)
            if container is None:
                style, label = self._agent_style(agent)
                container = AgentContainer(
                    agent=_coerce_agent_name(agent), label=label, style=style
                )
                self._containers[agent] = container
            return container

    def _combined_transient(self, term_lines: int) -> tuple[str, int]:
        """Empilha verticalmente o progresso transitório de todos os containers.

        Cada agente contribui suas linhas rolling sob o próprio label, em ordem
        estável de agente (o deck multi-agente). DEVE ser chamado com _lock retido.
        Retorna (texto_combinado, num_linhas) já limitado a 1/3 do terminal.
        """
        combined: list[str] = []
        for agt in sorted(self._containers, key=str):
            container = self._containers[agt]
            for msg in container.transient:
                combined.append(f"{container.label} {msg}")
        win_limit = max(1, term_lines // 3)
        combined = combined[-win_limit:]
        return "\n".join(combined), len(combined)

    def _print(self, renderable, kind: str = "generic", **kwargs):
        """Enfileira um evento de print para o writer thread."""
        self._queue.put(PrintEvent(renderable, kwargs, kind=kind))

    def _spacing(self):
        """Imprime linha em branco entre turnos; no-op em modo compact."""
        if self._density != "compact":
            self._print("", kind="spacing")

    def _remember_persistent_event(self, kind: str, agent: str | None = None) -> None:
        """Registra o último evento persistente impresso no histórico."""
        self._last_persistent_kind = kind
        self._last_persistent_agent = _coerce_agent_name(agent) if agent else None

    def _should_insert_message_spacing(self, agent: str | None = None) -> bool:
        """Decide se a próxima mensagem final deve abrir um novo bloco visual."""
        if self._density == "compact":
            return False
        if self._last_persistent_kind in {
            "plain",
            "error",
            "warning",
            "system",
            "system_neutral",
        }:
            return False
        if not agent:
            return True
        current_agent = _coerce_agent_name(agent)
        if self._last_persistent_agent != current_agent:
            return True
        return True

    def _build_turn_header(self, theme_name: str, label: str, style: str):
        """Monta cabeçalho de turno por tema."""
        if theme_name == "chat":
            header = Table.grid(expand=True, padding=(0, 1))
            header.add_column(width=2)
            header.add_column(ratio=1)
            header.add_row(Text("●", style=f"bold {style}"), Text(label, style=f"bold {style}"))
            return header
        if theme_name == "rule":
            return Rule(f"[bold {style}]{label}[/bold {style}]", style=f"dim {style}")
        if theme_name == "minimal":
            return Text(f"▶ {label}", style=f"bold {style}")
        if theme_name == "card":
            return Text(f"▎ {label}", style=f"bold {style}")
        if theme_name == "line":
            return Text(f"{label}", style=f"bold {style}")
        return Text(label, style=f"bold {style}")

    def _build_turn_body(
        self,
        theme_name: str,
        label: str,
        style: str,
        content: str,
        streaming: bool = False,
        render_mode: str = "auto",
    ):
        """Monta corpo textual do turno."""
        mode = _normalize_render_mode(render_mode)
        if mode == "auto":
            mode = "markdown"
        public_ui = _public_ui_module()
        if streaming or mode == "plain":
            body_content = Text(content or "", no_wrap=False, overflow="fold")
        else:
            body_content = public_ui.Markdown(content or "")
        if theme_name == "panel":
            title = f"[bold {style}]{label}[/bold {style}]" if streaming else None
            return public_ui.Panel(body_content, title=title, border_style=style, padding=(0, 1))
        if theme_name == "chat":
            return Padding(body_content, pad=(0, 0, 0, 4))
        if theme_name == "minimal":
            return Padding(body_content, pad=(0, 0, 0, 2))
        if theme_name == "card":
            from rich.panel import Panel as RichPanel
            return RichPanel(body_content, border_style=f"dim {style}", padding=(0, 1))
        if theme_name == "line":
            return body_content
        return body_content

    def _build_turn_tools(self, theme_name: str, label: str, style: str, tools_table, turn_id: str):
        """Monta seção de ferramentas mantendo vínculo visual com o turno."""
        title = "tools"
        if turn_id:
            title = f"tools · {turn_id}"
        if theme_name == "panel":
            return Panel(
                tools_table,
                title=f"[bold {style}]{label} · {title}[/bold {style}]",
                border_style=style,
                padding=(0, 0),
            )
        if theme_name == "chat":
            row = Table.grid(expand=True, padding=(0, 1))
            row.add_column(width=2)
            row.add_column(ratio=1)
            row.add_row(
                Text("◦", style=f"dim {style}"),
                Group(
                    Text(title, style=f"bold {style}"),
                    Padding(tools_table, pad=(0, 0, 0, 2)),
                ),
            )
            return row
        if theme_name == "rule":
            return Group(Text(title, style=f"bold {style}"), tools_table)
        if theme_name == "minimal":
            return Group(Text(f"◦ {title}", style=f"bold {style}"), Padding(tools_table, pad=(0, 0, 0, 2)))
        if theme_name == "card":
            from rich.panel import Panel as RichPanel
            return RichPanel(tools_table, border_style=f"dim {style}", padding=(0, 1),
                             title=f"[bold {style}]tools · {turn_id}[/bold {style}]" if turn_id else None)
        if theme_name == "line":
            return Group(Text(title, style=f"bold {style}"), tools_table)
        return tools_table

    def _render_turn_block(
        self,
        theme_name: str,
        label: str,
        style: str,
        *,
        content: str | None = None,
        tools_table=None,
        turn_id: str = "",
        include_header: bool = True,
        include_footer_rule: bool = False,
        streaming: bool = False,
        render_mode: str = "auto",
    ):
        """Monta bloco estruturado de turno: header -> corpo -> tools."""
        parts = []
        if include_header:
            parts.append(self._build_turn_header(theme_name, label, style))
        if content:
            parts.append(
                self._build_turn_body(
                    theme_name,
                    label,
                    style,
                    content,
                    streaming=streaming,
                    render_mode=render_mode,
                )
            )
        if tools_table is not None:
            parts.append(self._build_turn_tools(theme_name, label, style, tools_table, turn_id))
        if include_footer_rule and theme_name == "rule":
            parts.append(Rule(style="dim"))
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return Group(*parts)

    def _build_stream_renderable(self, theme_name: str, label: str, style: str, content: str):
        """Monta o renderable dinâmico usado no streaming (header incluso no bloco live)."""
        return self._render_turn_block(
            theme_name,
            label,
            style,
            content=content,
            include_header=True,
            streaming=True,
        )

    def _build_pending_card_renderable(self, container: "AgentContainer"):
        """Monta badge inline de aprovação/input pendente para exibição no Live.

        Aparece abaixo do stream_content do agente enquanto o Live está ativo,
        dando contexto visual de que o agente aguarda uma resposta do usuário.
        """
        icon = "⚠" if container.pending_kind == "approval" else "❓"
        question = container.pending_question.strip()
        first_line = question.splitlines()[0] if question else "aguardando aprovação"
        content = Text.assemble(
            (f"\n{icon} ", "bold yellow"),
            (first_line, "bold yellow"),
            ("\n  Executar? [y/N/a=todas]\n", "dim yellow"),
        )
        return Padding(content, pad=(0, 0, 0, 2))

    def _build_approval_card_renderable(
        self, label: str, style: str, question: str, kind: str = "approval"
    ):
        """Monta card de aprovação/input para impressão permanente no scrollback.

        Chamado de dentro de ``run_in_terminal`` (com o renderer suspenso e o
        chão cedido ao chamador): imprime via ``console.print()`` diretamente,
        garantindo que o card permaneça visível mesmo após o Live fechar.
        """
        public_ui = _public_ui_module()
        if not public_ui._RICH_AVAILABLE:
            return None
        icon = "⚠" if kind == "approval" else "❓"
        lines = question.strip().splitlines()
        content = Text()
        for i, line in enumerate(lines):
            if i > 0:
                content.append("\n")
            content.append(line, "bold yellow" if i == 0 else "dim")
        return public_ui.Panel(
            content,
            title=f"[bold {style}]{markup_escape(label)}[/] [dim]· aprovação pendente[/]",
            border_style="yellow",
            padding=(0, 1),
        )

    def set_agent_pending_input(self, agent: str, kind: str, question: str = "") -> None:
        """Sinaliza que o agente aguarda input — exibe badge inline no Live (se ativo).

        Atualiza o campo ``pending_kind`` / ``pending_question`` do container
        do agente e dispara um refresh do Live para que o badge apareça
        imediatamente enquanto o agente estiver em streaming.
        """
        with self._lock:
            container = self._containers.get(agent)
            if container is not None:
                container.pending_kind = kind
                container.pending_question = question
        self._queue.put(PendingInputEvent(agent, kind, question))

    def clear_agent_pending_input(self, agent: str) -> None:
        """Remove o badge de input pendente do agente."""
        self.set_agent_pending_input(agent, "", "")

    # ------------------------------------------------------------------
    # API pública de exibição de mensagens
    # ------------------------------------------------------------------

    def show_message(self, agent, content, render_mode: str = "auto"):
        """Exibe mensagem usando o tema ativo."""
        self.clear_agent_transient(agent)
        style, label = self._agent_style(agent)
        clean_content = strip_ansi(_extract_text_from_renderable(content))
        if self._consume_completed_stream(agent, clean_content):
            return
        if self._console:
            theme_name = self._theme.name
            if self._should_insert_message_spacing(agent):
                self._spacing()
            block = self._render_turn_block(
                theme_name,
                label,
                style,
                content=clean_content,
                include_header=True,
                include_footer_rule=True,
                render_mode=render_mode,
            )
            self._print(block, kind="message")
            self._remember_persistent_event("message", agent)
        else:
            print(f"\n{label}: {clean_content}\n")

    def _consume_completed_stream(self, agent, content: str) -> bool:
        """Evita render final duplicado quando a resposta já foi exibida via streaming."""
        normalized = _normalize_completed_content(content)
        with self._lock:
            previous = self._completed_streams.get(agent)
            if previous is None:
                return False
            del self._completed_streams[agent]
        return _normalize_completed_content(previous) == normalized

    # ------------------------------------------------------------------
    # API pública de streaming
    # ------------------------------------------------------------------

    def start_message_stream(self, agent):
        """Inicia a área de renderização incremental para uma resposta."""
        if not self._console:
            return
        with self._lock:
            container = self._container(agent)
            container.start_stream(self, self._theme.name)

    def update_message_stream(self, agent, chunk):
        """Atualiza a resposta incremental com mais um chunk."""
        if not self._console or not chunk:
            return
        self._queue.put(LiveUpdateChunkEvent(agent, chunk))

    def finish_message_stream(self, agent, final_content: str, render_mode: str = "auto"):
        """Fecha o streaming preservando o conteúdo já mostrado."""
        if not self._console:
            return
        with self._lock:
            self._container(agent).finish_stream(self, final_content, render_mode)

    def abort_message_stream(self, agent):
        """Fecha o stream sem marcar a resposta como completa."""
        if not self._console:
            return
        with self._lock:
            self._container(agent).abort_stream(self)

    def update_agent_transient(self, agent, message: str) -> None:
        """Atualiza progresso transitório do agente sem acumular linhas."""
        if not self._console or not agent:
            return
        clean_message = strip_ansi(str(message or "")).strip("\r\n")
        if not clean_message:
            return

        prompt_active = bool(self._is_prompt_active_fn and self._is_prompt_active_fn())
        self.log_debug_event(
            "transient_update",
            agent=agent,
            prompt_active=prompt_active,
            preview=_preview_text(clean_message),
        )

        if prompt_active:
            enqueue_event = None
            fallback: tuple[str, str] | None = None
            with self._lock:
                container = self._container(agent)
                buf = container.transient
                if buf and buf[-1] == clean_message:
                    return
                buf.append(clean_message)
                _term_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
                buf[:] = buf[-max(_SCROLLING_WINDOW_SIZE, _term_lines // 3):]
                self._transient_buf_version += 1
                buf_version = self._transient_buf_version

                if self._run_above_prompt_fn:
                    combined_text, count = self._combined_transient(_term_lines)
                    if combined_text and combined_text != self._last_combined_text:
                        self._last_combined_text = combined_text
                        enqueue_event = TransientWindowEvent(combined_text, count, buf_version)
                else:
                    fallback = (container.label, container.style)

            if enqueue_event is not None:
                self._queue.put(enqueue_event)
            elif fallback is not None:
                label, style = fallback
                line = Text.assemble(
                    (label, f"bold {style}"),
                    (" "),
                    (clean_message, "dim"),
                )
                line.no_wrap = False
                line.overflow = "fold"
                self._print(line, kind="agent_update")
            return

        with self._lock:
            container = self._container(agent)
            is_owned = container.transient_active
            is_active = container.streaming
            if not is_owned and is_active:
                return
            should_start = not is_owned
            if should_start:
                container.transient_active = True

            if not container.push_transient(clean_message):
                return
            display_content = "\n".join(container.transient)

        if should_start:
            self.start_message_stream(agent)
        self.update_message_stream(agent, {"diff": [{"op": "replace", "text": display_content}]})

    def clear_agent_transient(self, agent) -> None:
        """Limpa o bloco transitório do agente, se ativo."""
        if not self._console or not agent:
            return
        prompt_active = bool(self._is_prompt_active_fn and self._is_prompt_active_fn())

        with self._lock:
            container = self._containers.get(agent)
            is_transient_agent = bool(container and container.transient_active)
            if container is not None:
                if container.transient:
                    self._transient_buf_version += 1
                container.clear_transient_buffer()
            buf_version = self._transient_buf_version

            event = None
            if prompt_active and self._run_above_prompt_fn:
                _term_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
                combined_text, count = self._combined_transient(_term_lines)
                if not combined_text:
                    event = TransientClearEvent(buf_version)
                else:
                    self._last_combined_text = combined_text
                    event = TransientWindowEvent(combined_text, count, buf_version)

        if event is not None:
            self._queue.put(event)
            return

        if not is_transient_agent:
            return
        self.abort_message_stream(agent)

    def update_agent_elapsed(self, agent: str, elapsed: float) -> None:
        """Armazena o tempo decorrido do agente para exibição na toolbar."""
        with self._lock:
            self._container(agent).elapsed = elapsed

    def clear_agent_elapsed(self, agent: str) -> None:
        """Remove o tempo decorrido do agente."""
        with self._lock:
            container = self._containers.get(agent)
            if container is not None:
                container.elapsed = None

    def _get_agent_elapsed(self, agent: str) -> float | None:
        """Retorna o tempo decorrido do agente ou None."""
        with self._lock:
            container = self._containers.get(agent)
            return container.elapsed if container is not None else None

    def reset_visual_state(self, agent: str | None = None) -> None:
        """Reseta estado visual após cancelamento (Ctrl+C).

        Se agent for None, limpa todos os agentes.
        Caso contrário, limpa apenas o agente específico.
        """
        with self._lock:
            if agent:
                container = self._containers.get(agent)
                stream_agents = [agent] if (container and container.streaming) else []
                transient_agents = [agent] if (container and container.transient_active) else []
            else:
                stream_agents = [a for a, c in self._containers.items() if c.streaming]
                transient_agents = [a for a, c in self._containers.items() if c.transient_active]
                self._completed_streams.clear()
                self._statuses.clear()
                self._last_persistent_kind = None
                self._last_persistent_agent = None

        for agt in stream_agents:
            self.abort_message_stream(agt)
        for agt in transient_agents:
            self.clear_agent_transient(agt)

        with self._lock:
            if agent:
                container = self._containers.get(agent)
                if container is not None:
                    container.elapsed = None
            else:
                for container in self._containers.values():
                    container.elapsed = None

    def request_toolbar_refresh(self) -> None:
        """Enfileira evento para refresh periódico da toolbar."""
        self._queue.put(ToolbarTickEvent())

    # ------------------------------------------------------------------
    # Exibição de tipos especiais
    # ------------------------------------------------------------------

    def show_no_response(self, agent):
        """Exibe no response."""
        self.clear_agent_transient(agent)
        agent_style, label = self._agent_style(agent)
        message = "sem resposta válida"
        if self._console:
            _, icon = ROLE_STYLES["info"]
            line = Text.assemble(
                (f"{icon} ", "dim"),
                (label, f"bold {agent_style}"),
                (": ", "dim"),
                (message, "dim"),
            )
            self._print(line, kind="agent_update")
        else:
            print(f"{label}: {message}")

    def show_banner(self, message):
        """Exibe mensagem sem ícone (ex: logo de boas-vindas)."""
        clean_message = strip_ansi(str(message)).strip("\r\n")
        if self._console:
            line = Text(clean_message, style="bold cyan")
            line.no_wrap = True
            line.overflow = "ignore"
            self._print(line, kind="banner")
            self._print(Rule(style="dim cyan"), kind="banner")
        else:
            print(clean_message)

    def show_system(self, message):
        """Exibe system."""
        clean_message = strip_ansi(str(message)).strip("\r\n")
        if self._console:
            style, icon = ROLE_STYLES["system"]
            line = Text.assemble((f"{icon} ", f"dim {style}"), (clean_message, f"dim {style}"))
            line.no_wrap = False
            line.overflow = "fold"
            self._print(line, kind="system")
            self._remember_persistent_event("system")
        else:
            print(clean_message)

    def show_approval(self, message):
        """Exibe bloco de aprovação com estilo visual distinto de logs de sistema."""
        clean_message = strip_ansi(str(message)).strip("\r\n")
        if self._console:
            lines = clean_message.splitlines() or [clean_message]
            first = lines[0] if lines else ""
            rest = lines[1:]
            segments = [(f"⚠ ", "yellow"), (first, "bold yellow")]
            text = Text.assemble(*segments)
            for line in rest:
                text.append("\n")
                text.append(line, "dim")
            text.no_wrap = False
            text.overflow = "fold"
            self._print(text, kind="approval")
            self._remember_persistent_event("approval")
        else:
            print(clean_message)

    def show_newline(self):
        """Print a blank newline through the writer thread (thread-safe)."""
        self._print("", kind="generic")

    def show_system_neutral(self, message):
        """Exibe mensagem de sistema com ícone padrão e texto em estilo neutro (dim)."""
        clean_message = strip_ansi(str(message)).strip("\r\n")
        _, icon = ROLE_STYLES["system"]
        if self._console:
            line = Text.assemble((f"{icon} ", "dim"), (clean_message, "dim"))
            line.no_wrap = False
            line.overflow = "fold"
            self._print(line, kind="system_neutral")
            self._remember_persistent_event("system_neutral")
        else:
            print(f"{icon} {clean_message}")

    def show_plain(self, message, agent=None, muted=False):
        """Exibe plain."""
        if agent:
            self.clear_agent_transient(agent)
        self.show_feed(message, agent=agent, muted=muted)

    def show_feed(self, message, agent=None, muted=False):
        """Exibe linha persistente no feed sem limpar o transient/live do agente."""
        clean_message = strip_ansi(str(message)).strip("\r\n")
        if self._console:
            if agent:
                style, label = self._agent_style(agent)
                if muted:
                    segments = [(label, f"dim {style}"), (" "), (clean_message, "dim")]
                else:
                    segments = [(label, f"bold {style}"), (" "), (clean_message,)]
                line = Text.assemble(*segments)
            else:
                if muted:
                    line = Text.assemble(
                        ("·", "dim"),
                        (" "),
                        (clean_message, "dim"),
                    )
                else:
                    line = Text.assemble(
                        ("·", "dim"),
                        (" "),
                        (clean_message,),
                    )
            line.no_wrap = False
            line.overflow = "fold"
            self._print(line, kind="plain")
            self._remember_persistent_event("plain", agent)
        else:
            prefix = f"{agent}: " if agent else ""
            print(f"{prefix}{clean_message}")

    def show_error(self, message, agent=None, command_name=None, error_kind=None, return_code=None):
        """Exibe error."""
        clean_message = strip_ansi(str(message)).strip("\r\n")
        subject = _coerce_agent_name(agent) if agent else strip_ansi(str(command_name or "")).strip()
        if error_kind == "agent_exit" and return_code is not None:
            clean_message = (
                f"[erro] retornou código {return_code}"
                if agent
                else f"[erro] agente {subject or 'unknown'} retornou código {return_code}"
            )
        elif error_kind == "agent_comm":
            clean_message = (
                f"[erro] falha ao comunicar: {clean_message}"
                if agent
                else f"[erro] falha ao comunicar com {subject or 'unknown'}: {clean_message}"
            )
        elif error_kind == "agent_invalid_output":
            clean_message = (
                "[erro] não retornou saída válida"
                if agent
                else f"[erro] agente {subject or 'unknown'} não retornou saída válida"
            )

        if self._console:
            style, icon = ROLE_STYLES["error"]
            if agent:
                agent_style, label = self._agent_style(agent)
                line = Text.assemble(
                    (f"{label} ", f"bold {agent_style}"),
                    (f"{icon} ", style),
                    (clean_message, "red"),
                )
            else:
                line = Text.assemble((f"{icon} ", style), (clean_message, "red"))
            self._print(line, kind="error")
            self._remember_persistent_event("error", agent)
        else:
            if agent:
                _, label = self._agent_style(agent)
                print(f"{label} ✗ {clean_message}")
            else:
                print(clean_message)

    def show_warning(self, message):
        """Exibe warning."""
        clean_message = strip_ansi(str(message)).strip("\r\n")
        if self._console:
            style, icon = ROLE_STYLES["warning"]
            line = Text.assemble((f"{icon} ", style), (clean_message, "yellow"))
            self._print(line, kind="warning")
            self._remember_persistent_event("warning")
        else:
            print(clean_message)

    def show_turn_summary(self, agent: str | None, detail: dict) -> None:
        """Exibe resumo compacto do turno em uma linha."""
        runtime = str((detail or {}).get("runtime") or "").strip().lower()
        if runtime and runtime != "cli":
            return
        tools = detail.get("tools", []) if isinstance(detail, dict) else []
        if not isinstance(tools, list) or not tools:
            return

        total = 0
        ok_count = 0
        err_count = 0
        total_ms = 0
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            total += 1
            status = str(tool.get("status") or "").strip().lower()
            if status in {"ok", "success", "succeeded"}:
                ok_count += 1
            if status in {"error", "failed", "fail", "timeout"}:
                err_count += 1
            duration_ms = tool.get("duration_ms")
            if isinstance(duration_ms, int) and duration_ms >= 0:
                total_ms += duration_ms

        if total <= 0:
            return

        if total_ms < 1000:
            duration = f"{total_ms}ms"
        else:
            duration = f"{total_ms / 1000:.1f}s"

        summary = f"TOOLS: {total} chamadas · {ok_count} ok · {err_count} erro · {duration}"
        if isinstance(agent, str) and agent.strip():
            self.show_feed(summary, agent=agent, muted=True)
        else:
            self.show_system_neutral(summary)

    def show_delegation(self, from_agent, to_agent, task=None):
        """Exibe delegation."""
        from_style, from_label = self._agent_style(from_agent)
        to_style, to_label = self._agent_style(to_agent)
        if self._console:
            title_parts = [
                (f"  {from_label} ", f"bold {from_style}"),
                ("→ ", "dim"),
                (f"{to_label}  ", f"bold {to_style}"),
            ]
            if task:
                title_parts.append((f"· {task}", "dim"))
            title = Text.assemble(*title_parts)
            self._print(Rule(title, style="dim", characters="─"), kind="delegation")
        else:
            arrow = f"{from_label} → {to_label}"
            if task:
                arrow += f"  ·  {task}"
            print(arrow)

    def show_prompt_preview(self, agent: str, content: str):
        """Exibe preview do /prompt como painel de depuração."""
        public_ui = _public_ui_module()
        if self._console and public_ui._RICH_AVAILABLE:
            style, label = self._agent_style(agent)
            renderable = public_ui.Panel(
                _highlight_tags(strip_ansi(content.strip())),
                title=f"[bold {style}]Prompt Preview · {markup_escape(label)}[/]",
                border_style=f"dim {style}",
                padding=(1, 2),
            )
            self._print(renderable, kind="prompt_preview")
        else:
            sys.stderr.write(content + "\n")
            sys.stderr.flush()

    # ------------------------------------------------------------------
    # Status dinâmico (agentes paralelos)
    # ------------------------------------------------------------------

    def update_status(self, agent, message):
        """Atualiza o status de um agente no painel dinâmico."""
        if not self._console or not self._live:
            return
        clean_message = strip_ansi(str(message))
        with self._lock:
            self._statuses[agent] = clean_message

    def _render_status_panel(self):
        """Renderiza o painel de status fixo."""
        public_ui = _public_ui_module()
        table = Table.grid(expand=True)
        table.add_column(width=3)
        table.add_column()

        with self._lock:
            sorted_agents = sorted(self._statuses.keys())
            for agent in sorted_agents:
                status = self._statuses[agent]
                style, label = self._agent_style(agent)
                is_active = "concluído" not in status.lower() and "erro" not in status.lower()
                icon = f"[{style}]●[/{style}]" if is_active else "[green]✓[/]"
                table.add_row(icon, f"[{style}]{label}[/]: {markup_escape(status)}")

        count = len(self._statuses)
        title = f"[dim]Agentes em Execução · {count}[/]" if count else "[dim]Agentes em Execução[/]"
        return public_ui.Panel(
            table,
            title=title,
            border_style="dim",
            padding=(0, 1)
        )

    @contextmanager
    def live_status(self, agents):
        """Context manager para exibir status dinâmico de múltiplos agentes."""
        public_ui = _public_ui_module()
        if not self._console or not public_ui._RICH_AVAILABLE:
            yield
            return

        if self._stream_live_active.is_set():
            with self._lock:
                self._statuses = {agent: "inicializando..." for agent in agents}
            try:
                yield
            finally:
                with self._lock:
                    self._live = None
                    self._statuses = {}
            return

        with self._lock:
            self._statuses = {agent: "inicializando..." for agent in agents}
            self._live = public_ui.Live(
                self._render_status_panel(),
                console=self._console,
                refresh_per_second=4,
                get_renderable=self._render_status_panel,
                transient=False
            )

        with self._live:
            yield

        with self._lock:
            self._live = None
            self._statuses = {}

    def running_status(self, initial="", agent=None):
        """Retorna um context manager com spinner animado. Chame .update(text) dentro do bloco."""
        if self._console:
            if self._live and agent:
                class StatusProxy:
                    def __init__(self, renderer, agent):
                        self.renderer = renderer
                        self.agent = agent

                    def update(self, text):
                        self.renderer.update_status(self.agent, text)

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        self.renderer.update_status(self.agent, "concluído")

                return StatusProxy(self, agent)

            if self._stream_live_active.is_set():
                return _NullStatus()

            if threading.current_thread() is not threading.main_thread():
                return _NullStatus()
            if agent:
                color, label = self._agent_style(agent)
                segments = [(label, f"bold {color}")]
                if initial:
                    segments.extend([
                        (" · ", "dim"),
                        (initial, ""),
                    ])
                status_text = Text.assemble(*segments)
            else:
                status_text = initial
            return self._console.status(
                status_text,
                refresh_per_second=_SEQUENTIAL_STATUS_REFRESH_PER_SECOND,
            )

        return _NullStatus()

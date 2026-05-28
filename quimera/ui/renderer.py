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
    # Remove real ANSI escape sequences (starting with \x1b[)
    ansi_real = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')
    text = ansi_real.sub('', text)

    # Remove orphaned ANSI-like sequences that lost their \x1b prefix
    # These look like [1m, [?25h, [1G, [2K, etc.
    # Require at least one digit/?/; to avoid matching Rich markup like [bold]
    ansi_orphaned = re.compile(r'\[[0-9;?]+[A-Za-z]')
    text = ansi_orphaned.sub('', text)

    # Remove caracteres Unicode de controle/invisíveis (bidi, zero-width, C0/C1).
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
    # rich.markdown.Markdown — extrai o texto-fonte do markup
    markup = getattr(value, "markup", None)
    if isinstance(markup, str):
        return markup
    # rich.panel.Panel — extrai título e corpo
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
# Eventos tipados (item 4: Enum + dataclass)
# ---------------------------------------------------------------------------

@dataclass
class PrintEvent:
    renderable: Any
    kwargs: dict = field(default_factory=dict)


@dataclass
class LiveStartEvent:
    agent: str
    state: dict


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
    """Proxy seguro que substitui nullcontext(None) quando não há spinner ativo.

    C2: callers que fazem `.update(text)` dentro do bloco não recebem AttributeError.
    """

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


class TerminalRenderer:
    """Camada exclusiva de apresentação no terminal. Nunca toca em persistência."""

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

        # Streams completados: agent -> final_content (atualizado sync antes de live_stop)
        self._completed_streams = {}
        # Agents com stream ativo (atualizado sync, protegido por _lock)
        self._active_stream_agents = set()
        # Streams transitórios de progresso (substituídos visualmente no mesmo bloco)
        self._transient_stream_agents = set()
        # Buffer rolling para feed rolável do agente.
        # Em prompt inativo alimenta o Live; em prompt ativo controla dedupe/memória,
        # deixando o scrollback natural do terminal fazer a rolagem visual.
        self._rolling_buffers: dict[str, list[str]] = {}
        # Lock protege _completed_streams, _active_stream_agents e _statuses
        self._lock = threading.RLock()

        # Versão monotônica do buffer rolling — incrementada a cada mutação.
        # Eventos TransientWindowEvent/TransientClearEvent carregam o valor
        # do snapshot; o writer descarta eventos com versão defasada.
        self._transient_buf_version = 0

        # Último texto combinado enfileirado — usado para dedup no prompt ativo.
        self._last_combined_text: str | None = None

        # Flag: writer thread tem um Live ativo (sinaliza threads externas)
        self._stream_live_active = threading.Event()

        # Hooks de integração com prompt_toolkit (configurados via set_prompt_integration)
        self._is_prompt_active_fn = None  # () -> bool
        self._run_above_prompt_fn = None  # (callable) -> bool
        self._output_suspended = threading.Event()

        # Fila com backpressure (item 3): produtor bloqueia se fila cheia
        self._queue: _queue_module.Queue = _queue_module.Queue(maxsize=512)
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

    # ------------------------------------------------------------------
    # Ciclo de vida (item 1)
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
        """Expõe auditoria estruturada para camadas superiores (ex.: SpyOutputPresenter)."""
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
        """Single writer: processa todos os eventos de UI sequencialmente.

        Mantém um único Live unificado para todos os streams ativos.
        """
        _stream_states: dict[str, dict] = {}  # agent -> {content, label, style, theme_name}
        _ul: list = [None]  # _ul[0] = Live unificado ativo (ou None)
        _local_pending: collections.deque = collections.deque()  # buffer de "unget" para coalescing
        _deferred_post_prompt: collections.deque = collections.deque()  # prints deferidos enquanto prompt ativo
        _prev_lines: list = [0]  # [int] — só as closures atualizam; evita divergência em closures descartadas

        def _prompt_active() -> bool:
            try:
                return bool(self._is_prompt_active_fn and self._is_prompt_active_fn())
            except Exception:
                return False

        def _get_renderable():
            if not _stream_states:
                return Text("")
            parts = [
                self._build_stream_renderable(
                    st["theme_name"], st["label"], st["style"], st["content"]
                )
                for st in _stream_states.values()
            ]
            main = Group(*parts) if len(parts) > 1 else parts[0]
            if self._density == "compact":
                return main
            active_count = len(parts)
            if active_count > 1:
                labels = " · ".join(
                    _agent_toolbar_label(agent, st["label"])
                    for agent, st in _stream_states.items()
                )
                toolbar_text = f"[dim]{labels} · Ctrl+C para cancelar · T para tema[/dim]"
            else:
                only_agent, only_state = next(iter(_stream_states.items()))
                label_text = _agent_toolbar_label(only_agent, only_state["label"])
                toolbar_text = (
                    f"[bold {only_state['style']}]{label_text}[/] "
                    f"[dim]· Ctrl+C para cancelar · T para tema[/dim]"
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
                # Não inicia Live quando prompt_toolkit está ativo — os dois controladores
                # de terminal conflitam e corrompem o display.
                # Também não inicia se a saída está suspensa (ex.: editor externo aberto).
                if _prompt_active() or self._output_suspended.is_set():
                    return
                _ul[0] = _public_ui_module().Live(
                    _get_renderable(),
                    console=self._console,
                    refresh_per_second=8,
                    transient=False,
                    auto_refresh=False,
                )
                _ul[0].start()
                self._stream_live_active.set()

        def _refresh():
            if _ul[0] is not None and not self._output_suspended.is_set():
                _ul[0].update(_get_renderable(), refresh=True)

        def _close_live():
            """Encerra o Live ativo com refresh final vazio — garante que toolbar não congela."""
            if _ul[0] is not None:
                _ul[0].update(Text(""), refresh=True)
                _ul[0].stop()
                _ul[0] = None
                self._stream_live_active.clear()

        def _stop_if_empty():
            if not _stream_states:
                _close_live()

        def _cprint(renderable, **kwargs):
            """Imprime via Live ativo, run_in_terminal (se prompt ativo) ou direto ao console."""
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
                _r, _k = renderable, dict(kwargs)
                def _clear_and_print(_r=_r, _k=_k, lines=_prev_lines):
                    p = lines[0]
                    if p > 0:
                        sys.stdout.write(f"\033[{p}A\033[J")
                        lines[0] = 0
                    self._console.print(_r, **_k)
                if run_above(_clear_and_print):
                    _flush_deferred()
                    return
            _deferred_post_prompt.append((renderable, kwargs))

        def _flush_deferred(force=False):
            """Flush prints que foram deferidos enquanto prompt estava ativo.
            Só imprime se houver Live ou run_above disponível — caso contrário
            mantém deferido para evitar colar saída na linha do prompt.
            Use force=True (ex: flush()) para forçar o dreno mesmo sem run_above."""
            if self._output_suspended.is_set():
                return
            run_above = self._run_above_prompt_fn
            while _deferred_post_prompt:
                _r, _k = _deferred_post_prompt.popleft()
                if _ul[0] is not None:
                    _ul[0].console.print(_r, **_k)
                elif force and self._console:
                    # force=True: imprime direto no console mesmo sem prompt ativo.
                    # Usado por flush() antes do prompt iniciar, quando run_above
                    # falharia porque prompt_toolkit ainda não está rodando.
                    self._console.print(_r, **_k)
                elif run_above is not None:
                    def _clear_and_print(_r=_r, _k=_k, lines=_prev_lines):
                        p = lines[0]
                        if p > 0:
                            sys.stdout.write(f"\033[{p}A\033[J")
                            lines[0] = 0
                        self._console.print(_r, **_k)
                    if not run_above(_clear_and_print):
                        _deferred_post_prompt.appendleft((_r, _k))
                        break
                else:
                    # Sem Live nem run_above — re-defer para não colar no prompt
                    _deferred_post_prompt.appendleft((_r, _k))
                    break

        def _audit_event(event_name: str, **payload) -> None:
            self.log_debug_event(event_name, **payload)

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

            # Resiliência: exceção em qualquer evento não mata o writer
            try:
                if isinstance(event, PrintEvent):
                    preview = _preview_text(event.renderable)
                    if preview:
                        _audit_event(
                            "print",
                            prompt_active=_prompt_active(),
                            preview=preview,
                        )
                    _cprint(event.renderable, **event.kwargs)

                elif isinstance(event, LiveStartEvent):
                    _audit_event(
                        "stream_start",
                        agent=event.agent,
                        prompt_active=_prompt_active(),
                    )
                    _stream_states[event.agent] = event.state
                    _ensure_live()
                    _refresh()

                elif isinstance(event, LiveUpdateChunkEvent):
                    # Coalescing: drena chunks consecutivos do mesmo agente antes de renderizar
                    agent = event.agent
                    chunks = [event.chunk]
                    while True:
                        try:
                            next_ev = self._queue.get_nowait()
                        except _queue_module.Empty:
                            break
                        if isinstance(next_ev, LiveUpdateChunkEvent) and next_ev.agent == agent:
                            chunks.append(next_ev.chunk)
                        else:
                            # Preserva a ordem: evento não-relacionado vai para o buffer local (frente)
                            _local_pending.appendleft(next_ev)
                            break

                    state = _stream_states.get(agent)
                    if state:
                        for chunk in chunks:
                            if isinstance(chunk, dict):
                                state["content"] = _apply_stream_diff(
                                    state["content"],
                                    _normalize_stream_diff(chunk.get("diff"))
                                )
                                text = chunk.get("text")
                                if text and not chunk.get("diff"):
                                    state["content"] += strip_ansi(str(text))
                            else:
                                state["content"] += strip_ansi(str(chunk))
                        _audit_event(
                            "stream_chunk",
                            agent=agent,
                            chunk_count=len(chunks),
                            preview=_preview_chunk(chunks[-1]),
                            previews=[_preview_chunk(chunk) for chunk in chunks[:5]],
                            previews_truncated=len(chunks) > 5,
                        )
                        _refresh()

                elif isinstance(event, LiveStopEvent):
                    _audit_event(
                        "stream_stop",
                        agent=event.agent,
                        render_mode=event.render_mode,
                        preview=_preview_text(event.final_content),
                    )
                    state = _stream_states.pop(event.agent, None)
                    if state:
                        if _stream_states:   # só atualiza Live se ainda há outros streams
                            _refresh()
                        _stop_if_empty()    # para o Live se for o último agente
                        final_block = self._render_turn_block(
                            state["theme_name"], state["label"], state["style"],
                            content=event.final_content,
                            include_header=True,
                            include_footer_rule=True,
                            render_mode=event.render_mode,
                        )
                        _cprint(final_block)

                elif isinstance(event, LiveAbortEvent):
                    _audit_event("stream_abort", agent=event.agent)
                    _stream_states.pop(event.agent, None)
                    if _stream_states:   # só atualiza Live se ainda há outros streams
                        _refresh()
                    _stop_if_empty()

                elif isinstance(event, NoopEvent):
                    _flush_deferred(force=event.force_flush)
                    event.done.set()

                elif isinstance(event, ToolbarTickEvent):
                    _refresh()

                elif isinstance(event, OutputControlEvent):
                    if event.suspend:
                        self._output_suspended.set()
                        _close_live()
                    else:
                        self._output_suspended.clear()
                        _flush_deferred(force=True)
                    if event.done is not None:
                        event.done.set()

                elif isinstance(event, TransientWindowEvent):
                    # Coalescing: drena eventos consecutivos — o mais recente substitui os anteriores
                    while True:
                        try:
                            _next = self._queue.get_nowait()
                        except _queue_module.Empty:
                            break
                        if isinstance(_next, TransientWindowEvent):
                            event = _next  # contém combined completo, substitui
                        else:
                            _local_pending.appendleft(_next)
                            break

                    if event.buf_version < self._transient_buf_version:
                        continue
                    text, count = event.text, event.count

                    if not text:
                        continue

                    def _replace(buf_v=event.buf_version, t=text, c=count, lines=_prev_lines):
                        if buf_v < self._transient_buf_version:
                            # Closure descartada: screen não foi alterada, não atualiza contador
                            return
                        p = lines[0]  # lê estado real da tela no momento da execução
                        if p > 0:
                            sys.stdout.write(f"\033[{p}A\033[J")
                        sys.stdout.write(t)
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        lines[0] = c  # atualiza somente quando realmente escreveu

                    run_above = self._run_above_prompt_fn
                    if run_above is not None:
                        if not run_above(_replace):
                            _prev_lines[0] = 0
                    elif self._console:
                        self._console.print(text)

                elif isinstance(event, TransientClearEvent):
                    if event.buf_version < self._transient_buf_version:
                        continue

                    def _clear(buf_v=event.buf_version, lines=_prev_lines):
                        if buf_v < self._transient_buf_version:
                            return
                        p = lines[0]
                        lines[0] = 0  # atualiza antes de escrever para refletir estado real
                        if p > 0:
                            sys.stdout.write(f"\033[{p}A\033[J")
                            sys.stdout.flush()

                    run_above = self._run_above_prompt_fn
                    if run_above is not None:
                        run_above(_clear)

            except Exception:
                _log.exception("writer thread: erro ao processar evento %r", event)

    def flush(self, timeout: float = 5.0):
        """Aguarda o writer thread processar todos os eventos pendentes
        e força o dreno de mensagens deferidas para o console."""
        done = threading.Event()
        self._queue.put(NoopEvent(done, force_flush=True))
        if not done.wait(timeout=timeout):
            raise TimeoutError(f"TerminalRenderer.flush timed out after {timeout} seconds")

    def flush_quick(self, timeout: float = 0.15) -> bool:
        """Tenta drenar rapidamente sem bloquear o thread do prompt por muito tempo."""
        try:
            self.flush(timeout=timeout)
            return True
        except TimeoutError:
            return False

    def suspend_output(self, timeout: float = 2.0) -> bool:
        """Suspende temporariamente prints no terminal (ex.: editor externo ativo)."""
        # Ativa o bloqueio imediatamente para evitar vazamento de linhas enquanto
        # o evento de controle ainda aguarda processamento na fila do writer.
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
            # Evita ficar preso em estado suspenso indefinidamente caso o writer
            # esteja travado ou atrasado além do timeout.
            self._output_suspended.clear()
        return resumed

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

    def _print(self, renderable, **kwargs):
        """Enfileira um evento de print para o writer thread."""
        self._queue.put(PrintEvent(renderable, kwargs))

    def _spacing(self):
        """Imprime linha em branco entre turnos; no-op em modo compact."""
        if self._density != "compact":
            self._print("")

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
        if content is not None:
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
            self._print(block)
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
        style, label = self._agent_style(agent)
        with self._lock:
            if agent in self._active_stream_agents:
                return
            self._active_stream_agents.add(agent)
            theme_name = self._theme.name

        self._spacing()
        state = {
            "content": "",
            "label": label,
            "style": style,
            "theme_name": theme_name,
        }
        self._queue.put(LiveStartEvent(agent, state))

    def update_message_stream(self, agent, chunk):
        """Atualiza a resposta incremental com mais um chunk."""
        if not self._console or not chunk:
            return
        self._queue.put(LiveUpdateChunkEvent(agent, chunk))

    def finish_message_stream(self, agent, final_content: str, render_mode: str = "auto"):
        """Fecha o streaming preservando o conteúdo já mostrado."""
        if not self._console:
            return
        clean_content = strip_ansi(str(final_content or ""))
        normalized_mode = _normalize_render_mode(render_mode)
        # Atualiza _completed_streams de forma síncrona, antes de enfileirar live_stop,
        # para que _consume_completed_stream em show_message funcione corretamente.
        with self._lock:
            self._completed_streams[agent] = clean_content
            self._active_stream_agents.discard(agent)
        self._queue.put(LiveStopEvent(agent, clean_content, normalized_mode))

    def abort_message_stream(self, agent):
        """Fecha o stream sem marcar a resposta como completa."""
        if not self._console:
            return
        with self._lock:
            self._active_stream_agents.discard(agent)
        self._queue.put(LiveAbortEvent(agent))

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
            with self._lock:
                buf = self._rolling_buffers.setdefault(agent, [])
                if buf and buf[-1] == clean_message:
                    return
                buf.append(clean_message)
                _term_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
                buf[:] = buf[-max(_SCROLLING_WINDOW_SIZE, _term_lines // 3):]
                self._transient_buf_version += 1
                buf_version = self._transient_buf_version

                enqueue_event = None
                if self._run_above_prompt_fn:
                    all_bufs = dict(self._rolling_buffers)
                    combined = []
                    for agt, msgs in sorted(all_bufs.items()):
                        _, label = self._agent_style(agt)
                        for msg in msgs:
                            combined.append(f"{label} {msg}")
                    _term_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
                    _win_limit = max(1, _term_lines // 3)
                    combined = combined[-_win_limit:]
                    combined_text = '\n'.join(combined)
                    if combined and combined_text != self._last_combined_text:
                        self._last_combined_text = combined_text
                        enqueue_event = TransientWindowEvent(combined_text, len(combined), buf_version)
                else:
                    style, label = self._agent_style(agent)

            # Outside lock — operações de fila
            if enqueue_event is not None:
                self._queue.put(enqueue_event)
            elif not self._run_above_prompt_fn:
                line = Text.assemble(
                    (label, f"bold {style}"),
                    (" "),
                    (clean_message, "dim"),
                )
                line.no_wrap = False
                line.overflow = "fold"
                self._print(line)
            return

        with self._lock:
            is_owned = agent in self._transient_stream_agents
            is_active = agent in self._active_stream_agents
            # Não interfere em stream de resposta que não pertence ao canal transitório.
            if not is_owned and is_active:
                return
            should_start = not is_owned
            if should_start:
                self._transient_stream_agents.add(agent)

            # Rolling buffer: adiciona apenas se diferente da última entrada
            buf = self._rolling_buffers.setdefault(agent, [])
            if buf and buf[-1] == clean_message:
                return
            buf.append(clean_message)
            buf[:] = buf[-_SCROLLING_WINDOW_SIZE:]
            display_content = "\n".join(buf)

        if should_start:
            self.start_message_stream(agent)
        self.update_message_stream(agent, {"diff": [{"op": "replace", "text": display_content}]})

    def clear_agent_transient(self, agent) -> None:
        """Limpa o bloco transitório do agente, se ativo."""
        if not self._console or not agent:
            return
        self.log_debug_event("transient_clear", agent=agent)

        prompt_active = bool(self._is_prompt_active_fn and self._is_prompt_active_fn())

        with self._lock:
            removed = self._rolling_buffers.pop(agent, None)
            is_transient_agent = agent in self._transient_stream_agents
            if is_transient_agent:
                self._transient_stream_agents.discard(agent)
            if removed is not None:
                self._transient_buf_version += 1
            buf_version = self._transient_buf_version

            event = None
            if prompt_active and self._run_above_prompt_fn:
                all_bufs = dict(self._rolling_buffers)
                if not all_bufs:
                    event = TransientClearEvent(buf_version)
                else:
                    combined = []
                    for agt, msgs in sorted(all_bufs.items()):
                        _, label = self._agent_style(agt)
                        for msg in msgs:
                            combined.append(f"{label} {msg}")
                    _term_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
                    _win_limit = max(1, _term_lines // 3)
                    combined = combined[-_win_limit:]
                    if not combined:
                        event = TransientClearEvent(buf_version)
                    else:
                        combined_text = '\n'.join(combined)
                        self._last_combined_text = combined_text
                        event = TransientWindowEvent(combined_text, len(combined), buf_version)

        if event is not None:
            self._queue.put(event)
            return

        if not is_transient_agent:
            return
        self.abort_message_stream(agent)

    def update_agent_elapsed(self, agent: str, elapsed: float) -> None:
        """Armazena o tempo decorrido do agente para exibição na toolbar."""
        with self._lock:
            self._agent_elapsed[agent] = elapsed

    def clear_agent_elapsed(self, agent: str) -> None:
        """Remove o tempo decorrido do agente."""
        with self._lock:
            self._agent_elapsed.pop(agent, None)

    def _get_agent_elapsed(self, agent: str) -> float | None:
        """Retorna o tempo decorrido do agente ou None."""
        with self._lock:
            return self._agent_elapsed.get(agent)

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
            self._print(line)
        else:
            print(f"{label}: {message}")

    def show_banner(self, message):
        """Exibe mensagem sem ícone (ex: logo de boas-vindas)."""
        clean_message = strip_ansi(str(message)).strip("\r\n")
        if self._console:
            line = Text(clean_message, style="bold cyan")
            # Logo ASCII precisa manter geometria original; wrap automático deforma.
            line.no_wrap = True
            line.overflow = "ignore"
            self._print(line)
            self._print(Rule(style="dim cyan"))
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
            self._print(line)
        else:
            print(clean_message)

    def show_newline(self):
        """Print a blank newline through the writer thread (thread-safe)."""
        self._print("")

    def show_system_neutral(self, message):
        """Exibe mensagem de sistema com ícone padrão e texto em estilo neutro (dim)."""
        clean_message = strip_ansi(str(message)).strip("\r\n")
        _, icon = ROLE_STYLES["system"]
        if self._console:
            line = Text.assemble((f"{icon} ", "dim"), (clean_message, "dim"))
            line.no_wrap = False
            line.overflow = "fold"
            self._print(line)
        else:
            print(f"{icon} {clean_message}")

    def show_plain(self, message, agent=None, muted=False):
        """Exibe plain."""
        if agent:
            self.clear_agent_transient(agent)
        clean_message = strip_ansi(str(message)).strip("\r\n")
        if self._console:
            if agent:
                style, label = self._agent_style(agent)
                segments = [(label, f"bold {style}"), (" ")]
                if muted:
                    segments.append((clean_message, "dim"))
                else:
                    segments.append((clean_message,))
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
            self._print(line)
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
            self._print(line)
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
            self._print(line)
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
        last_tool_name = "ferramenta"
        last_tool_status = "unknown"

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
            last_tool_name = str(tool.get("tool") or "ferramenta")
            last_tool_status = str(tool.get("status") or "unknown")

        if total <= 0:
            return

        if total_ms < 1000:
            duration = f"{total_ms}ms"
        else:
            duration = f"{total_ms / 1000:.1f}s"

        trace_id = str(detail.get("trace_id") or detail.get("turn_id") or "n/a")
        summary = (
            f"TOOLS: {total} chamadas · {ok_count} ok · {err_count} erro · {duration} "
            f"· último: {last_tool_name}({last_tool_status}) · trace_id={trace_id}"
        )
        prefix = f"{agent} " if isinstance(agent, str) and agent.strip() else ""
        self.show_system_neutral(f"{prefix}{summary}")

    def show_handoff(self, from_agent, to_agent, task=None):
        """Exibe handoff."""
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
            self._print(Rule(title, style="dim", characters="─"))
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
            self._print(renderable)
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

                # Indicador de status para agentes ativos
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

        # C1: se o writer já tem um Live ativo (streaming), não abrir segundo Live no mesmo Console
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
            # Se já estamos em modo Live paralelo (live_status), atualiza o painel global
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

            # Se há um Live de streaming ativo no writer thread, não criar outro Live
            # (múltiplos Lives no mesmo Console causam corrupção visual)
            if self._stream_live_active.is_set():
                return _NullStatus()

            # Caso sequencial sem Live ativo: usa o spinner padrão do Rich.
            # Em threads de background (modo threaded), o Rich escreve diretamente
            # no terminal enquanto prompt_toolkit controla o input na main thread —
            # isso causa corrupção visual. Neste caso, suprime o spinner.
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

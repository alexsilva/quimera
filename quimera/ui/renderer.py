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
from typing import Any

_log = logging.getLogger(__name__)

from .agent_window_controller import AgentWindowController
from .compositor import TerminalCompositor
from .audit import RenderAuditLogger
from .events import (
    LiveAbortEvent,
    LiveStartEvent,
    LiveStopEvent,
    LiveUpdateChunkEvent,
    NoopEvent,
    OutputControlEvent,
    PendingInputEvent,
    PrintEvent,
    TerminalResizeEvent,
    ToolbarTickEvent,
    TransientClearEvent,
    TransientWindowEvent,
)
from .window_manager import WindowManager, WindowRenderPlan
from .windows import (
    AgentWindowState,
    RenderWindowState,
    RestorePolicy,
    WindowDeck,
    WindowKind,
    WindowLayer,
    WindowModality,
)

from .text import (
    _PREVIEW_LIMIT,
    _UNICODE_CONTROL_RE,
    _apply_stream_diff,
    _extract_text_from_renderable,
    _highlight_tags,
    _normalize_completed_content,
    _normalize_stream_diff,
    _preview_chunk,
    _preview_text,
    strip_ansi,
)

_RENDER_MODES = {"plain", "markdown", "auto"}
_SEQUENTIAL_STATUS_REFRESH_PER_SECOND = 4
_SCROLLING_WINDOW_SIZE = 10


def _normalize_render_mode(render_mode: str | None) -> str:
    mode = str(render_mode or "auto").strip().lower()
    if mode in _RENDER_MODES:
        return mode
    return "auto"


def _is_interactive_terminal() -> bool:
    """Check if we're running in an interactive terminal (not piped/captured)."""
    return sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'


def _public_ui_module():
    module = _sys_module.modules.get("quimera.ui")
    if module is not None:
        return module
    return _sys_module.modules[__name__]


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

        # Deck de janelas: fonte unica para estado visual por agente.
        self._deck = WindowDeck()
        self._window_manager = WindowManager(self._deck)
        self._floor_windows_by_thread: dict[int, str] = {}
        # Último evento persistente impresso, para evitar espaçamento redundante.
        self._last_persistent_kind: str | None = None
        self._last_persistent_agent: str | None = None
        # Lock protege _deck, _statuses e versão
        self._lock = threading.RLock()

        # Hooks de integração com prompt_toolkit
        self._is_prompt_active_fn = None  # () -> bool
        self._run_above_prompt_fn = None  # (callable) -> bool

        # Compositor — dono do writer thread, queue e controle de saída
        self._compositor = TerminalCompositor(self, audit_logger=self._audit_logger)

        # Aliases temporários para compatibilidade com testes e código cliente
        self._queue = self._compositor.queue
        self._output_suspended = self._compositor.output_suspended
        self._stream_live_active = self._compositor.stream_live_active
        # overlay_lines é uma lista compartilhada (mutável) — alias mantém a mesma referência
        self._overlay_lines = self._compositor._overlay_lines

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
        """Encerra o compositor graciosamente."""
        self._compositor.close(timeout=timeout)
        if self._audit_logger is not None:
            self._audit_logger.close()

    def log_debug_event(self, event: str, **payload) -> None:
        """Expõe auditoria estruturada para camadas superiores."""
        if self._audit_logger is None:
            return
        self._audit_logger.log_event(event, **payload)

    def __del__(self):
        self._compositor.stop_nowait()

    def _emit_ui_event(self, event) -> None:
        """Enfileira evento de UI no compositor (thread-safe)."""
        self._compositor.emit(event)

    def flush(self, timeout: float = 5.0):
        """Aguarda o writer thread processar todos os eventos pendentes."""
        self._compositor.flush(timeout=timeout)

    def flush_quick(self, timeout: float = 0.15) -> bool:
        """Tenta drenar rapidamente sem bloquear o thread do prompt."""
        return self._compositor.flush_quick(timeout=timeout)

    def _freeze_output(self, timeout: float = 2.0) -> bool:
        """Congela temporariamente a saída do compositor."""
        return self._compositor.freeze_output(timeout=timeout)

    def _thaw_output(self, timeout: float = 2.0) -> bool:
        """Retoma a saída do compositor e drena saídas deferidas."""
        return self._compositor.thaw_output(timeout=timeout)

    def _apply_window_render_plan(self, plan: WindowRenderPlan, timeout: float = 2.0) -> bool:
        """Aplica no terminal o plano produzido pelo WindowManager."""
        return self._compositor.apply_window_render_plan(plan, timeout=timeout)

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
    # Isto formaliza a posse do terminal em uma única API de alto nível.

    def _implicit_floor_window(
        self,
        kind: WindowKind | str = WindowKind.TERMINAL_FLOOR,
        title: str = "Terminal floor",
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RenderWindowState:
        thread_id = threading.get_ident()
        return self._window_manager.make_floor_window(
            window_id=f"floor:{thread_id}",
            kind=kind,
            title=title,
            owner=owner,
            metadata=metadata or {},
        )

    def request_floor(
        self,
        timeout: float = 2.0,
        *,
        window: RenderWindowState | None = None,
        kind: WindowKind | str = WindowKind.TERMINAL_FLOOR,
        title: str = "Terminal floor",
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Cede o chão do terminal ao chamador (que vira o dono temporário do stdout).

        DEVE ser chamado de dentro de run_in_terminal — o chamador é, naquele
        instante, dono do terminal. Suspende o feed (Live + overlay transitório)
        e limpa as linhas de overlay que ainda estejam na tela, deixando o chão
        limpo para o prompt do leitor.
        """
        active_window = window or self._implicit_floor_window(kind, title, owner, metadata)
        with self._lock:
            transition = self._window_manager.mount(active_window)
            self._floor_windows_by_thread[threading.get_ident()] = active_window.id
        return self._apply_window_render_plan(transition.render_plan, timeout=timeout)

    def release_floor(self, timeout: float = 2.0) -> bool:
        """Devolve o chão ao Compositor: retoma o feed e drena prints deferidos."""
        with self._lock:
            window_id = self._floor_windows_by_thread.pop(threading.get_ident(), None)
            transition = self._window_manager.close(window_id) if window_id else None
        if transition is None:
            return True
        return self._apply_window_render_plan(transition.render_plan, timeout=timeout)

    @contextmanager
    def external_window(self, window_id: str, title: str = "", metadata: dict[str, Any] | None = None):
        """Monta uma janela externa modal que recebe posse exclusiva do terminal."""
        window = self._window_manager.make_external_window(
            window_id,
            kind=WindowKind.EDITOR,
            title=title,
            metadata=metadata or {},
        )
        self.request_floor(window=window)
        try:
            yield window
        finally:
            self.release_floor()

    @contextmanager
    def approval_window(
        self,
        *,
        title: str = "Aprovação",
        metadata: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ):
        """Monta uma janela de aprovação com posse exclusiva do terminal."""
        self.request_floor(
            timeout=timeout,
            kind=WindowKind.APPROVAL,
            title=title,
            metadata=metadata or {},
        )
        try:
            yield
        finally:
            self.release_floor(timeout=timeout)

    @contextmanager
    def input_window(
        self,
        *,
        title: str = "Entrada",
        metadata: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ):
        """Monta uma janela de entrada com posse exclusiva do terminal."""
        self.request_floor(
            timeout=timeout,
            kind=WindowKind.INPUT,
            title=title,
            metadata=metadata or {},
        )
        try:
            yield
        finally:
            self.release_floor(timeout=timeout)

    @contextmanager
    def selection_window(
        self,
        *,
        title: str = "Seleção",
        metadata: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ):
        """Monta uma janela de seleção com posse exclusiva do terminal."""
        self.request_floor(
            timeout=timeout,
            kind=WindowKind.SELECTION,
            title=title,
            metadata=metadata or {},
        )
        try:
            yield
        finally:
            self.release_floor(timeout=timeout)

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
        self._compositor.bump_transient_version()
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

    def _container(self, agent) -> AgentWindowState:
        """Get-or-create do container do agente (protegido por _lock reentrante)."""
        with self._lock:
            container = self._deck.get(agent)
            if container is None:
                style, label = self._agent_style(agent)
                container = AgentWindowState(
                    agent=_coerce_agent_name(agent), label=label, style=style
                )
                self._deck.windows[agent] = container
            return container

    def _agent_window_controller(self, agent) -> AgentWindowController:
        """Cria controller efêmero para mutações do estado de janela do agente."""
        return AgentWindowController(self._container(agent))

    def _combined_transient(self, term_lines: int) -> tuple[str, int]:
        """Empilha verticalmente o progresso transitório de todos os containers.

        Cada agente contribui suas linhas rolling sob o próprio label, em ordem
        estável de agente (o deck multi-agente). DEVE ser chamado com _lock retido.
        Retorna (texto_combinado, num_linhas) já limitado a 1/3 do terminal.
        """
        combined: list[str] = []
        for agt in sorted(self._deck.windows, key=str):
            container = self._deck.windows[agt]
            for msg in container.transient:
                combined.append(f"{container.label} {msg}")
        win_limit = max(1, term_lines // 3)
        combined = combined[-win_limit:]
        return "\n".join(combined), len(combined)

    def _print(self, renderable, kind: str = "generic", **kwargs):
        """Enfileira um evento de print para o compositor."""
        self._compositor.emit(PrintEvent(renderable, kwargs, kind=kind))

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

    def _build_pending_card_renderable(self, container: "AgentWindowState"):
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
            container = self._deck.get(agent)
            if container is not None:
                container.pending_kind = kind
                container.pending_question = question
        self._compositor.emit(PendingInputEvent(agent, kind, question))

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
            previous = self._deck.consume_completed_stream(agent)
            if previous is None:
                return False
        return _normalize_completed_content(previous) == normalized

    # ------------------------------------------------------------------
    # API pública de streaming
    # ------------------------------------------------------------------

    def start_message_stream(self, agent):
        """Inicia a área de renderização incremental para uma resposta."""
        if not self._console:
            return
        with self._lock:
            self._agent_window_controller(agent).start_stream(self, self._theme.name)

    def update_message_stream(self, agent, chunk):
        """Atualiza a resposta incremental com mais um chunk."""
        if not self._console or not chunk:
            return
        self._compositor.emit(LiveUpdateChunkEvent(agent, chunk))

    def finish_message_stream(self, agent, final_content: str, render_mode: str = "auto"):
        """Fecha o streaming preservando o conteúdo já mostrado."""
        if not self._console:
            return
        with self._lock:
            self._agent_window_controller(agent).finish_stream(self, final_content, render_mode)

    def abort_message_stream(self, agent):
        """Fecha o stream sem marcar a resposta como completa."""
        if not self._console:
            return
        with self._lock:
            self._agent_window_controller(agent).abort_stream(self)

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
                buf_version = self._compositor.mark_transient_changed()

                if self._run_above_prompt_fn:
                    combined_text, count = self._combined_transient(_term_lines)
                    if self._compositor.remember_combined_transient(combined_text):
                        enqueue_event = TransientWindowEvent(combined_text, count, buf_version)
                else:
                    fallback = (container.label, container.style)

            if enqueue_event is not None:
                self._compositor.emit(enqueue_event)
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
            container = self._deck.get(agent)
            is_transient_agent = bool(container and container.transient_active)
            changed = bool(container and container.transient)
            if container is not None:
                container.clear_transient_buffer()
            buf_version = self._compositor.mark_transient_changed(changed=changed)

            event = None
            if prompt_active and self._run_above_prompt_fn:
                _term_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
                combined_text, count = self._combined_transient(_term_lines)
                if not combined_text:
                    event = TransientClearEvent(buf_version)
                else:
                    self._compositor.remember_combined_transient(combined_text)
                    event = TransientWindowEvent(combined_text, count, buf_version)

        if event is not None:
            self._compositor.emit(event)
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
            container = self._deck.get(agent)
            if container is not None:
                container.elapsed = None

    def _get_agent_elapsed(self, agent: str) -> float | None:
        """Retorna o tempo decorrido do agente ou None."""
        with self._lock:
            container = self._deck.get(agent)
            return container.elapsed if container is not None else None

    def reset_visual_state(self, agent: str | None = None) -> None:
        """Reseta estado visual após cancelamento (Ctrl+C).

        Se agent for None, limpa todos os agentes.
        Caso contrário, limpa apenas o agente específico.
        """
        with self._lock:
            if agent:
                container = self._deck.get(agent)
                stream_agents = [agent] if (container and container.streaming) else []
                transient_agents = [agent] if (container and container.transient_active) else []
            else:
                stream_agents = [a for a, c in self._deck.windows.items() if c.streaming]
                transient_agents = [a for a, c in self._deck.windows.items() if c.transient_active]
                self._deck.completed_streams.clear()
                self._statuses.clear()
                self._last_persistent_kind = None
                self._last_persistent_agent = None

        for agt in stream_agents:
            self.abort_message_stream(agt)
        for agt in transient_agents:
            self.clear_agent_transient(agt)

        with self._lock:
            if agent:
                container = self._deck.get(agent)
                if container is not None:
                    container.elapsed = None
            else:
                for container in self._deck.windows.values():
                    container.elapsed = None

    def request_toolbar_refresh(self) -> None:
        """Enfileira evento para refresh periódico da toolbar."""
        self._compositor.emit(ToolbarTickEvent())

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

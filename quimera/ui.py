"""Componentes de `quimera.ui`."""
import os
import queue as _queue_module
import re
import sys
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any

from quimera.runtime.streaming import apply_stream_diff, normalize_stream_diff


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

    return text


def _normalize_stream_diff(diff) -> list[dict[str, str]]:
    """Normaliza o payload incremental aceito pelo renderer."""
    return normalize_stream_diff(diff, transform_text=strip_ansi)


def _apply_stream_diff(content: str, diff: list[dict[str, str]]) -> str:
    """Aplica operações incrementais de texto no buffer atual."""
    return apply_stream_diff(content, diff)


def _is_interactive_terminal() -> bool:
    """Check if we're running in an interactive terminal (not piped/captured)."""
    return sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'


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


@dataclass
class LiveAbortEvent:
    agent: str


@dataclass
class NoopEvent:
    done: threading.Event


def _agent_style(agent: str, get_plugin_style=None):
    """Retorna (color, label) para o agente; fallback para white/capitalize."""
    if get_plugin_style:
        result = get_plugin_style(agent.lower())
        if result:
            return result
    return ("white", f"🤖 {agent.capitalize()}")


class TerminalRenderer:
    """Camada exclusiva de apresentação no terminal. Nunca toca em persistência."""

    def __init__(self, theme: str | None = None, get_plugin_style=None, density: str | None = None):
        """Inicializa uma instância de TerminalRenderer."""
        if _RICH_AVAILABLE:
            self._console = Console(
                force_terminal=_is_interactive_terminal(),
                no_color=False
            )
        else:
            self._console = None
        self._theme = themes.get(theme or themes.DEFAULT_THEME)
        self._density = density if density in themes.DENSITY_OPTIONS else DEFAULT_DENSITY
        self._get_plugin_style = get_plugin_style
        self._live = None
        self._statuses = {}

        # Streams completados: agent -> final_content (atualizado sync antes de live_stop)
        self._completed_streams = {}
        # Agents com stream ativo (atualizado sync, protegido por _lock)
        self._active_stream_agents = set()
        # Lock protege _completed_streams, _active_stream_agents e _statuses
        self._lock = threading.RLock()

        # Fila com backpressure (item 3): produtor bloqueia se fila cheia
        self._queue: _queue_module.Queue = _queue_module.Queue(maxsize=512)
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

    # ------------------------------------------------------------------
    # Ciclo de vida (item 1)
    # ------------------------------------------------------------------

    def close(self, timeout: float = 5.0) -> None:
        """Encerra o writer thread graciosamente, aguardando eventos pendentes."""
        self._queue.put(_STOP)
        self._writer_thread.join(timeout=timeout)

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
        """Single writer: processa todos os eventos de UI sequencialmente."""
        _stream_states: dict[str, dict] = {}  # agent -> {live, content, label, style, theme_name}

        while True:
            event = self._queue.get()
            if event is _STOP:
                for state in _stream_states.values():
                    try:
                        state["live"].stop()
                    except Exception:
                        pass
                break

            # Resiliência (item 2): exceção em qualquer evento não mata o writer
            try:
                self._handle_event(event, _stream_states)
            except Exception:
                pass  # silencia para manter o loop vivo; erros visuais não devem travar o app

    def _handle_event(self, event, _stream_states: dict) -> None:
        """Despacha e processa um único evento de UI."""
        if isinstance(event, PrintEvent):
            if self._console:
                self._console.print(event.renderable, **event.kwargs)

        elif isinstance(event, LiveStartEvent):
            _stream_states[event.agent] = event.state
            event.state["live"].start()

        elif isinstance(event, LiveUpdateChunkEvent):
            # Coalescing (item 3): drena chunks consecutivos do mesmo agente antes de renderizar
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
                    # Devolve o evento não relacionado para reprocessamento
                    # Não há "unget" em Queue; processamos inline imediatamente
                    self._handle_event(next_ev, _stream_states)
                    break

            state = _stream_states.get(agent)
            if not state:
                return
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
            renderable = self._build_stream_renderable(
                state["theme_name"], state["label"], state["style"], state["content"]
            )
            state["live"].update(renderable, refresh=True)

        elif isinstance(event, LiveStopEvent):
            state = _stream_states.pop(event.agent, None)
            if state and self._console:
                renderable = self._build_stream_renderable(
                    state["theme_name"], state["label"], state["style"], event.final_content
                )
                state["live"].update(renderable, refresh=True)
                state["live"].stop()
                if state["theme_name"] == "rule":
                    self._console.print(Rule(style="dim"))
                self._console.print()

        elif isinstance(event, LiveAbortEvent):
            state = _stream_states.pop(event.agent, None)
            if state:
                state["live"].stop()
            if self._console:
                self._console.print()

        elif isinstance(event, NoopEvent):
            event.done.set()

    def flush(self):
        """Aguarda o writer thread processar todos os eventos pendentes."""
        done = threading.Event()
        self._queue.put(NoopEvent(done))
        done.wait(timeout=5)

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _agent_style(self, agent: str):
        """Retorna (color, label) para o agente."""
        return _agent_style(agent, self._get_plugin_style)

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
        return Text(label, style=f"bold {style}")

    def _build_turn_body(self, theme_name: str, label: str, style: str, content: str, streaming: bool = False):
        """Monta corpo textual do turno."""
        content_md = Markdown(content or "")
        if theme_name == "panel":
            title = f"[bold {style}]{label}[/bold {style}]" if streaming else None
            return Panel(content_md, title=title, border_style=style, padding=(0, 1))
        if theme_name == "chat":
            return Padding(content_md, pad=(0, 0, 0, 4))
        if theme_name == "minimal":
            return Padding(content_md, pad=(0, 0, 0, 2))
        return content_md

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
    ):
        """Monta bloco estruturado de turno: header -> corpo -> tools."""
        parts = []
        if include_header:
            parts.append(self._build_turn_header(theme_name, label, style))
        if content is not None:
            parts.append(self._build_turn_body(theme_name, label, style, content, streaming=streaming))
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
        """Monta o renderable dinâmico usado no streaming."""
        return self._render_turn_block(
            theme_name,
            label,
            style,
            content=content,
            include_header=False,
            streaming=True,
        )

    # ------------------------------------------------------------------
    # API pública de exibição de mensagens
    # ------------------------------------------------------------------

    def show_message(self, agent, content):
        """Exibe mensagem usando o tema ativo."""
        style, label = self._agent_style(agent)
        clean_content = strip_ansi(str(content))
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
            )
            self._print(block)
        else:
            print(f"\n{label}: {clean_content}\n")

    def _consume_completed_stream(self, agent, content: str) -> bool:
        """Evita render final duplicado quando a resposta já foi exibida via streaming."""
        normalized = content.strip()
        with self._lock:
            previous = self._completed_streams.get(agent)
            if previous is None:
                return False
            del self._completed_streams[agent]
        return previous.strip() == normalized

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
        self._print(self._render_turn_block(theme_name, label, style, content=None, include_header=True))

        initial = self._build_stream_renderable(theme_name, label, style, "")
        live = Live(initial, console=self._console, refresh_per_second=20, transient=False, auto_refresh=True)
        state = {
            "content": "",
            "label": label,
            "style": style,
            "theme_name": theme_name,
            "live": live,
        }
        self._queue.put(LiveStartEvent(agent, state))

    def update_message_stream(self, agent, chunk):
        """Atualiza a resposta incremental com mais um chunk."""
        if not self._console or not chunk:
            return
        self._queue.put(LiveUpdateChunkEvent(agent, chunk))

    def finish_message_stream(self, agent, final_content: str):
        """Fecha o streaming preservando o conteúdo já mostrado."""
        if not self._console:
            return
        clean_content = strip_ansi(str(final_content or ""))
        # Atualiza _completed_streams de forma síncrona, antes de enfileirar live_stop,
        # para que _consume_completed_stream em show_message funcione corretamente.
        with self._lock:
            self._completed_streams[agent] = clean_content
            self._active_stream_agents.discard(agent)
        self._queue.put(LiveStopEvent(agent, clean_content))

    def abort_message_stream(self, agent):
        """Fecha o stream sem marcar a resposta como completa."""
        if not self._console:
            return
        with self._lock:
            self._active_stream_agents.discard(agent)
        self._queue.put(LiveAbortEvent(agent))

    # ------------------------------------------------------------------
    # Exibição de tipos especiais
    # ------------------------------------------------------------------

    def show_no_response(self, agent):
        """Exibe no response."""
        _, label = self._agent_style(agent)
        message = "sem resposta válida"
        if self._console:
            style, icon = ROLE_STYLES["info"]
            line = Text.assemble((f"{icon} ", f"dim {style}"), (f"{label}: {message}", "dim"))
            self._print(line)
        else:
            print(f"{label}: {message}")

    def show_system(self, message):
        """Exibe system."""
        clean_message = strip_ansi(str(message))
        if self._console:
            style, icon = ROLE_STYLES["system"]
            line = Text.assemble((f"{icon} ", f"dim {style}"), (clean_message, style))
            line.no_wrap = False
            line.overflow = "fold"
            self._print(line)
        else:
            print(clean_message)

    def show_plain(self, message, agent=None):
        """Exibe plain."""
        clean_message = strip_ansi(str(message))
        if self._console:
            if agent:
                style, label = self._agent_style(agent)
                line = Text.assemble(
                    (label, f"bold {style}"),
                    (" "),
                    (clean_message,),
                )
            else:
                line = Text.assemble(
                    ("·", "dim"),
                    (" "),
                    (clean_message, "dim"),
                )
            line.no_wrap = False
            line.overflow = "fold"
            self._print(line)
        else:
            prefix = f"{agent}: " if agent else ""
            print(f"{prefix}{clean_message}")

    def show_error(self, message):
        """Exibe error."""
        clean_message = strip_ansi(str(message))
        if self._console:
            style, icon = ROLE_STYLES["error"]
            line = Text.assemble((f"{icon} ", style), (clean_message, "red"))
            self._print(line)
        else:
            print(clean_message)

    def show_warning(self, message):
        """Exibe warning."""
        clean_message = strip_ansi(str(message))
        if self._console:
            style, icon = ROLE_STYLES["warning"]
            line = Text.assemble((f"{icon} ", style), (clean_message, "yellow"))
            self._print(line)
        else:
            print(clean_message)

    def show_turn_summary(self, agent: str | None, detail: dict) -> None:
        """Exibe resumo do turno como tabela Rich compacta."""
        if not self._console or not _RICH_AVAILABLE:
            return
        tools = detail.get("tools", []) if isinstance(detail, dict) else []
        if not tools:
            return
        style, label = self._agent_style(agent) if agent else ("dim", "sistema")
        turn_id = detail.get("turn_id", "")
        width = getattr(self._console, "width", 80)
        compact_tools_layout = width < 72
        table_padding = (0, 0) if compact_tools_layout else (0, 1)
        table = Table(
            box=rich_box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold",
            padding=table_padding,
        )
        table.add_column("Ferramenta", style="cyan", no_wrap=False, overflow="fold")
        if compact_tools_layout:
            table.add_column("St", width=2, justify="center")
            table.add_column("Dur", width=5, justify="right", style="dim")
        else:
            table.add_column("Status", width=6, justify="center")
            table.add_column("Duração", width=7, justify="right", style="dim")
        if not compact_tools_layout:
            table.add_column("Detalhes", style="dim", overflow="fold")
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_name = markup_escape(str(tool.get("tool") or "ferramenta"))
            status = tool.get("status") or "unknown"
            dur_ms = tool.get("duration_ms")
            if isinstance(dur_ms, int) and dur_ms >= 0:
                dur_str = f"{dur_ms}ms" if dur_ms < 1000 else f"{dur_ms / 1000:.1f}s"
            else:
                dur_str = "—"
            if status in ("ok", "success"):
                status_cell = Text("✓", style="green")
            elif status == "error":
                status_cell = Text("✗", style="bold red")
            elif status in ("running", "unknown"):
                status_cell = Text("…", style="yellow")
            else:
                status_max = 2 if compact_tools_layout else 5
                status_cell = Text(status[:status_max], style="dim")
            inp = tool.get("input")
            if isinstance(inp, dict):
                if inp.get("cmd"):
                    details = markup_escape(f"cmd: {inp['cmd']}")
                elif inp.get("path"):
                    details = markup_escape(f"path: {inp['path']}")
                else:
                    parts = [f"{k}={v}" for k, v in inp.items() if v is not None][:2]
                    details = markup_escape(", ".join(parts))
            else:
                details = ""
            err = tool.get("error")
            if isinstance(err, dict) and err.get("message"):
                details = markup_escape(f"erro: {err['message']}")
            if compact_tools_layout:
                tool_cell = Text(tool_name, style="cyan")
                if details:
                    tool_cell.append(f"\n{details}", style="dim")
                table.add_row(tool_cell, status_cell, dur_str)
            else:
                table.add_row(tool_name, status_cell, dur_str, details)
        block = self._render_turn_block(
            self._theme.name,
            label,
            style,
            tools_table=table,
            turn_id=str(turn_id),
            include_header=False,
            include_footer_rule=True,
        )
        self._print(block)

    def show_handoff(self, from_agent, to_agent, task=None):
        """Exibe handoff."""
        _, from_label = self._agent_style(from_agent)
        _, to_label = self._agent_style(to_agent)
        arrow = f"{from_label} → {to_label}"
        if task:
            arrow += f"  ·  {task}"
        if self._console:
            style, icon = ROLE_STYLES["info"]
            line = Text.assemble((f"{icon} ", f"dim {style}"), (arrow, "dim"))
            self._print(line)
        else:
            print(arrow)

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

        return Panel(
            table,
            title="[bold blue]Agentes em Execução[/]",
            border_style="blue",
            padding=(0, 1)
        )

    @contextmanager
    def live_status(self, agents):
        """Context manager para exibir status dinâmico de múltiplos agentes."""
        if not self._console or not _RICH_AVAILABLE:
            yield
            return

        with self._lock:
            self._statuses = {agent: "inicializando..." for agent in agents}
            self._live = Live(
                self._render_status_panel(),
                console=self._console,
                refresh_per_second=10,
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
            # Se já estamos em modo Live (paralelo), retornamos um proxy que atualiza o painel global
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

            # Caso contrário, usa o spinner padrão do Rich (sequencial)
            return self._console.status(initial)

        return nullcontext(None)

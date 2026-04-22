"""Componentes de `quimera.ui`."""
import os
import re
import sys
import threading
from contextlib import contextmanager, nullcontext

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


def _agent_style(agent: str, get_plugin_style=None):
    """Retorna (color, label) para o agente; fallback para white/capitalize."""
    if get_plugin_style:
        result = get_plugin_style(agent.lower())
        if result:
            return result
    return ("white", f"🤖 {agent.capitalize()}")


class TerminalRenderer:
    """Camada exclusiva de apresentação no terminal. Nunca toca em persistência."""

    def __init__(self, theme: str | None = None, get_plugin_style=None):
        """Inicializa uma instância de TerminalRenderer."""
        if _RICH_AVAILABLE:
            self._console = Console(
                force_terminal=_is_interactive_terminal(),
                no_color=False
            )
        else:
            self._console = None
        self._theme = themes.get(theme or themes.DEFAULT_THEME)
        self._get_plugin_style = get_plugin_style
        self._live = None
        self._statuses = {}
        self._message_streams = {}
        self._completed_streams = {}
        self._lock = threading.RLock()

    def _agent_style(self, agent: str):
        """Retorna (color, label) para o agente."""
        return _agent_style(agent, self._get_plugin_style)

    def show_message(self, agent, content):
        """Exibe message usando o tema ativo."""
        style, label = self._agent_style(agent)
        clean_content = strip_ansi(str(content))
        if self._consume_completed_stream(agent, clean_content):
            return
        if self._console:
            self._theme.render(self._console, label, style, Markdown(clean_content))
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

    def _build_stream_renderable(self, theme_name: str, label: str, style: str, content: str):
        """Monta o renderable dinâmico usado no streaming."""
        content_md = Markdown(content or "")
        if theme_name == "panel":
            return Panel(
                content_md,
                title=f"[bold {style}]{label}[/bold {style}]",
                border_style=style,
                padding=(0, 1),
            )
        if theme_name == "chat":
            return Padding(content_md, pad=(0, 0, 0, 4))
        if theme_name == "minimal":
            return Padding(content_md, pad=(0, 0, 0, 2))
        return content_md

    def start_message_stream(self, agent):
        """Inicia a área de renderização incremental para uma resposta."""
        if not self._console:
            return
        style, label = self._agent_style(agent)
        with self._lock:
            if agent in self._message_streams:
                return
            theme_name = self._theme.name
            live = None
            self._console.print()
            if theme_name == "rule":
                self._console.print(Rule(f"[bold {style}]{label}[/bold {style}]", style=f"dim {style}"))
            elif theme_name == "chat":
                header = Table.grid(expand=True, padding=(0, 1))
                header.add_column(width=2)
                header.add_column(ratio=1)
                header.add_row(Text("●", style=f"bold {style}"), Text(label, style=f"bold {style}"))
                self._console.print(header)
            elif theme_name == "minimal":
                self._console.print(Text(f"▶ {label}", style=f"bold {style}"))
            initial = self._build_stream_renderable(theme_name, label, style, "")
            live = Live(initial, console=self._console, refresh_per_second=20, transient=False, auto_refresh=True)
            live.start()
            self._message_streams[agent] = {
                "content": "",
                "label": label,
                "style": style,
                "theme_name": theme_name,
                "live": live,
            }

    def update_message_stream(self, agent, chunk):
        """Atualiza a resposta incremental com mais um chunk."""
        if not self._console or not chunk:
            return
        with self._lock:
            state = self._message_streams.get(agent)
            if state is None:
                return
            if isinstance(chunk, dict):
                state["content"] = _apply_stream_diff(state["content"], _normalize_stream_diff(chunk.get("diff")))
                text = chunk.get("text")
                if text and not chunk.get("diff"):
                    state["content"] += strip_ansi(str(text))
            else:
                state["content"] += strip_ansi(str(chunk))
            renderable = self._build_stream_renderable(
                state["theme_name"],
                state["label"],
                state["style"],
                state["content"],
            )
            state["live"].update(renderable, refresh=True)

    def finish_message_stream(self, agent, final_content: str):
        """Fecha o streaming preservando o conteúdo já mostrado."""
        if not self._console:
            return
        clean_content = strip_ansi(str(final_content or ""))
        with self._lock:
            state = self._message_streams.pop(agent, None)
            if state is None:
                return
            renderable = self._build_stream_renderable(
                state["theme_name"],
                state["label"],
                state["style"],
                clean_content,
            )
            state["live"].update(renderable, refresh=True)
            state["live"].stop()
            if state["theme_name"] == "rule":
                self._console.print(Rule(style="dim"))
            self._console.print()
            self._completed_streams[agent] = clean_content

    def abort_message_stream(self, agent):
        """Fecha o stream sem marcar a resposta como completa."""
        if not self._console:
            return
        with self._lock:
            state = self._message_streams.pop(agent, None)
            if state is None:
                return
            state["live"].stop()
            self._console.print()

    def show_no_response(self, agent):
        """Exibe no response."""
        _, label = self._agent_style(agent)
        message = "sem resposta válida"
        if self._console:
            self._console.print(f"\n[dim]{label}: {message}[/dim]\n")
        else:
            print(f"\n{label}: {message}\n")

    def show_system(self, message):
        """Exibe system."""
        clean_message = strip_ansi(str(message))
        if self._console:
            self._console.print(f"[dim]{markup_escape(clean_message)}[/dim]")
        else:
            print(clean_message)

    def show_plain(self, message, agent=None):
        # Remove ANSI escape sequences to prevent display issues
        """Exibe plain."""
        clean_message = strip_ansi(str(message))
        if self._console:
            if agent:
                style, label = self._agent_style(agent)
                table = Table.grid(expand=True, padding=(0, 1))
                table.add_column(no_wrap=True)
                table.add_column(ratio=1)
                table.add_row(
                    Text(label, style=f"bold {style}"),
                    Text(clean_message),
                )
                self._console.print(table, soft_wrap=True)
            else:
                table = Table.grid(expand=True, padding=(0, 1))
                table.add_column(width=2)
                table.add_column(ratio=1)
                table.add_row(
                    Text("·", style="dim"),
                    Text(clean_message, style="dim"),
                )
                self._console.print(table, soft_wrap=True)
        else:
            prefix = f"{agent}: " if agent else ""
            print(f"{prefix}{clean_message}")

    def show_error(self, message):
        """Exibe error."""
        clean_message = strip_ansi(str(message))
        if self._console:
            self._console.print(f"[bold red]{markup_escape(clean_message)}[/bold red]")
        else:
            print(clean_message)

    def show_warning(self, message):
        """Exibe warning."""
        clean_message = strip_ansi(str(message))
        if self._console:
            self._console.print(f"[yellow]{markup_escape(clean_message)}[/yellow]")
        else:
            print(clean_message)

    def show_handoff(self, from_agent, to_agent, task=None):
        """Exibe handoff."""
        _, from_label = self._agent_style(from_agent)
        _, to_label = self._agent_style(to_agent)
        message = f"[handoff] {from_label} -> {to_label}"
        if task:
            message += f" | task: {task}"
        self.show_system(message)

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

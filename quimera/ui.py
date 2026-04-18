"""Componentes de `quimera.ui`."""
import os
import re
import sys
import threading
from contextlib import contextmanager, nullcontext


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


def _is_interactive_terminal() -> bool:
    """Check if we're running in an interactive terminal (not piped/captured)."""
    return sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'


try:
    from rich.console import Console, Group
    from rich.markdown import Markdown
    from rich.markup import escape as markup_escape
    from rich.panel import Panel
    from rich.live import Live
    from rich.table import Table

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

import quimera.plugins as plugins
import quimera.themes as themes


def _agent_style(agent: str):
    """Retorna (color, label) para o agente; fallback para white/capitalize."""
    plugin = plugins.get(agent.lower())
    if plugin:
        color, label = plugin.style
        icon = getattr(plugin, "icon", "🤖")
        return (color, f"{icon} {label}")
    return ("white", f"🤖 {agent.capitalize()}")


class TerminalRenderer:
    """Camada exclusiva de apresentação no terminal. Nunca toca em persistência."""

    _MAX_WIDTH = 96

    def __init__(self, theme: str | None = None):
        """Inicializa uma instância de TerminalRenderer."""
        if _RICH_AVAILABLE:
            self._console = Console(
                width=self._MAX_WIDTH,
                force_terminal=_is_interactive_terminal(),
                no_color=False
            )
        else:
            self._console = None
        self._theme = themes.get(theme or themes.DEFAULT_THEME)
        self._live = None
        self._statuses = {}
        self._lock = threading.RLock()

    def show_message(self, agent, content):
        """Exibe message usando o tema ativo."""
        style, label = _agent_style(agent)
        clean_content = strip_ansi(str(content))
        if self._console:
            self._theme.render(self._console, label, style, Markdown(clean_content))
        else:
            print(f"\n{label}: {clean_content}\n")

    def show_no_response(self, agent):
        """Exibe no response."""
        _, label = _agent_style(agent)
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
                style, label = _agent_style(agent)
                prefix = f"[{style}]{label}:[/] "
                self._console.print(f"{prefix}{markup_escape(clean_message)}")
            else:
                self._console.print(markup_escape(clean_message))
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
        _, from_label = _agent_style(from_agent)
        _, to_label = _agent_style(to_agent)
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
                style, label = _agent_style(agent)

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

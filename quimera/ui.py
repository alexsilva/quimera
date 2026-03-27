try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False


class TerminalRenderer:
    """Camada exclusiva de apresentação no terminal. Nunca toca em persistência."""

    _AGENT_STYLES = {
        "claude": ("blue", "Claude"),
        "codex": ("green", "Codex"),
    }
    _MAX_WIDTH = 96

    def __init__(self):
        if _RICH_AVAILABLE:
            self._console = Console(width=self._MAX_WIDTH)
        else:
            self._console = None

    def show_message(self, agent, content):
        style, label = self._AGENT_STYLES.get(agent.lower(), ("white", agent.capitalize()))
        if self._console:
            self._console.print()
            self._console.print(
                Panel(
                    Markdown(content),
                    title=f"[bold white on {style}] {label} [/bold white on {style}]",
                    border_style=style,
                    padding=(0, 1),
                )
            )
        else:
            print(f"\n{label}: {content}\n")

    def show_no_response(self, agent):
        _, label = self._AGENT_STYLES.get(agent.lower(), ("white", agent.capitalize()))
        if self._console:
            self._console.print(f"\n[dim]{label}: [sem resposta válida][/dim]\n")
        else:
            print(f"\n{label}: [sem resposta válida]\n")

    def show_system(self, message):
        if self._console:
            self._console.print(f"[dim]{message}[/dim]")
        else:
            print(message)

    def show_plain(self, message):
        if self._console:
            self._console.print(message)
        else:
            print(message)

    def show_error(self, message):
        if self._console:
            self._console.print(f"[bold red]{message}[/bold red]")
        else:
            print(message)

    def show_warning(self, message):
        if self._console:
            self._console.print(f"[yellow]{message}[/yellow]")
        else:
            print(message)

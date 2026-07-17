"""Sistema de temas para renderização de mensagens no terminal."""
from dataclasses import dataclass
from typing import Callable

try:
    from rich.console import Group
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False


# ---------------------------------------------------------------------------
# Role-based palette: role -> (rich_style, icon)
# ---------------------------------------------------------------------------
ROLE_STYLES: dict[str, tuple[str, str]] = {
    "system":  ("blue",        "⚙"),
    "error":   ("bold red",    "✗"),
    "warning": ("yellow",      "⚠"),
    "info":    ("cyan",        "ℹ"),
    "tool":    ("dim cyan",    "⚒"),
    "human":   ("bold green",  "❯"),
}

DEFAULT_DENSITY = "normal"
DENSITY_OPTIONS = ("normal", "compact")


# ---------------------------------------------------------------------------
# Funções de renderização por tema
# ---------------------------------------------------------------------------

def _render_panel(console, label, style, content_md):
    """Painel com borda colorida — visual atual padrão."""
    console.print()
    console.print(
        Panel(
            content_md,
            title=f"[bold {style}]{label}[/bold {style}]",
            border_style=style,
            padding=(0, 1),
        )
    )


def _render_chat(console, label, style, content_md):
    """Cabeçalho enxuto com trilho lateral para separar melhor o corpo."""
    console.print()
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=2)
    table.add_column(ratio=1)
    table.add_row(
        Text("●", style=f"bold {style}"),
        Group(
            Text(label, style=f"bold {style}"),
            Padding(content_md, pad=(0, 0, 0, 2)),
        ),
    )
    console.print(table)


def _render_rule(console, label, style, content_md):
    """Separador horizontal com nome centralizado + conteúdo livre."""
    console.print()
    console.print(Rule(f"[bold {style}]{label}[/bold {style}]", style=f"dim {style}"))
    console.print(content_md)
    console.print(Rule(style="dim"))


def _render_minimal(console, label, style, content_md):
    """Seta ▶ colorida + nome, conteúdo sem adorno algum."""
    console.print()
    console.print(Text(f"▶ {label}", style=f"bold {style}"))
    console.print(content_md)


def _render_card(console, label, style, content_md):
    """Card limpo com borda sutil e cantos suaves — visual moderno."""
    console.print()
    from rich.panel import Panel as RichPanel
    console.print(
        RichPanel(
            content_md,
            title=f"[bold {style}]{label}[/bold {style}]",
            border_style=f"dim {style}",
            padding=(0, 1),
            subtitle="▸",
            subtitle_align="right",
        )
    )


def _render_line(console, label, style, content_md):
    """Linha com rótulo — estilo log/compacto."""
    console.print(Text(f"{label}", style=f"bold {style}"), content_md)


# ---------------------------------------------------------------------------
# Dataclass e registro
# ---------------------------------------------------------------------------

@dataclass
class Theme:
    """Representa um tema de exibição de mensagens."""
    name: str
    description: str
    render_fn: Callable

    def render(self, console, label, style, content_md):
        """Renderiza a mensagem usando a função de tema."""
        self.render_fn(console, label, style, content_md)


THEMES: dict[str, Theme] = {
    "panel": Theme(
        name="panel",
        description="Painel com borda colorida (padrão)",
        render_fn=_render_panel,
    ),
    "chat": Theme(
        name="chat",
        description="Bullet ● + conteúdo indentado (estilo Slack/Discord)",
        render_fn=_render_chat,
    ),
    "rule": Theme(
        name="rule",
        description="Separador horizontal com nome + conteúdo livre",
        render_fn=_render_rule,
    ),
    "minimal": Theme(
        name="minimal",
        description="Seta ▶ + nome colorido, sem bordas",
        render_fn=_render_minimal,
    ),
    "card": Theme(
        name="card",
        description="Card com borda sutil e cantos suaves (moderno)",
        render_fn=_render_card,
    ),
    "line": Theme(
        name="line",
        description="Linha única com rótulo colorido",
        render_fn=_render_line,
    ),
}

DEFAULT_THEME = "chat"


def get(name: str) -> Theme:
    """Retorna o tema pelo nome; fallback para o padrão se não encontrado."""
    return THEMES.get(name) or THEMES[DEFAULT_THEME]


def names() -> list[str]:
    """Retorna lista de nomes de temas disponíveis."""
    return list(THEMES.keys())

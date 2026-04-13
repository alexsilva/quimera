"""Sistema de temas para renderização de mensagens no terminal."""
from dataclasses import dataclass
from typing import Callable

try:
    from rich.markdown import Markdown
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.text import Text

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False


# ---------------------------------------------------------------------------
# Funções de renderização por tema
# ---------------------------------------------------------------------------

def _render_panel(console, label, style, content_md):
    """Painel com borda colorida — visual atual padrão."""
    console.print()
    console.print(
        Panel(
            content_md,
            title=f"[bold white on {style}] {label} [/bold white on {style}]",
            border_style=style,
            padding=(0, 1),
        )
    )


def _render_chat(console, label, style, content_md):
    """Bullet ● colorido + conteúdo indentado (estilo Slack/Discord)."""
    console.print()
    console.print(Text(f"● {label}", style=f"bold {style}"))
    console.print(Padding(content_md, pad=(0, 0, 0, 2)))


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
}

DEFAULT_THEME = "panel"


def get(name: str) -> Theme:
    """Retorna o tema pelo nome; fallback para o padrão se não encontrado."""
    return THEMES.get(name) or THEMES[DEFAULT_THEME]


def names() -> list[str]:
    """Retorna lista de nomes de temas disponíveis."""
    return list(THEMES.keys())

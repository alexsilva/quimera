"""Identidade visual compartilhada entre renderers (banner/logo)."""
from rich.text import Text

# Gradiente vertical do banner: violeta -> ciano.
BANNER_GRADIENT = ((0xC0, 0x84, 0xFC), (0x22, 0xD3, 0xEE))


def banner_gradient_text(message: str) -> Text:
    """Monta o banner com gradiente de cor linha a linha, sem quebra de linha."""
    lines = str(message).split("\n")
    (r1, g1, b1), (r2, g2, b2) = BANNER_GRADIENT
    steps = max(len(lines) - 1, 1)
    text = Text(no_wrap=True, overflow="ignore")
    for index, line in enumerate(lines):
        ratio = index / steps
        color = "#{:02x}{:02x}{:02x}".format(
            round(r1 + (r2 - r1) * ratio),
            round(g1 + (g2 - g1) * ratio),
            round(b1 + (b2 - b1) * ratio),
        )
        text.append(line, style=f"bold {color}")
        if index < len(lines) - 1:
            text.append("\n")
    return text

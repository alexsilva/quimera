from pathlib import Path

import pytest
from rich.console import Console

from quimera.ui import TerminalRenderer


BASELINE_WIDTHS = (80, 60, 40)
BASELINE_THEMES = ("panel", "chat", "rule", "minimal")
BASELINE_DENSITIES = ("normal", "compact")
BASELINE_DIR = Path(__file__).parent / "fixtures" / "ui_baseline"


def _render_baseline_sample(*, theme: str, density: str, width: int) -> str:
    renderer = TerminalRenderer(theme=theme, density=density)
    renderer._console = Console(width=width, record=True, force_terminal=False)

    renderer.show_system(
        "Sessão ativa: /home/alex/PycharmProjects/quimera/data/logs/2026-04-30/"
        "sessao-2026-04-30-222821.jsonl"
    )
    renderer.show_plain("Alex: validar baseline visual oficial")
    renderer.show_message(
        "codex",
        "Implementação concluída.\nAjustes de wrapping e alinhamento preservados.",
    )
    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "turn_1650",
            "tools": [
                {
                    "tool": "exec_command",
                    "status": "ok",
                    "duration_ms": 87,
                    "input": {"cmd": "pytest -q tests/test_ui.py tests/test_ui_nonregression.py"},
                }
            ],
        },
    )

    renderer.flush()
    return renderer._console.export_text()


@pytest.mark.parametrize("theme", BASELINE_THEMES)
@pytest.mark.parametrize("density", BASELINE_DENSITIES)
@pytest.mark.parametrize("width", BASELINE_WIDTHS)
def test_ui_visual_baseline(theme: str, density: str, width: int):
    baseline_path = BASELINE_DIR / f"{theme}-{density}-{width}.txt"
    assert baseline_path.exists(), f"baseline ausente: {baseline_path}"

    expected = baseline_path.read_text(encoding="utf-8")
    rendered = _render_baseline_sample(theme=theme, density=density, width=width)
    assert rendered == expected

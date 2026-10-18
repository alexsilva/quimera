"""Smoke test do modo pipe: o app real com stdin redirecionado.

Sem TTY o ``cli`` usa o ``PlainTextRenderer`` e o ``SimpleInputGate``: as
linhas do stdin são consumidas em ordem por quem pergunta. Aqui a segunda
linha responde ao ``ask_user`` do agente fake (opção 2 = Rust), em vez de
virar uma segunda submissão do chat.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pipe_mode_answers_ask_user_with_the_next_stdin_line(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "HOME": str(tmp_path / "home"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "QUIMERA_MCP_TOKEN": "pipe-mode-test-token",
            "QUIMERA_FAKE_API_KEY": "test",
        }
    )
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "quimera.cli",
            "--test",
            "--agents",
            "fake-openai-mcp-cli",
            "--visibility",
            "full",
        ],
        cwd=tmp_path,
        input="faça uma pergunta ask_user\n2\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )

    output = proc.stdout
    assert proc.returncode == 0, output + proc.stderr
    assert "\x1b[" not in output, "modo pipe não pode escrever escapes ANSI"
    assert "EOF when reading" not in output
    assert "Qual linguagem você prefere?" in output
    assert "Selecione (1-4): 2" in output
    assert "Rust" in output

"""Smoke test da inicialização real da CLI num pseudo-terminal (TUI Textual).

O processo é ``python -m quimera``; o teste espera o cabeçalho de boot
aparecer na tela antes de digitar ``/exit``, para validar que a TUI subiu de
fato e encerra limpo pelo comando.
"""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quimera.ui.commands import MSG_CHAT_STARTED

REPO_ROOT = Path(__file__).resolve().parents[1]
#: A TUI quebra a linha de boot em segmentos; o começo dela basta como marcador.
BOOT_MARKER = MSG_CHAT_STARTED.strip().split(" (")[0]
_ANSI_RE = re.compile(r"\x1b\[[0-9;?<>=]*[a-zA-Z]|\x1b[()][A-Za-z0-9]|\x1b[^\[()]")


def _plain(raw: bytes) -> str:
    return _ANSI_RE.sub("", raw.decode("utf-8", errors="replace"))


def _run_cli_in_pty(tmp_path: Path, extra_args: list[str]) -> tuple[int, str]:
    """Sobe o app num PTY, espera o boot e sai por /exit; devolve (rc, tela)."""
    master_fd, slave_fd = pty.openpty()
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "HOME": str(tmp_path / "home"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "QUIMERA_MCP_TOKEN": "interactive-startup-test-token",
            "QUIMERA_FAKE_API_KEY": "test",
            "COLUMNS": "100",
            "LINES": "40",
        }
    )
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "quimera",
            "--test",
            "--agents",
            "fake-openai",
            "--visibility",
            "quiet",
            *extra_args,
        ],
        cwd=tmp_path,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)

    output = bytearray()
    booted_at: float | None = None
    typed_at: float | None = None
    confirmed = False
    deadline = time.monotonic() + 90
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            now = time.monotonic()
            if booted_at is None and BOOT_MARKER in _plain(bytes(output)):
                # A TUI só processa teclas depois de montada: o marcador de
                # boot na tela garante que o /exit chega ao prompt.
                booted_at = now
            elif booted_at is not None and typed_at is None and now - booted_at > 1.0:
                os.write(master_fd, b"/exit\r")
                typed_at = now
            elif typed_at is not None and not confirmed and now - typed_at > 1.5:
                # Digitar "/" abre a lista de comandos; o primeiro Enter aceita
                # a sugestão e o segundo submete, como para o humano. Com o
                # comando já submetido, um Enter no prompt vazio é inócuo.
                os.write(master_fd, b"\r")
                confirmed = True
            if proc.poll() is not None:
                break
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
    finally:
        os.close(master_fd)

    return proc.returncode, _plain(bytes(output))


@pytest.mark.parametrize("extra_args", [[], ["--no-mcp"]])
def test_cli_app_starts_interactively_and_exits_cleanly(tmp_path, extra_args):
    """A TUI sobe no PTY, mostra o cabeçalho de boot e encerra com /exit."""
    returncode, screen = _run_cli_in_pty(tmp_path, extra_args)

    assert BOOT_MARKER in screen, screen[-2000:]
    assert returncode == 0, screen[-2000:]

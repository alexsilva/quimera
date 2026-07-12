"""Smoke tests that exercise the real CLI/app startup through a pseudo-terminal."""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cli_in_pty(tmp_path: Path, extra_args: list[str]) -> subprocess.CompletedProcess[str]:
    """Start the app as an interactive process and exit via the normal /exit command."""

    master_fd, slave_fd = pty.openpty()
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "HOME": str(tmp_path / "home"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "QUIMERA_MCP_TOKEN": "interactive-startup-test-token",
        }
    )
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "quimera.cli",
            "--agents",
            "claude",
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
    sent_exit = False
    deadline = time.monotonic() + 30
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
            if not sent_exit:
                os.write(master_fd, b"/exit\n")
                sent_exit = True
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

    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        output.decode("utf-8", errors="replace"),
        "",
    )


@pytest.mark.parametrize("extra_args", [[], ["--no-mcp"]])
def test_cli_app_starts_interactively_and_exits_cleanly(tmp_path, extra_args):
    """Verifica que a CLI inicia em modo interativo via PTY e encerra corretamente com /exit."""
    result = _run_cli_in_pty(tmp_path, extra_args)

    assert result.returncode == 0, result.stdout
    assert "/exit" in result.stdout

"""Componentes de `quimera.sandbox.bwrap`."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from quimera.modes import ExecutionMode

_HOME_DIR = str(Path.home())
_COMMON_RO_PATHS = ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt", _HOME_DIR]

# Diretórios de dados de agentes que precisam de escrita mesmo em modos restritos
_AGENT_RW_PATHS = [
    str(Path.home() / ".codex"),
    str(Path.home() / ".local" / "share" / "opencode"),
]


def is_bwrap_available() -> bool:
    """Retorna True se bubblewrap (bwrap) estiver instalado no sistema."""
    return shutil.which("bwrap") is not None


def build_bwrap_cmd(mode: ExecutionMode, working_dir: str, cmd: list[str]) -> list[str]:
    """Envolve cmd com bwrap aplicando as restrições do ExecutionMode.

    Se bwrap não estiver disponível, retorna cmd inalterado.
    """
    if not is_bwrap_available():
        return cmd

    bwrap: list[str] = ["bwrap"]

    for path in _COMMON_RO_PATHS:
        if os.path.exists(path):
            bwrap += ["--ro-bind", path, path]

    for path in _AGENT_RW_PATHS:
        if os.path.exists(path):
            bwrap += ["--bind", path, path]

    bwrap += ["--dev", "/dev"]
    bwrap += ["--proc", "/proc"]
    bwrap += ["--bind", "/tmp", "/tmp"]

    # /run é necessário para DNS: /etc/resolv.conf geralmente é symlink para
    # /run/systemd/resolve/stub-resolv.conf (systemd-resolved)
    if os.path.exists("/run"):
        bwrap += ["--ro-bind", "/run", "/run"]

    if mode.read_only_fs:
        bwrap += ["--ro-bind", working_dir, working_dir]
    else:
        bwrap += ["--bind", working_dir, working_dir]

    bwrap += ["--chdir", working_dir]

    if not mode.allow_network:
        bwrap += ["--unshare-net"]

    return bwrap + ["--"] + cmd

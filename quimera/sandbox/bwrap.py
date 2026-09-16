"""Componentes de `quimera.sandbox.bwrap`."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class _ExecutionProfileProto(Protocol):
    """Interface mínima de profile esperada pelo sandbox."""

    runtime_rw_paths: list


@runtime_checkable
class _ExecutionModeProto(Protocol):
    """Interface mínima esperada pelo sandbox; desacopla bwrap de quimera.modes."""

    read_only_fs: bool
    allow_network: bool

_HOME_DIR = str(Path.home())
_COMMON_RO_PATHS = ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt", _HOME_DIR]
def _resolve_hidden_files(hidden_paths: list[str] | tuple[str, ...]) -> list[str]:
    """Normaliza somente arquivos existentes que precisam ser mascarados."""
    return [
        str(Path(path).expanduser().resolve())
        for path in hidden_paths
        if os.path.isfile(path)
    ]


def _append_hidden_file_masks(args: list[str], hidden_paths: list[str]) -> None:
    """Sobrepõe arquivos privados com ``/dev/null`` sem estado temporário.

    O bind é gravável de propósito: usar ``--ro-bind /dev/null`` pode tornar o
    próprio device read-only em alguns kernels/user namespaces. Como o destino
    passa a ser o device ``/dev/null``, qualquer escrita é descartada e nunca
    alcança o arquivo real do host.
    """
    for path in hidden_paths:
        args += ["--dev-bind", "/dev/null", path]


def build_secret_mask_cmd(
        working_dir: str,
        cmd: list[str],
        hidden_paths: list[str] | tuple[str, ...],
        *,
        die_with_parent: bool = True,
) -> list[str]:
    """Mascara arquivos privados mantendo o restante do filesystem acessível."""
    paths = _resolve_hidden_files(hidden_paths)
    if not paths or not is_bwrap_available():
        return list(cmd)

    bwrap: list[str] = ["bwrap"]
    if die_with_parent:
        bwrap.append("--die-with-parent")
    bwrap += [
        "--unshare-pid",
        "--bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
    ]
    _append_hidden_file_masks(bwrap, paths)
    bwrap += ["--chdir", str(Path(working_dir).resolve()), "--"]
    return bwrap + list(cmd)


def is_bwrap_available() -> bool:
    """Retorna True se bubblewrap (bwrap) estiver instalado no sistema."""
    return shutil.which("bwrap") is not None


def build_bwrap_cmd(
        mode: _ExecutionModeProto,
        working_dir: str,
        cmd: list[str],
        profile: _ExecutionProfileProto | None = None,
        hidden_paths: list[str] | tuple[str, ...] = (),
        *,
        die_with_parent: bool = True,
) -> list[str]:
    """Envolve cmd com bwrap aplicando as restrições do ExecutionMode.

    Se bwrap não estiver disponível, retorna cmd inalterado.
    """
    if not is_bwrap_available():
        return cmd

    bwrap: list[str] = ["bwrap"]
    if die_with_parent:
        bwrap.append("--die-with-parent")
    bwrap.append("--unshare-pid")

    for path in _COMMON_RO_PATHS:
        if os.path.exists(path):
            bwrap += ["--ro-bind", path, path]

    resolved_hidden_paths = _resolve_hidden_files(hidden_paths)

    for path in getattr(profile, "runtime_rw_paths", []):
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

    # Aplica as máscaras por último para que binds RW de profile/workspace não
    # consigam reexpor arquivos privados dentro de diretórios já montados.
    if resolved_hidden_paths:
        _append_hidden_file_masks(bwrap, resolved_hidden_paths)

    bwrap += ["--chdir", working_dir]

    if not mode.allow_network:
        bwrap += ["--unshare-net"]

    return bwrap + ["--"] + cmd

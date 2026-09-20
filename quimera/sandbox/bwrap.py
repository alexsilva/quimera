"""Componentes de `quimera.sandbox.bwrap`."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable


class SandboxError(RuntimeError):
    """Erro base que impede uma execução confinada."""


class SandboxUnavailableError(SandboxError):
    """Sandbox obrigatório indisponível; execução deve falhar fechada."""


class SandboxPathError(SandboxError):
    """Diretório de execução incompatível com o workspace confinado."""


_SANDBOX_UNAVAILABLE_MSG = (
    "sandbox do workspace está ativo, mas o bubblewrap (bwrap) não está "
    "disponível neste sistema. Instale o pacote 'bubblewrap' ou desative com "
    "/sandbox off."
)


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
    bwrap_executable = _find_bwrap_executable()
    if not paths or bwrap_executable is None:
        return list(cmd)

    bwrap: list[str] = [bwrap_executable]
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
    return _find_bwrap_executable() is not None


def _find_bwrap_executable() -> str | None:
    """Resolve o bwrap para um path absoluto fora do PATH do subprocesso.

    As tools podem ajustar ``PATH`` para priorizar o virtualenv gravável do
    workspace. Retornar o nome literal ``bwrap`` permitiria que um executável
    plantado nesse virtualenv substituísse o mecanismo de confinamento.
    """
    executable = shutil.which("bwrap")
    if executable is None:
        return None
    return str(Path(executable).resolve())


def bwrap_self_test(timeout_seconds: float = 5.0) -> bool:
    """Retorna True quando o bwrap está instalado e consegue criar namespaces.

    Em kernels que bloqueiam user namespaces (ex.: Android), o binário pode
    existir mas falhar em runtime; por isso o teste executa um comando real.
    """
    bwrap_executable = _find_bwrap_executable()
    if bwrap_executable is None:
        return False
    try:
        result = subprocess.run(
            [
                bwrap_executable, "--die-with-parent", "--unshare-pid",
                "--ro-bind", "/", "/",
                "--proc", "/proc", "--dev", "/dev",
                "--", "true",
            ],
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def build_workspace_sandbox_cmd(
        workspace_root: str,
        working_dir: str,
        cmd: list[str],
        hidden_paths: list[str] | tuple[str, ...] = (),
        *,
        rw_paths: list[str] | tuple[str, ...] = (),
        die_with_parent: bool = True,
        read_only_workspace: bool = False,
        allow_network: bool = True,
) -> list[str]:
    """Confina cmd ao workspace: escrita apenas no workspace, /tmp e rw_paths.

    Diferente dos demais builders, este é fail-closed: levanta
    ``SandboxUnavailableError`` quando o bwrap não está instalado, em vez de
    devolver o comando sem isolamento.
    """
    bwrap_executable = _find_bwrap_executable()
    if bwrap_executable is None:
        raise SandboxUnavailableError(_SANDBOX_UNAVAILABLE_MSG)

    root_path = Path(workspace_root).resolve()
    chdir_path = Path(working_dir).resolve()
    if chdir_path != root_path and root_path not in chdir_path.parents:
        raise SandboxPathError(
            f"diretório de execução fora do workspace: {chdir_path}"
        )
    root = str(root_path)
    chdir = str(chdir_path)

    bwrap: list[str] = [bwrap_executable]
    if die_with_parent:
        bwrap.append("--die-with-parent")
    bwrap.append("--unshare-pid")

    for path in _COMMON_RO_PATHS:
        if os.path.exists(path):
            bwrap += ["--ro-bind", path, path]

    # Exceções de escrita: diretórios de runtime dos agentes (ex.: ~/.codex,
    # ~/.claude) montados por cima do $HOME somente leitura.
    for path in rw_paths:
        if os.path.exists(path):
            bwrap += ["--bind", path, path]

    bwrap += ["--dev", "/dev"]
    bwrap += ["--proc", "/proc"]
    bwrap += ["--bind", "/tmp", "/tmp"]

    # /run é necessário para DNS (resolv.conf costuma apontar para /run).
    if os.path.exists("/run"):
        bwrap += ["--ro-bind", "/run", "/run"]

    workspace_bind = "--ro-bind" if read_only_workspace else "--bind"
    bwrap += [workspace_bind, root, root]

    # Máscaras por último para que binds RW não reexponham arquivos privados.
    resolved_hidden_paths = _resolve_hidden_files(hidden_paths)
    if resolved_hidden_paths:
        _append_hidden_file_masks(bwrap, resolved_hidden_paths)

    bwrap += ["--chdir", chdir]
    if not allow_network:
        bwrap.append("--unshare-net")
    return bwrap + ["--"] + list(cmd)


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
    bwrap_executable = _find_bwrap_executable()
    if bwrap_executable is None:
        return cmd

    bwrap: list[str] = [bwrap_executable]
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

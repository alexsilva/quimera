"""Estado do sandbox por workspace e wrapper único de subprocessos.

O flag ``sandbox_enabled`` é persistido no arquivo de configuração do
workspace e lido no momento de cada spawn. Isso garante que o
toggle via ``/sandbox on|off`` (ou modal de configuração) valha imediatamente
para todos os pontos de execução — tools de shell/git, MCP clients stdio e
CLIs dos agentes — inclusive em executores de background, sem plumbing extra.
"""
from __future__ import annotations

from .bwrap import (
    SandboxError,
    SandboxUnavailableError,  # noqa: F401 (reexport para call sites)
    build_secret_mask_cmd,
    build_workspace_sandbox_cmd,
)


def is_sandbox_enabled(workspace) -> bool:
    """Retorna o flag persistido no config do workspace; False sem workspace."""
    if workspace is None:
        return False
    config_file = getattr(workspace, "workspace_config_file", None)
    if config_file is None:
        config_file = getattr(workspace, "mcp_config_file", None)
    if config_file is None:
        return False
    from quimera.config_store import read_json_object

    try:
        data = read_json_object(config_file, strict=True)
    except ValueError as exc:
        raise SandboxError(
            "configuração do workspace inválida; execução bloqueada por segurança"
        ) from exc
    return data.get("sandbox_enabled") is True


def agent_runtime_rw_paths() -> list[str]:
    """União dos ``runtime_rw_paths`` de todos os profiles registrados."""
    from quimera import profiles

    paths: list[str] = []
    for profile in profiles.all_profiles():
        for path in getattr(profile, "runtime_rw_paths", []) or []:
            text = str(path)
            if text and text not in paths:
                paths.append(text)
    return paths


def wrap_subprocess_cmd(
        workspace,
        working_dir: str,
        cmd: list[str],
        *,
        rw_paths: list[str] | tuple[str, ...] | None = None,
        die_with_parent: bool = True,
) -> list[str]:
    """Envolve cmd conforme o estado do sandbox do workspace.

    - sandbox OFF: mantém o comportamento atual (mascaramento de segredos,
      filesystem inteiro visível conforme permissões do usuário).
    - sandbox ON: confina ao workspace + /tmp + diretórios de runtime dos
      agentes, falhando fechado (``SandboxUnavailableError``) sem bwrap.
    """
    if workspace is None:
        return list(cmd)
    hidden_paths = [str(path) for path in getattr(workspace, "protected_files", ())]
    if not is_sandbox_enabled(workspace):
        return build_secret_mask_cmd(
            working_dir,
            list(cmd),
            hidden_paths,
            die_with_parent=die_with_parent,
        )
    effective_rw = list(rw_paths) if rw_paths is not None else agent_runtime_rw_paths()
    return build_workspace_sandbox_cmd(
        str(workspace.cwd),
        working_dir,
        list(cmd),
        hidden_paths,
        rw_paths=effective_rw,
        die_with_parent=die_with_parent,
    )

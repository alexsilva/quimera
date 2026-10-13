"""Metadados canônicos das tools nativas do runtime.

Este módulo concentra decisões operacionais que antes ficavam duplicadas entre
policy, approval broker e perfis MCP. O schema público continua em
``drivers/tool_catalog.py``; aqui ficam risco, mutação, paths, serialização,
capabilities e perfis de exposição.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ToolRisk(str, Enum):
    """Classificação de risco usada pela governança de tools."""

    READ = "read"
    NETWORK = "network"
    DELEGATION = "delegation"
    WRITE = "write"
    SHELL = "shell"
    DESTRUCTIVE = "destructive"


class ApprovalMode(str, Enum):
    """Regra de approval aplicada pela policy à tool."""

    NONE = "none"
    MUTATION = "mutation"
    TASK_CREATION = "task_creation"
    HTTP_METHOD = "http_method"


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """Metadados operacionais de uma tool nativa."""

    risk: ToolRisk
    mutates: bool = False
    approval: ApprovalMode = ApprovalMode.NONE
    path_args: tuple[str, ...] = ()
    requires_path_permission: bool = False
    serialization: str | None = None
    parallel_safe: bool = False
    capabilities: tuple[str, ...] = ()
    http_profiles: frozenset[str] = frozenset()


_READ_LOCAL = frozenset({"read-local", "read", "agent"})
_READ_NETWORK = frozenset({"read", "agent"})
_AGENT = frozenset({"agent"})


def _meta(
    risk: ToolRisk,
    *,
    mutates: bool = False,
    approval: ApprovalMode = ApprovalMode.NONE,
    path_args: tuple[str, ...] = (),
    requires_path_permission: bool = False,
    serialization: str | None = None,
    parallel_safe: bool = False,
    capabilities: tuple[str, ...] = (),
    http_profiles: frozenset[str] = frozenset(),
) -> ToolMetadata:
    return ToolMetadata(
        risk=risk,
        mutates=mutates,
        approval=approval,
        path_args=path_args,
        requires_path_permission=requires_path_permission,
        serialization=serialization,
        parallel_safe=parallel_safe,
        capabilities=capabilities,
        http_profiles=http_profiles,
    )


TOOL_METADATA: dict[str, ToolMetadata] = {
    # Host diagnostics.
    "host_processes": _meta(
        ToolRisk.READ, parallel_safe=True, http_profiles=_READ_LOCAL,
    ),
    "host_process_inspect": _meta(
        ToolRisk.READ, parallel_safe=True, http_profiles=_READ_LOCAL,
    ),
    "host_process_sample": _meta(
        ToolRisk.READ, parallel_safe=True, http_profiles=_READ_LOCAL,
    ),
    "host_memory": _meta(
        ToolRisk.READ, parallel_safe=True, http_profiles=_READ_LOCAL,
    ),

    # Diagnóstico da TUI (só com --debug e interface Textual ativa). O SVG vai
    # para os logs da sessão, nunca para o workspace: não é mutação.
    "tui_screenshot": _meta(
        ToolRisk.READ,
        parallel_safe=True,
        capabilities=("tui_screenshot",),
        http_profiles=_READ_LOCAL,
    ),

    # Descoberta de tools diferidas.
    "tool_search": _meta(
        ToolRisk.READ, parallel_safe=True, http_profiles=_READ_LOCAL,
    ),

    # Files/workspace.
    "list_files": _meta(
        ToolRisk.READ,
        path_args=("path",),
        requires_path_permission=True,
        parallel_safe=True,
        http_profiles=_READ_LOCAL,
    ),
    "read_file": _meta(
        ToolRisk.READ,
        path_args=("path",),
        requires_path_permission=True,
        parallel_safe=True,
        http_profiles=_READ_LOCAL,
    ),
    "write_file": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        path_args=("path",),
        serialization="path",
    ),
    "replace_text": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        path_args=("path",),
        serialization="path",
        http_profiles=_AGENT,
    ),
    "apply_patch": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        serialization="patch_paths",
    ),
    "grep_search": _meta(
        ToolRisk.READ,
        path_args=("path",),
        requires_path_permission=True,
        parallel_safe=True,
        http_profiles=_READ_LOCAL,
    ),
    "inspect_symbols": _meta(
        ToolRisk.READ,
        path_args=("path",),
        parallel_safe=True,
        http_profiles=_READ_LOCAL,
    ),
    "remove_file": _meta(
        ToolRisk.DESTRUCTIVE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        path_args=("path",),
        requires_path_permission=True,
        serialization="path",
    ),

    # Shell/process sessions.
    "run_shell": _meta(
        ToolRisk.SHELL,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        serialization="workspace",
    ),
    "exec_command": _meta(
        ToolRisk.SHELL,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        serialization="workspace",
    ),
    "close_command_session": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        serialization="command_session",
    ),
    "write_stdin": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        serialization="command_session",
    ),
    "poll_command_session": _meta(
        ToolRisk.SHELL,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        serialization="command_session",
    ),

    # Tasks/todo.
    "tasks_create": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.TASK_CREATION,
        capabilities=("task_db", "tasks"),
        http_profiles=_AGENT,
    ),
    "tasks_delegate": _meta(
        ToolRisk.DELEGATION,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        capabilities=("delegate",),
        http_profiles=_AGENT,
    ),
    "tasks_list": _meta(
        ToolRisk.READ,
        parallel_safe=True,
        capabilities=("task_db",),
        http_profiles=_READ_LOCAL,
    ),
    # Cancelamento não passa por approval: a gate real é a checagem de dono
    # (só quem delegou, ou o humano) feita pela própria tool.
    "tasks_cancel": _meta(
        ToolRisk.WRITE,
        mutates=True,
        capabilities=("task_db", "delegate"),
        http_profiles=_AGENT,
    ),
    # Workflows: publicar/consultar/pausar/cancelar/concluir não colocam trabalho novo
    # em movimento (a gate é a checagem de dono feita pela própria tool). Iniciar e
    # replanejar disparam execução com auto-aprovação de tasks, então exigem a mesma
    # aprovação de tasks_create: delegar não amplia permissões.
    "workflows_create": _meta(
        ToolRisk.WRITE,
        mutates=True,
        capabilities=("task_db", "workflows"),
        http_profiles=_AGENT,
    ),
    "workflows_start": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.TASK_CREATION,
        capabilities=("task_db", "workflows"),
        http_profiles=_AGENT,
    ),
    "workflows_status": _meta(
        ToolRisk.READ,
        parallel_safe=True,
        capabilities=("task_db", "workflows"),
        http_profiles=_READ_LOCAL,
    ),
    "workflows_control": _meta(
        ToolRisk.WRITE,
        mutates=True,
        capabilities=("task_db", "workflows"),
        http_profiles=_AGENT,
    ),
    "workflows_replan": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.TASK_CREATION,
        capabilities=("task_db", "workflows"),
        http_profiles=_AGENT,
    ),
    "workflows_cancel": _meta(
        ToolRisk.WRITE,
        mutates=True,
        capabilities=("task_db", "workflows"),
        http_profiles=_AGENT,
    ),
    "workflows_finish": _meta(
        ToolRisk.WRITE,
        mutates=True,
        capabilities=("task_db", "workflows"),
        http_profiles=_AGENT,
    ),
    "jobs_list": _meta(
        ToolRisk.READ,
        parallel_safe=True,
        capabilities=("task_db",),
        http_profiles=_READ_LOCAL,
    ),
    "jobs_get": _meta(
        ToolRisk.READ,
        parallel_safe=True,
        capabilities=("task_db",),
        http_profiles=_READ_LOCAL,
    ),
    "todo_write": _meta(ToolRisk.WRITE, mutates=True),
    "todo_list": _meta(
        ToolRisk.READ, parallel_safe=True, http_profiles=_READ_LOCAL,
    ),

    # Network/web.
    "web_search": _meta(
        ToolRisk.NETWORK, parallel_safe=True, http_profiles=_READ_NETWORK,
    ),
    "web_fetch": _meta(
        ToolRisk.NETWORK, parallel_safe=True, http_profiles=_READ_NETWORK,
    ),
    # HTTP risk/mutation are method-dependent; NETWORK is the safe GET baseline.
    "http_request": _meta(
        ToolRisk.NETWORK,
        mutates=True,
        approval=ApprovalMode.HTTP_METHOD,
        http_profiles=_AGENT,
    ),

    # Memory. memory_save intentionally preserves the current auto-approved
    # workspace-memory behavior; deletion remains destructive.
    "memory_save": _meta(ToolRisk.READ, mutates=True, http_profiles=_AGENT),
    # retrieve poda TTLs expirados e pode reescrever memory.json; portanto é
    # leitura na API, mas não uma operação sem mutação para o scheduler.
    "memory_retrieve": _meta(ToolRisk.READ, http_profiles=_READ_LOCAL),
    "memory_delete": _meta(
        ToolRisk.DESTRUCTIVE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        http_profiles=_AGENT,
    ),
    "memory_list_namespaces": _meta(
        ToolRisk.READ, parallel_safe=True, http_profiles=_READ_LOCAL,
    ),

    # Agents/interactions/state.
    "list_agents": _meta(
        ToolRisk.READ,
        parallel_safe=True,
        capabilities=("delegate",),
        http_profiles=_AGENT,
    ),
    "ask_user": _meta(ToolRisk.READ, capabilities=("ask_user",)),
    "update_shared_state": _meta(
        ToolRisk.READ,
        mutates=True,
        capabilities=("update_shared_state",),
    ),

    # Git.
    "git_status": _meta(
        ToolRisk.READ, parallel_safe=True, http_profiles=_READ_LOCAL,
    ),
    "git_log": _meta(
        ToolRisk.READ, parallel_safe=True, http_profiles=_READ_LOCAL,
    ),
    "git_diff": _meta(
        ToolRisk.READ, parallel_safe=True, http_profiles=_READ_LOCAL,
    ),
    "git_branch": _meta(
        ToolRisk.READ, parallel_safe=True, http_profiles=_READ_LOCAL,
    ),
    "git_fetch": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        http_profiles=_AGENT,
    ),
    "git_add": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        http_profiles=_AGENT,
    ),
    "git_commit": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        http_profiles=_AGENT,
    ),
    "git_checkout": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        http_profiles=_AGENT,
    ),
    "git_push": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
        http_profiles=_AGENT,
    ),

    # Browser. Observational operations are NETWORK; operations that change
    # browser/page/session state are WRITE.
    "browser_start": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
    ),
    "browser_status": _meta(ToolRisk.NETWORK),
    "browser_close": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
    ),
    "browser_navigate": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
    ),
    "browser_snapshot": _meta(ToolRisk.NETWORK),
    "browser_click": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
    ),
    "browser_type": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
    ),
    "browser_press": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
    ),
    "browser_mouse": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
    ),
    "browser_wait": _meta(ToolRisk.NETWORK),
    "browser_evaluate": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
    ),
    "browser_screenshot": _meta(
        ToolRisk.WRITE,
        mutates=True,
        approval=ApprovalMode.MUTATION,
    ),
    "browser_console": _meta(ToolRisk.NETWORK),
    "browser_network": _meta(ToolRisk.NETWORK),
}


def get_tool_metadata(tool_name: str) -> ToolMetadata | None:
    """Retorna metadados nativos, ou ``None`` para tool externa/desconhecida."""
    return TOOL_METADATA.get(tool_name)


def tools_for_http_profile(profile: str) -> frozenset[str]:
    """Deriva a allowlist HTTP diretamente dos metadados canônicos."""
    return frozenset(
        name
        for name, metadata in TOOL_METADATA.items()
        if profile in metadata.http_profiles
    )


def mutating_tool_names() -> frozenset[str]:
    """Retorna todas as tools nativas que podem alterar estado observável."""
    return frozenset(
        name for name, metadata in TOOL_METADATA.items() if metadata.mutates
    )

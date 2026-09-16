"""Componentes de `quimera.runtime.config`."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quimera.session_paths import SessionPaths
    from quimera.workspace import Workspace
    from .workspace_policy import WorkspacePolicy


DEFAULT_MCP_TOOL_TIMEOUT_SECONDS = 600
# Delegações rodam trabalho longo (revisões, análises) e têm orçamento próprio,
# muito acima do teto das tools comuns.
DEFAULT_DELEGATE_TIMEOUT_SECONDS = 3600
# O cliente MCP (ex.: Codex) deve expirar sempre DEPOIS do servidor, para que
# o erro estruturado venha do servidor e nunca do cliente abandonando a chamada.
DEFAULT_MCP_CLIENT_TOOL_TIMEOUT_SECONDS = 2 * DEFAULT_DELEGATE_TIMEOUT_SECONDS


@dataclass(slots=True)
class ToolRuntimeConfig:
    """Configuração do runtime associada ao único ``Workspace`` da aplicação."""
    workspace: Workspace
    session_paths: SessionPaths | None = None
    command_timeout_seconds: int = 20
    command_max_timeout_seconds: int = 300
    mcp_tool_timeout_seconds: int = DEFAULT_MCP_TOOL_TIMEOUT_SECONDS
    delegate_parallel_timeout_seconds: int = DEFAULT_DELEGATE_TIMEOUT_SECONDS
    interactive_command_default_yield_ms: int = 1000
    max_output_chars: int = 1_000_000
    max_file_read_chars: int = 20_000
    max_search_results: int = 100
    max_task_results: int = 500
    require_approval_for_mutations: bool = True
    require_approval_for_task_creation: bool = True
    allow_ask_user: bool = True
    delegation_budget_per_run: int = 8
    workspace_policy: WorkspacePolicy | None = None
    allowed_read_roots: list[Path] = field(default_factory=list)
    shell_allowlist: set[str] = field(
        default_factory=lambda: {
            "awk",
            "cargo",
            "cat",
            "cmake",
            "cmp",
            "composer",
            "cp",
            "cut",
            "diff",
            "docker",
            "docker-compose",
            "dotnet",
            "echo",
            "file",
            "find",
            "go",
            "gradle",
            "gradlew",
            "git",
            "grep",
            "head",
            "java",
            "javac",
            "jq",
            "ls",
            "make",
            "mkdir",
            "mvn",
            "mvnw",
            "node",
            "npm",
            "npx",
            "pnpm",
            "poetry",
            "pip",
            "pip3",
            "pwd",
            "pytest",
            "python",
            "python3",
            "rg",
            "ruff",
            "sed",
            "sort",
            "stat",
            "tail",
            "tee",
            "tree",
            "tsc",
            "uv",
            "wc",
            "xargs",
            "yarn",
        }
    )
    shell_denylist_patterns: tuple[str, ...] = (
        "rm -rf",
        "rm -r /",
        "rm -rf /",
        "sudo ",
        "systemctl ",
        "shutdown",
        "reboot",
        "poweroff",
        "mkfs",
        " dd ",
        ":(){",
        ":()",
        "chmod -R 777",
        "chown -R",
        "chattr",
        "dd if=",
        "wget ",
        "curl -o",
        "curl --output",
    )

    def __post_init__(self) -> None:
        """Executa post init."""
        if self.workspace is None:
            raise TypeError("ToolRuntimeConfig exige Workspace")
        self.allowed_read_roots = [Path(path).resolve() for path in self.allowed_read_roots]

    def read_roots(self) -> tuple[Path, ...]:
        """Roots de leitura efetivos, sempre derivados do Workspace atual."""
        roots = [self.workspace.cwd, *self.allowed_read_roots]
        artifacts_root = self.session_paths.artifacts_dir if self.session_paths is not None else None
        if artifacts_root is not None and not any(artifacts_root.is_relative_to(root) for root in roots):
            roots.append(artifacts_root)
        return tuple(dict.fromkeys(path.resolve() for path in roots))

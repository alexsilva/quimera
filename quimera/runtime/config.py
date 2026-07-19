"""Componentes de `quimera.runtime.config`."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace_policy import WorkspacePolicy


@dataclass(slots=True)
class ToolRuntimeConfig:
    """Implementa `ToolRuntimeConfig`."""
    workspace_root: Path
    db_path: Path | None = None
    memory_file: Path | None = None
    artifacts_root: Path | None = None
    command_timeout_seconds: int = 20
    mcp_tool_timeout_seconds: int = 600
    delegate_parallel_timeout_seconds: int = 600
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
        if not isinstance(self.workspace_root, Path):
            raise TypeError(f"workspace_root deve ser Path, não {type(self.workspace_root).__name__}")
        self.workspace_root = self.workspace_root.resolve()
        if self.db_path is not None:
            self.db_path = Path(self.db_path).resolve()
        if self.memory_file is not None:
            self.memory_file = Path(self.memory_file).resolve()
        if self.artifacts_root is not None:
            self.artifacts_root = Path(self.artifacts_root).resolve()
        else:
            self.artifacts_root = self.workspace_root / "artifacts"
        if not self.allowed_read_roots:
            self.allowed_read_roots = [self.workspace_root]
        else:
            self.allowed_read_roots = [p.resolve() for p in self.allowed_read_roots]
        already_covered = any(
            self.artifacts_root.is_relative_to(root) for root in self.allowed_read_roots
        )
        if not already_covered:
            self.allowed_read_roots.append(self.artifacts_root)

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ToolRuntimeConfig:
    workspace_root: Path
    db_path: Path | None = None
    command_timeout_seconds: int = 20
    max_output_chars: int = 12_000
    max_file_read_chars: int = 20_000
    max_search_results: int = 100
    require_approval_for_mutations: bool = True
    allowed_read_roots: list[Path] = field(default_factory=list)
    shell_allowlist: set[str] = field(
        default_factory=lambda: {
            "cat",
            "echo",
            "find",
            "git",
            "grep",
            "head",
            "ls",
            "pwd",
            "pytest",
            "python",
            "sed",
            "tail",
        }
    )
    shell_denylist_patterns: tuple[str, ...] = (
        "rm -rf",
        "sudo ",
        "systemctl ",
        "shutdown",
        "reboot",
        "poweroff",
        "mkfs",
        " dd ",
        ":(){",
        "chmod -R 777 /",
        "chown -R /",
    )

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.resolve()
        if not self.allowed_read_roots:
            self.allowed_read_roots = [self.workspace_root]
        else:
            self.allowed_read_roots = [p.resolve() for p in self.allowed_read_roots]

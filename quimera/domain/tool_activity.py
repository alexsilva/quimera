"""Classificação semântica de atividades executadas por ferramentas."""
from __future__ import annotations

import shlex
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping


class ToolActivityCategory(str, Enum):
    INSPECTION = "inspection"
    MODIFICATION = "modification"
    VALIDATION = "validation"
    VERSION_CONTROL = "version_control"
    RESEARCH = "research"
    EXECUTION = "execution"


INSPECTION_TOOLS = frozenset({
    "read_file", "list_files", "grep_search", "git_status", "git_diff",
    "git_log", "git_branch", "browser_status", "browser_network",
})
MODIFICATION_TOOLS = frozenset({"apply_patch", "write_file", "remove_file"})
VERSION_CONTROL_TOOLS = frozenset({
    "git_add", "git_commit", "git_checkout", "git_push", "git_fetch",
})
RESEARCH_TOOLS = frozenset({
    "web_search", "web_fetch", "browser_start", "browser_open",
    "browser_click", "browser_screenshot",
})
COMMAND_TOOLS = frozenset({"exec_command", "run_shell", "exec_session", "run_command", "shell"})
VALIDATION_EXECUTABLES = frozenset({
    "pytest", "ruff", "mypy", "pyright", "eslint", "tsc", "tox", "nox",
    "jest", "vitest", "unittest",
})
INSPECTION_EXECUTABLES = frozenset({
    "cat", "less", "head", "tail", "grep", "rg", "find", "fd", "ls",
    "pwd", "stat", "wc", "tree",
})
MODIFICATION_EXECUTABLES = frozenset({"rm", "mv", "cp", "mkdir", "touch", "install", "truncate"})
GIT_INSPECTION = frozenset({"status", "diff", "log", "show", "branch", "rev-parse", "ls-files"})
GIT_MUTATION = frozenset({
    "add", "commit", "checkout", "switch", "merge", "rebase", "cherry-pick",
    "push", "pull", "fetch", "reset", "restore", "stash", "tag",
})
PACKAGE_RUNNERS = frozenset({"npm", "pnpm", "yarn", "bun"})
VALIDATION_TARGETS = frozenset({"test", "lint", "check", "typecheck", "build"})


def normalize_tool_name(tool_name: object) -> str:
    name = str(tool_name or "").strip().lower().replace(".", "_")
    for prefix in ("mcp__quimera__", "quimera_", "mcp_quimera_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _tokens(command: object) -> list[str]:
    raw = str(command or "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw, posix=True)
    except ValueError:
        return raw.split()


def _basename(token: str) -> str:
    return Path(token).name.lower()


def classify_command_activity(command: object) -> ToolActivityCategory:
    tokens = _tokens(command)
    if not tokens:
        return ToolActivityCategory.EXECUTION

    executable = _basename(tokens[0])
    args = [token.lower() for token in tokens[1:]]

    if executable in VALIDATION_EXECUTABLES:
        return ToolActivityCategory.VALIDATION
    if executable in {"python", "python3", "python3.12"} and len(args) >= 2 and args[0] == "-m":
        if _basename(args[1]) in VALIDATION_EXECUTABLES or args[1] == "compileall":
            return ToolActivityCategory.VALIDATION
    if executable in PACKAGE_RUNNERS:
        target_index = 1 if args[:1] == ["run"] else 0
        if len(args) > target_index and args[target_index] in VALIDATION_TARGETS:
            return ToolActivityCategory.VALIDATION
    if executable == "cargo" and args[:1] and args[0] in {"test", "check", "clippy", "build"}:
        return ToolActivityCategory.VALIDATION
    if executable == "go" and args[:1] and args[0] in {"test", "vet", "build"}:
        return ToolActivityCategory.VALIDATION
    if executable == "make" and args[:1] and args[0] in VALIDATION_TARGETS:
        return ToolActivityCategory.VALIDATION

    if executable == "git" and args:
        if args[0] in GIT_MUTATION:
            return ToolActivityCategory.VERSION_CONTROL
        if args[0] in GIT_INSPECTION:
            return ToolActivityCategory.INSPECTION

    if executable in MODIFICATION_EXECUTABLES:
        return ToolActivityCategory.MODIFICATION
    if executable in INSPECTION_EXECUTABLES:
        return ToolActivityCategory.INSPECTION
    return ToolActivityCategory.EXECUTION


def coerce_tool_activity(value: object) -> ToolActivityCategory | None:
    if isinstance(value, ToolActivityCategory):
        return value
    normalized = str(value or "").strip().lower()
    return next((category for category in ToolActivityCategory if category.value == normalized), None)


def classify_tool_activity(
    tool_name: object,
    input_payload: Mapping[str, object] | None = None,
    *,
    explicit: object = None,
) -> ToolActivityCategory:
    explicit_category = coerce_tool_activity(explicit)
    if explicit_category is not None:
        return explicit_category

    normalized = normalize_tool_name(tool_name)
    if normalized in INSPECTION_TOOLS:
        return ToolActivityCategory.INSPECTION
    if normalized in MODIFICATION_TOOLS:
        return ToolActivityCategory.MODIFICATION
    if normalized in VERSION_CONTROL_TOOLS:
        return ToolActivityCategory.VERSION_CONTROL
    if normalized in RESEARCH_TOOLS:
        return ToolActivityCategory.RESEARCH
    if normalized in COMMAND_TOOLS:
        command = input_payload.get("cmd") if isinstance(input_payload, Mapping) else None
        return classify_command_activity(command)
    return ToolActivityCategory.EXECUTION


def count_tool_activities(tools: Iterable[object]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for raw_tool in tools:
        if not isinstance(raw_tool, Mapping):
            continue
        category = classify_tool_activity(
            raw_tool.get("tool"),
            raw_tool.get("input") if isinstance(raw_tool.get("input"), Mapping) else None,
            explicit=raw_tool.get("activity"),
        )
        counts[category.value] += 1
    return dict(counts)

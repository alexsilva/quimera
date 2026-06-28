"""Componentes de `quimera.profiles.antigravity`."""
from quimera.profiles.base import ExecutionProfile, register


class AntigravityProfile(ExecutionProfile):
    """Profile do Antigravity CLI (agy)."""


profile = AntigravityProfile(
    name="antigravity",
    prefix="/antigravity",
    icon="🪐",
    cmd=["agy", "--dangerously-skip-permissions", "-p"],
    style=("cyan", "Antigravity"),
    prompt_as_arg=True,
    keep_stdin_open=False,
    output_format=None,
    capabilities=["code_review", "documentation", "general_reasoning", "code_editing", "complex_refactoring"],
    preferred_task_types=["code_review", "documentation", "general", "code_edit", "architecture", "bug_investigation"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    tool_use_reliability="high",
    supports_code_editing=True,
    supports_long_context=True,
    base_tier=3,
)
register(profile)

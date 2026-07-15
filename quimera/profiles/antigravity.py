"""Componentes de `quimera.profiles.antigravity`."""
from quimera.profiles.base import CliConnection, ExecutionProfile, register


class AntigravityProfile(ExecutionProfile):
    """Profile do Antigravity CLI (agy)."""

    def configure_with_model(self, model_id: str) -> CliConnection:
        normalized = (model_id or "").strip()
        if not normalized:
            raise ValueError("model_id não pode ser vazio.")
        connection = self.effective_connection()
        if not isinstance(connection, CliConnection):
            raise ValueError(f"Profile '{self.name}' não usa driver CLI.")

        cmd = list(connection.cmd)
        for idx, arg in enumerate(cmd):
            if arg.startswith("--model="):
                cmd[idx] = f"--model={normalized}"
                return CliConnection(cmd=cmd, prompt_as_arg=connection.prompt_as_arg, output_format=connection.output_format)
            if arg == "--model" and idx + 1 < len(cmd):
                cmd[idx + 1] = normalized
                return CliConnection(cmd=cmd, prompt_as_arg=connection.prompt_as_arg, output_format=connection.output_format)

        if cmd:
            cmd = [cmd[0], "--model", normalized, *cmd[1:]]
        else:
            cmd = ["agy", "--model", normalized]
        return CliConnection(cmd=cmd, prompt_as_arg=connection.prompt_as_arg, output_format=connection.output_format)


profile = AntigravityProfile(
    name="antigravity",
    prefix="/antigravity",
    icon="🪐",
    cmd=["agy", "--dangerously-skip-permissions", "-p"],
    style=("cyan", "Antigravity"),
    prompt_as_arg=True,
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

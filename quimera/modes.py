"""Componentes de `quimera.modes`."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExecutionMode:
    """Define restrições de execução ativas durante uma rodada."""

    name: str
    read_only_fs: bool = False
    allow_network: bool = True
    blocked_tools: list[str] = field(default_factory=list)
    prompt_addon: str = ""


MODES: dict[str, ExecutionMode] = {
    "/planning": ExecutionMode(
        name="planning",
        read_only_fs=True,
        allow_network=True,
        blocked_tools=[
            "write_file", "apply_patch",
        ],
        prompt_addon=(
            "[MODO: PLANEJAMENTO] Planejamento com workspace somente leitura. "
            "Não edite arquivos."
        ),
    ),
    "/analysis": ExecutionMode(
        name="analysis",
        read_only_fs=True,
        allow_network=True,
        blocked_tools=["write_file", "apply_patch"],
        prompt_addon=(
            "[MODO: ANÁLISE] Apenas leitura e análise. Não edite arquivos."
        ),
    ),
    "/design": ExecutionMode(
        name="design",
        read_only_fs=True,
        allow_network=True,
        blocked_tools=[
            "write_file", "apply_patch",
            "run_shell", "exec_command", "write_stdin", "close_command_session",
        ],
        prompt_addon=(
            "[MODO: DESIGN] Apenas design e arquitetura. Não execute código."
        ),
    ),
    "/review": ExecutionMode(
        name="review",
        read_only_fs=True,
        allow_network=True,
        blocked_tools=[
            "write_file", "apply_patch",
            "run_shell", "exec_command", "write_stdin", "close_command_session",
        ],
        prompt_addon=(
            "[MODO: REVISÃO] Apenas revisão de código. Não edite arquivos."
        ),
    ),
    "/execute": ExecutionMode(
        name="execute",
        read_only_fs=False,
        allow_network=True,
        blocked_tools=[],
        prompt_addon="",
    ),
}


def get_mode(command: str) -> ExecutionMode | None:
    """Retorna o ExecutionMode para o comando /modo, ou None se não reconhecido."""
    return MODES.get(command.lower())

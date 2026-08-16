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
    allowed_tools: list[str] | None = None
    prompt_addon: str = ""


MODES: dict[str, ExecutionMode] = {
    "/planning": ExecutionMode(
        name="planning",
        read_only_fs=True,
        allow_network=True,
        blocked_tools=[
            "write_file",
            "apply_patch",
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
        prompt_addon=("[MODO: ANÁLISE] Apenas leitura e análise. Não edite arquivos."),
    ),
    "/design": ExecutionMode(
        name="design",
        read_only_fs=True,
        allow_network=True,
        blocked_tools=[
            "write_file",
            "apply_patch",
            "run_shell",
            "exec_command",
            "write_stdin",
            "close_command_session",
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
            "write_file",
            "apply_patch",
            "run_shell",
            "exec_command",
            "write_stdin",
            "close_command_session",
        ],
        prompt_addon=("[MODO: REVISÃO] Apenas revisão de código. Não edite arquivos."),
    ),
    "/execute": ExecutionMode(
        name="execute",
        read_only_fs=False,
        allow_network=True,
        blocked_tools=[],
        prompt_addon=(
            "[MODO: EXECUÇÃO] Ferramentas de escrita e execução estão liberadas. "
            "Isso amplia permissões, mas não muda a intenção do pedido do humano. "
            "Se o humano pedir análise, analise; só implemente ou execute mudanças "
            "quando isso for solicitado explicitamente."
        ),
    ),
}


DEBATE_MODE = ExecutionMode(
    name="debate",
    read_only_fs=True,
    allow_network=True,
    blocked_tools=[
        "write_file",
        "replace_text",
        "apply_patch",
        "remove_file",
        "run_shell",
        "run_shell_command",
        "exec_command",
        "poll_command_session",
        "write_stdin",
        "close_command_session",
        "delegate",
        "tasks",
        "ask_user",
        "update_shared_state",
        "todo_write",
        "memory_save",
        "git_fetch",
        "git_add",
        "git_commit",
        "git_checkout",
        "git_push",
        "browser_start",
        "browser_close",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_mouse",
        "browser_wait",
        "browser_evaluate",
        "browser_screenshot",
    ],
    allowed_tools=[
        "list_files",
        "read_file",
        "grep_search",
        "git_status",
        "git_diff",
        "git_log",
        "web_search",
        "web_fetch",
    ],
    prompt_addon=(
        "[MODO: DEBATE] Investigue antes de opinar e fundamente afirmacoes com "
        "evidencias verificaveis: arquivos reais do workspace ou paginas web "
        "que voce mesmo buscou. Nunca aceite a afirmacao de outro participante "
        "sem verificar por conta propria. Use somente ferramentas de leitura "
        "autorizadas. Nao edite arquivos, nao crie tasks, nao delegue e nao "
        "solicite entrada humana."
    ),
)


def get_mode(command: str | None) -> ExecutionMode | None:
    """Retorna o ExecutionMode para o comando /modo, ou None se não reconhecido."""
    if not isinstance(command, str) or not command.strip():
        return None
    return MODES.get(command.lower())

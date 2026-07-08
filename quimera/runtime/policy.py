"""Componentes de `quimera.runtime.policy`."""
from __future__ import annotations

from pathlib import Path

from ..shared_state import MAX_AGENT_UPDATE_KEYS
from .config import ToolRuntimeConfig
from .models import ToolCall


def is_path_inside(path: Path, root: Path) -> bool:
    """Return True when *path* resolves inside *root*.

    Both paths are resolved before comparison to avoid false prefix matches
    (e.g. ``/home/foo-bar`` is NOT inside ``/home/foo``).
    """
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


class ToolPolicyError(Exception):
    """Erro lançado quando uma chamada de ferramenta viola a política de segurança."""
    pass


class PathPermissionError(ToolPolicyError):
    """Raised when a tool needs user permission to access a path outside the default workspace."""

    def __init__(self, raw_path: str, resolved_path: Path) -> None:
        """Inicializa uma instância de PathPermissionError."""
        self.raw_path = raw_path
        self.resolved_path = resolved_path
        super().__init__(f"Permissão necessária para acessar: {resolved_path}")


class ToolPolicy:
    """Valida chamadas de ferramentas contra regras de segurança, permissão de path e aprovação."""

    _SHELL_CHAIN_OPERATORS = (";", "&&", "||", "|", "`", "$(")
    _POLICY_BYPASS_TOOLS: set[str] = set()

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de ToolPolicy."""
        self.config = config
        self.blocked_tools: list[str] = []
        self._tool_validators: dict[str, object] = {}

    def register_tool_validator(self, tool_names: list[str], tool) -> None:
        """Registra um ValidatableTool como responsável pela validação das tools listadas."""
        for name in tool_names:
            self._tool_validators[name] = tool

    def validate(self, call: ToolCall) -> None:
        """Valida a chamada: despacha para o validator registrado ou para _validate_<name>."""
        if call.name in self.blocked_tools:
            raise ToolPolicyError(
                f"Ferramenta '{call.name}' bloqueada pelo modo de execução ativo."
            )
        tool = self._tool_validators.get(call.name)
        if tool is not None:
            tool.validate(call)
            return
        validator_name = f"_validate_{call.name}"
        validator = getattr(self, validator_name, None)
        if validator is None:
            raise ToolPolicyError(f"Sem política para a ferramenta: {call.name}")
        validator(call)

    def requires_validation(self, call: ToolCall) -> bool:
        """Retorna True quando a tool deve passar por validação de policy."""
        return call.name not in self._POLICY_BYPASS_TOOLS

    def requires_path_permission(self, call: ToolCall) -> bool:
        """Retorna True quando a tool precisa validar permissão de path."""
        return call.name in {"read_file", "list_files", "grep_search", "remove_file"}

    def requires_approval(self, call: ToolCall) -> bool:
        """Retorna True quando a tool requer aprovação humana antes de ser executada."""
        if call.name in {
            "write_file",
            "apply_patch",
            "run_shell",
            "run_shell_command",
            "exec_command",
            "poll_command_session",
            "close_command_session",
            "remove_file",
            "write_stdin",
            "delegate",
            "git_add",
            "git_commit",
            "git_checkout",
            "git_push",
        }:
            return self.config.require_approval_for_mutations
        return False

    def check_path_permission(self, call: ToolCall) -> PathPermissionError | None:
        """Check if the tool needs user permission to access a path outside allowed roots."""
        if not self.requires_path_permission(call):
            return None

        raw = call.arguments.get("path", ".")
        normalized = raw.lstrip("/") or "."
        path = (self.config.workspace_root / normalized).resolve()

        for allowed_root in self.config.allowed_read_roots:
            if is_path_inside(path, allowed_root):
                return None

        return PathPermissionError(raw, path)

    def _validate_propose_task(self, call: ToolCall) -> None:
        """propose_task foi desativada; crie tasks apenas com o comando /task do humano."""
        raise ToolPolicyError("propose_task foi desativada; crie tasks apenas com o comando /task do humano")

    def _validate_approve_task(self, call: ToolCall) -> None:
        """approve_task foi desativada no chat."""
        raise ToolPolicyError("approve_task foi desativada no chat; tasks humanas já nascem roteadas")

    def _validate_complete_task(self, call: ToolCall) -> None:
        """complete_task não é exposta no chat."""
        raise ToolPolicyError("complete_task não é exposta no chat; o executor interno encerra a task")

    def _validate_fail_task(self, call: ToolCall) -> None:
        """fail_task não é exposta no chat."""
        raise ToolPolicyError("fail_task não é exposta no chat; o executor interno encerra a task")

    def _validate_ask_user(self, call: ToolCall) -> None:
        """ask_user é seguro: apenas exibe pergunta e aguarda resposta do usuário.

        Sem 'options' a pergunta é de texto livre; com 'options' é enquete e
        exige pelo menos 2 itens.
        """
        question = str(call.arguments.get("question") or "").strip()
        if not question:
            raise ToolPolicyError("ask_user requer 'question'")
        options = call.arguments.get("options") or []
        if options and len(options) < 2:
            raise ToolPolicyError("ask_user com 'options' requer pelo menos 2 opções (ou omita para texto livre)")

    def _validate_update_shared_state(self, call: ToolCall) -> None:
        """update_shared_state é seguro: apenas mescla campos no shared_state em memória.

        Tipo/tamanho de cada valor são validados depois em
        ``shared_state.validate_agent_state_value``; aqui rejeitamos cedo um
        payload com número de campos muito acima do contrato de agente, para
        evitar processar chamadas obviamente abusivas.
        """
        updates = call.arguments.get("updates")
        if not isinstance(updates, dict) or not updates:
            raise ToolPolicyError("update_shared_state requer 'updates' como objeto não vazio")
        if len(updates) > MAX_AGENT_UPDATE_KEYS:
            raise ToolPolicyError(
                f"update_shared_state aceita no máximo {MAX_AGENT_UPDATE_KEYS} campos por chamada"
            )

    def _validate_run_shell_command(self, call: ToolCall) -> None:
        """Valida o alias legado `run_shell_command` com política mínima de shell."""
        command = str(call.arguments.get("command", "")).strip()
        if not command:
            raise ToolPolicyError("run_shell_command requer um comando não vazio")
        policy = self.config.workspace_policy
        if policy is not None and policy.shell_allow_chaining:
            return
        for op in self._SHELL_CHAIN_OPERATORS:
            if op in command:
                raise ToolPolicyError(
                    f"Comando bloqueado: operador de encadeamento proibido: '{op}'"
                )

"""Componentes de `quimera.runtime.policy`."""
from __future__ import annotations

import shlex
from pathlib import Path

from .config import ToolRuntimeConfig
from .models import ToolCall


class ToolPolicyError(Exception):
    """Implementa `ToolPolicyError`."""
    pass


class PathPermissionError(ToolPolicyError):
    """Raised when a tool needs user permission to access a path outside the default workspace."""
    def __init__(self, raw_path: str, resolved_path: Path) -> None:
        """Inicializa uma instância de PathPermissionError."""
        self.raw_path = raw_path
        self.resolved_path = resolved_path
        super().__init__(f"Permissão necessária para acessar: {resolved_path}")


class ToolPolicy:
    """Implementa `ToolPolicy`."""
    _SHELL_CHAIN_OPERATORS = (";", "&&", "||", "`", "$(")

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de ToolPolicy."""
        self.config = config

    def validate(self, call: ToolCall) -> None:
        """Executa validate."""
        validator_name = f"_validate_{call.name}"
        validator = getattr(self, validator_name, None)
        if validator is None:
            raise ToolPolicyError(f"Sem política para a ferramenta: {call.name}")
        validator(call)

    def requires_approval(self, call: ToolCall) -> bool:
        """Executa requires approval."""
        if call.name in {"write_file", "apply_patch", "run_shell"}:
            return self.config.require_approval_for_mutations
        return False

    def check_path_permission(self, call: ToolCall) -> PathPermissionError | None:
        """Check if the tool needs user permission to access a path outside allowed roots."""
        if call.name not in {"read_file", "list_files", "grep_search"}:
            return None
        
        raw = call.arguments.get("path", ".")
        normalized = raw.lstrip("/") or "."
        path = (self.config.workspace_root / normalized).resolve()
        
        for allowed_root in self.config.allowed_read_roots:
            if str(path).startswith(str(allowed_root)):
                return None
        
        return PathPermissionError(raw, path)

    def _validate_list_files(self, call: ToolCall) -> None:
        """Executa validate list files."""
        self._resolve_workspace_path(call.arguments.get("path", "."))

    def _validate_read_file(self, call: ToolCall) -> None:
        """Executa validate read file."""
        raw = call.arguments.get("path")
        if not raw:
            raise ToolPolicyError("read_file requer 'path'")
        path = self._resolve_workspace_path(raw)
        if not path.is_file():
            raise ToolPolicyError(f"Arquivo inválido para leitura: {path}")

    def _validate_write_file(self, call: ToolCall) -> None:
        """Executa validate write file."""
        raw = call.arguments.get("path")
        if not raw:
            raise ToolPolicyError("write_file requer 'path'")
        path = self._resolve_workspace_path(raw)
        if "content" not in call.arguments:
            raise ToolPolicyError("write_file requer 'content'")
        mode = str(call.arguments.get("mode", "overwrite"))
        replace_existing = bool(call.arguments.get("replace_existing", False))
        if mode == "overwrite" and path.exists() and not replace_existing:
            raise ToolPolicyError(
                "write_file não pode sobrescrever arquivo existente sem replace_existing=true; "
                "para edições parciais use apply_patch"
            )

    def _validate_apply_patch(self, call: ToolCall) -> None:
        """Executa validate apply patch."""
        patch = str(call.arguments.get("patch", "")).strip()
        if not patch:
            raise ToolPolicyError("apply_patch requer 'patch'")

    def _validate_grep_search(self, call: ToolCall) -> None:
        """Executa validate grep search."""
        self._resolve_workspace_path(call.arguments.get("path", "."))
        pattern = str(call.arguments.get("pattern", "")).strip()
        if not pattern:
            raise ToolPolicyError("grep_search requer um padrão não vazio")

    def _validate_propose_task(self, call: ToolCall) -> None:
        """Executa validate propose task."""
        raise ToolPolicyError("propose_task foi desativada; crie tasks apenas com o comando /task do humano")

    def _validate_list_tasks(self, call: ToolCall) -> None:
        """Executa validate list tasks."""
        pass

    def _validate_list_jobs(self, call: ToolCall) -> None:
        """Executa validate list jobs."""
        pass

    def _validate_get_job(self, call: ToolCall) -> None:
        """Executa validate get job."""
        return

    def _validate_approve_task(self, call: ToolCall) -> None:
        """Executa validate approve task."""
        raise ToolPolicyError("approve_task foi desativada no chat; tasks humanas já nascem roteadas")

    def _validate_complete_task(self, call: ToolCall) -> None:
        """Executa validate complete task."""
        raise ToolPolicyError("complete_task não é exposta no chat; o executor interno encerra a task")

    def _validate_fail_task(self, call: ToolCall) -> None:
        """Executa validate fail task."""
        raise ToolPolicyError("fail_task não é exposta no chat; o executor interno encerra a task")

    def _validate_run_shell(self, call: ToolCall) -> None:
        """Executa validate run shell."""
        command = str(call.arguments.get("command", "")).strip()
        if not command:
            raise ToolPolicyError("run_shell requer um comando não vazio")
        for op in self._SHELL_CHAIN_OPERATORS:
            if op in command:
                raise ToolPolicyError(f"Comando bloqueado: operador de encadeamento proibido: '{op}'")
        lowered = f" {command.lower()} "
        for pattern in self.config.shell_denylist_patterns:
            if pattern.lower() in lowered:
                raise ToolPolicyError(f"Comando bloqueado pela denylist: {pattern}")
        try:
            first_token = shlex.split(command)[0]
        except Exception as exc:  # noqa: BLE001
            raise ToolPolicyError(f"Comando inválido: {command}") from exc
        if first_token not in self.config.shell_allowlist:
            raise ToolPolicyError(f"Comando fora da allowlist: {first_token}")

    def _resolve_workspace_path(self, raw_path: str) -> Path:
        """Resolve workspace path."""
        normalized = raw_path.lstrip("/") or "."
        path = (self.config.workspace_root / normalized).resolve()
        if not str(path).startswith(str(self.config.workspace_root)):
            raise ToolPolicyError(f"Path fora da workspace: {raw_path}")
        return path

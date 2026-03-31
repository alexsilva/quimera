from __future__ import annotations

import shlex
from pathlib import Path

from .config import ToolRuntimeConfig
from .models import ToolCall


class ToolPolicyError(Exception):
    pass


class ToolPolicy:
    def __init__(self, config: ToolRuntimeConfig) -> None:
        self.config = config

    def validate(self, call: ToolCall) -> None:
        validator_name = f"_validate_{call.name}"
        validator = getattr(self, validator_name, None)
        if validator is None:
            raise ToolPolicyError(f"Sem política para a ferramenta: {call.name}")
        validator(call)

    def requires_approval(self, call: ToolCall) -> bool:
        if call.name in {"write_file", "run_shell"}:
            return self.config.require_approval_for_mutations
        return False

    def _validate_list_files(self, call: ToolCall) -> None:
        self._resolve_workspace_path(call.arguments.get("path", "."))

    def _validate_read_file(self, call: ToolCall) -> None:
        raw = call.arguments.get("path")
        if not raw:
            raise ToolPolicyError("read_file requer 'path'")
        path = self._resolve_workspace_path(raw)
        if not path.is_file():
            raise ToolPolicyError(f"Arquivo inválido para leitura: {path}")

    def _validate_write_file(self, call: ToolCall) -> None:
        raw = call.arguments.get("path")
        if not raw:
            raise ToolPolicyError("write_file requer 'path'")
        self._resolve_workspace_path(raw)
        if "content" not in call.arguments:
            raise ToolPolicyError("write_file requer 'content'")

    def _validate_grep_search(self, call: ToolCall) -> None:
        self._resolve_workspace_path(call.arguments.get("path", "."))
        pattern = str(call.arguments.get("pattern", "")).strip()
        if not pattern:
            raise ToolPolicyError("grep_search requer um padrão não vazio")

    _SHELL_CHAIN_OPERATORS = (";", "&&", "||", "`", "$(")

    def _validate_run_shell(self, call: ToolCall) -> None:
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
        normalized = raw_path.lstrip("/") or "."
        path = (self.config.workspace_root / normalized).resolve()
        if not str(path).startswith(str(self.config.workspace_root)):
            raise ToolPolicyError(f"Path fora da workspace: {raw_path}")
        return path

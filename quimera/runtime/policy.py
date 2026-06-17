"""Componentes de `quimera.runtime.policy`."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

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
    _SHELL_CHAIN_OPERATORS = (";", "&&", "||", "|", "`", "$(")
    _POLICY_BYPASS_TOOLS: set[str] = set()
    # Comandos que aceitam paths de arquivo como argumento — sujeitos à validação de workspace
    _FILE_PATH_CMDS: frozenset[str] = frozenset({"cat", "head", "tail", "less", "grep", "sed", "find", "ls"})

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
        """Executa validate."""
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
        """Executa requires approval."""
        if call.name in {
            "write_file",
            "apply_patch",
            "run_shell",
            "run_shell_command",
            "exec_command",
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


    def _validate_delegate(self, call: ToolCall) -> None:
        """Valida delegação cross-agent antes do ApprovalBroker."""
        reserved = {
            "allowlisted",
            "approval_budget",
            "approval_scope_id",
            "transport",
            "run_id",
            "parent_run_id",
        }
        present_reserved = sorted(reserved.intersection(call.arguments))
        if present_reserved:
            raise ToolPolicyError(
                "delegate recebeu campos reservados não confiáveis: "
                + ", ".join(present_reserved)
            )
        target_agent = call.arguments.get("target_agent")
        request = call.arguments.get("request")
        if not isinstance(target_agent, str) or not target_agent.strip():
            raise ToolPolicyError("delegate requer 'target_agent' não vazio")
        if not isinstance(request, str) or not request.strip():
            raise ToolPolicyError("delegate requer 'request' não vazia")
        context = call.arguments.get("context")
        if context is not None and not isinstance(context, str):
            raise ToolPolicyError("delegate.context deve ser string quando fornecido")
        fallback_agents = call.arguments.get("fallback_agents", [])
        if fallback_agents is not None and not isinstance(fallback_agents, list):
            raise ToolPolicyError("delegate.fallback_agents deve ser uma lista")
        steps = call.arguments.get("steps")
        if steps is not None:
            if not isinstance(steps, list):
                raise ToolPolicyError("delegate.steps deve ser uma lista")
            for i, step in enumerate(steps):
                if not isinstance(step, dict):
                    raise ToolPolicyError(f"delegate.steps[{i}] deve ser um objeto")
                step_target = step.get("target_agent")
                step_request = step.get("request")
                if not isinstance(step_target, str) or not step_target.strip():
                    raise ToolPolicyError(f"delegate.steps[{i}].target_agent não pode ser vazio")
                if not isinstance(step_request, str) or not step_request.strip():
                    raise ToolPolicyError(f"delegate.steps[{i}].request não pode ser vazio")

    def _validate_list_agents(self, call: ToolCall) -> None:
        """list_agents não requer argumentos — sempre válida."""
        pass

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

    def _validate_remove_file(self, call: ToolCall) -> None:
        """Valida uma chamada de remoção de arquivo."""
        raw = call.arguments.get("path")
        if not raw:
            raise ToolPolicyError("remove_file requer 'path'")
        path = self._resolve_workspace_path(raw)
        dry_run = call.arguments.get("dry_run", True)
        if dry_run is not False:
            raise ToolPolicyError(
                "remove_file requer dry_run=False explícito para confirmar a remoção"
            )

    def _validate_web_search(self, call: ToolCall) -> None:
        """Valida uma chamada de busca na web."""
        query = call.arguments.get("query", "")
        if not query or not str(query).strip():
            raise ToolPolicyError("web_search requer 'query' não vazia")

    def _validate_web_fetch(self, call: ToolCall) -> None:
        """Valida uma chamada de fetch de URL."""
        url = call.arguments.get("url")
        if isinstance(url, str) and url.strip():
            return
        raise ToolPolicyError("web_fetch requer 'url' não vazia")

    def _validate_propose_task(self, call: ToolCall) -> None:
        """Executa validate propose task."""
        raise ToolPolicyError("propose_task foi desativada; crie tasks apenas com o comando /task do humano")

    def _validate_list_tasks(self, call: ToolCall) -> None:
        """Executa validate list tasks."""
        # Exige ao menos um filtro para evitar DoS por listagem sem limites
        filt = call.arguments.get("filters") or {}
        has_top_level_filter = any(
            call.arguments.get(k) is not None
            for k in ("job_id", "status", "assigned_to", "id")
        )
        has_dict_filter = isinstance(filt, dict) and bool(filt)
        if not has_top_level_filter and not has_dict_filter:
            raise ToolPolicyError(
                "list_tasks exige ao menos um filtro (job_id, status, assigned_to, id ou filters)"
            )

    def _validate_list_jobs(self, call: ToolCall) -> None:
        """Executa validate list jobs."""
        pass

    def _validate_get_job(self, call: ToolCall) -> None:
        """Executa validate get job."""
        return

    def _validate_todo_write(self, call: ToolCall) -> None:
        """Executa validate todo write."""
        items = call.arguments.get("todos")
        if not isinstance(items, list) or not items:
            raise ToolPolicyError("todo_write requer 'todos' como lista não vazia")
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ToolPolicyError(f"todo_write: item {i} deve ser um dicionário")
            if not item.get("content"):
                raise ToolPolicyError(f"todo_write: item {i} requer 'content' não vazio")
            status = item.get("status")
            if status and status not in ("pending", "in_progress", "done", "cancelled"):
                raise ToolPolicyError(
                    f"todo_write: status inválido '{status}' em item {i}"
                )
            priority = item.get("priority")
            if priority and priority not in ("high", "medium", "low"):
                raise ToolPolicyError(
                    f"todo_write: priority inválida '{priority}' em item {i}"
                )

    def _validate_todo_list(self, call: ToolCall) -> None:
        """Executa validate todo list."""
        pass

    def _validate_memory_save(self, call: ToolCall) -> None:
        namespace = call.arguments.get("namespace")
        key = call.arguments.get("key")
        if not isinstance(namespace, str) or not namespace.strip():
            raise ToolPolicyError("memory_save requer 'namespace' não vazio")
        if not isinstance(key, str) or not key.strip():
            raise ToolPolicyError("memory_save requer 'key' não vazio")
        self._validate_memory_token(namespace, field_name="namespace")
        self._validate_memory_token(key, field_name="key")
        if "value" not in call.arguments:
            raise ToolPolicyError("memory_save requer 'value'")
        try:
            serialized = json.dumps(call.arguments.get("value"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ToolPolicyError("memory_save requer 'value' JSON-serializable") from exc
        if len(serialized.encode("utf-8")) > 32_000:
            raise ToolPolicyError("memory_save rejeitou value grande demais; limite de 32000 bytes serializados")
        ttl = call.arguments.get("ttl_seconds")
        if ttl is not None:
            try:
                ttl_int = int(ttl)
            except (TypeError, ValueError) as exc:
                raise ToolPolicyError("memory_save.ttl_seconds deve ser inteiro positivo") from exc
            if ttl_int <= 0:
                raise ToolPolicyError("memory_save.ttl_seconds deve ser inteiro positivo")

    def _validate_memory_retrieve(self, call: ToolCall) -> None:
        namespace = call.arguments.get("namespace")
        key = call.arguments.get("key")
        prefix = call.arguments.get("prefix")
        if namespace is not None:
            if not isinstance(namespace, str) or not namespace.strip():
                raise ToolPolicyError("memory_retrieve.namespace deve ser string não vazia")
            self._validate_memory_token(namespace, field_name="namespace")
        if key is not None:
            if not isinstance(key, str) or not key.strip():
                raise ToolPolicyError("memory_retrieve.key deve ser string não vazia")
            self._validate_memory_token(key, field_name="key")
        if prefix is not None:
            if not isinstance(prefix, str) or not prefix.strip():
                raise ToolPolicyError("memory_retrieve.prefix deve ser string não vazia")
            self._validate_memory_token(prefix, field_name="prefix")
        tags = call.arguments.get("tags")
        if tags is not None:
            if not isinstance(tags, list):
                raise ToolPolicyError("memory_retrieve.tags deve ser lista de strings")
            for tag in tags:
                if not isinstance(tag, str) or not tag.strip():
                    raise ToolPolicyError("memory_retrieve.tags deve conter apenas strings não vazias")
                self._validate_memory_token(tag, field_name="tag")
        limit = call.arguments.get("limit")
        if limit is not None:
            try:
                limit_int = int(limit)
            except (TypeError, ValueError) as exc:
                raise ToolPolicyError("memory_retrieve.limit deve ser inteiro positivo") from exc
            if limit_int <= 0:
                raise ToolPolicyError("memory_retrieve.limit deve ser inteiro positivo")

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
        self._validate_shell_command(command, tool_name="run_shell")

    def _validate_run_shell_command(self, call: ToolCall) -> None:
        """Valida o alias legado `run_shell_command` com a mesma política de `run_shell`."""
        self._validate_run_shell(call)

    def _validate_exec_command(self, call: ToolCall) -> None:
        """Valida uma chamada interativa de execução de comando."""
        command = str(call.arguments.get("cmd", "")).strip()
        self._validate_shell_command(command, tool_name="exec_command")
        raw_workdir = call.arguments.get("workdir")
        if raw_workdir is not None:
            self._resolve_workspace_path(str(raw_workdir))

    def _validate_write_stdin(self, call: ToolCall) -> None:
        """Valida uma operação de escrita ou polling em sessão ativa."""
        if "session_id" not in call.arguments:
            raise ToolPolicyError("write_stdin requer 'session_id'")
        try:
            int(call.arguments["session_id"])
        except (ValueError, TypeError) as exc:
            raise ToolPolicyError("write_stdin requer um session_id inteiro") from exc
        if "yield_time_ms" in call.arguments:
            try:
                int(call.arguments["yield_time_ms"])
            except (ValueError, TypeError) as exc:
                raise ToolPolicyError("write_stdin requer yield_time_ms inteiro") from exc

    def _validate_close_command_session(self, call: ToolCall) -> None:
        """Valida o fechamento explícito de uma sessão de comando."""
        if "session_id" not in call.arguments:
            raise ToolPolicyError("close_command_session requer 'session_id'")
        try:
            int(call.arguments["session_id"])
        except (ValueError, TypeError) as exc:
            raise ToolPolicyError("close_command_session requer um session_id inteiro") from exc

    def _validate_shell_command(self, command: str, *, tool_name: str) -> None:
        """Aplica a política comum de shell para ferramentas de comando."""
        if not command:
            raise ToolPolicyError(f"{tool_name} requer um comando não vazio")
        for op in self._SHELL_CHAIN_OPERATORS:
            if op in command:
                raise ToolPolicyError(f"Comando bloqueado: operador de encadeamento proibido: '{op}'")
        lowered = f" {command.lower()} "
        for pattern in self.config.shell_denylist_patterns:
            if pattern.lower() in lowered:
                raise ToolPolicyError(f"Comando bloqueado pela denylist: {pattern}")
        try:
            tokens = shlex.split(command)
            first_token = tokens[0]
        except Exception as exc:  # noqa: BLE001
            raise ToolPolicyError(f"Comando inválido: {command}") from exc
        if first_token not in self.config.shell_allowlist:
            raise ToolPolicyError(f"Comando fora da allowlist: {first_token}")
        if first_token == "git" and len(tokens) > 1 and tokens[1] in {"push"}:
            raise ToolPolicyError("Comando bloqueado: git push exige confirmação forte fora do shell MCP")
        if first_token in self._FILE_PATH_CMDS:
            self._validate_shell_file_paths(tokens[1:])

    def _validate_shell_file_paths(self, args: list[str]) -> None:
        """Valida que paths absolutos em argumentos de comandos de leitura ficam dentro do workspace."""
        for arg in args:
            expanded = Path(arg).expanduser()
            if not expanded.is_absolute():
                continue
            resolved = expanded.resolve()
            if not is_path_inside(resolved, self.config.workspace_root):
                raise ToolPolicyError(f"Caminho fora do workspace: {arg}")

    def _resolve_workspace_path(self, raw_path: str) -> Path:
        """Resolve workspace path."""
        normalized = raw_path.lstrip("/") or "."
        path = (self.config.workspace_root / normalized).resolve()
        if not is_path_inside(path, self.config.workspace_root):
            raise ToolPolicyError(f"Path fora da workspace: {raw_path}")
        return path

    @staticmethod
    def _validate_memory_token(value: str, *, field_name: str) -> None:
        if value.startswith("/") or "/" in value or "\\" in value:
            raise ToolPolicyError(f"{field_name} não pode conter path")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
        if any(ch not in allowed for ch in value):
            raise ToolPolicyError(
                f"{field_name} contém caracteres inválidos; use apenas letras, números, '.', '_', ':' ou '-'"
            )

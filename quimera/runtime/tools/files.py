"""Componentes de `quimera.runtime.tools.files`."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError, is_path_inside
from .base import ToolBase, ValidatableTool

_logger = logging.getLogger("quimera.staging")
_thread_local = threading.local()
_FILE_TOOL_NAMES = [
    "list_files",
    "read_file",
    "write_file",
    "remove_file",
    "grep_search",
]


def get_staging_root() -> Path | None:
    """Retorna staging root."""
    return getattr(_thread_local, "staging_root", None)


def set_staging_root(path: Path | None) -> None:
    """Define staging root."""
    _thread_local.staging_root = path
    if path:
        _logger.debug("staging initialized: %s (thread=%s)", path, threading.current_thread().name)
    else:
        _logger.debug("staging cleared (thread=%s)", threading.current_thread().name)


class FileTools(ToolBase):
    """Implementa `FileTools`."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de FileTools."""
        super().__init__(config)

    def _is_allowed_path(self, path: Path) -> bool:
        """Retorna True quando o path está em staging ou em algum root permitido."""
        staging = get_staging_root()
        if staging and path.is_relative_to(staging):
            return True
        return any(path.is_relative_to(allowed) for allowed in self.config.allowed_read_roots)

    def _resolve(self, raw_path: str) -> Path:
        """Resolve resolve."""
        normalized = raw_path.lstrip("/") or "."
        staging = get_staging_root()
        base = staging if staging else self.config.workspace_root
        path = (base / normalized).resolve()

        if self._is_allowed_path(path):
            return path

        raise ValueError(f"Path fora da workspace: {raw_path}")

    def list_files(self, call: ToolCall) -> ToolResult:
        """Lista files."""
        staging = get_staging_root()
        workspace = self.config.workspace_root
        raw_path = call.arguments.get("path", ".")

        path = self._resolve(raw_path)

        base = path

        all_names: dict[str, tuple[Path, bool]] = {}

        if base.exists():
            for item in base.iterdir():
                all_names[item.name] = (item, item.is_dir())

        if staging and base != staging and path.is_relative_to(workspace):
            staging_check = staging / (raw_path.lstrip("/") or ".")
            if staging_check.exists():
                for item in staging_check.iterdir():
                    all_names[item.name] = (item, item.is_dir())

        entries = []
        for name, (item, is_dir) in sorted(all_names.items(), key=lambda x: (not x[1][1], x[0].lower())):
            suffix = "/" if is_dir else ""
            entries.append(f"{name}{suffix}")

        return ToolResult(ok=True, tool_name=call.name, content="\n".join(entries))

    def read_file(self, call: ToolCall) -> ToolResult:
        """Lê arquivo, com suporte a range de linhas."""
        staging = get_staging_root()
        raw_path = call.arguments["path"]
        start_line = call.arguments.get("start_line")
        end_line = call.arguments.get("end_line")

        if staging:
            staging_path = (staging / raw_path.lstrip("/")).resolve()
            if staging_path.is_relative_to(staging) and staging_path.exists():
                path = staging_path
            else:
                # Fall back to the real workspace when staging is active but does
                # not contain the requested file.
                workspace_path = (self.config.workspace_root / (raw_path.lstrip("/") or ".")).resolve()
                if not workspace_path.is_relative_to(self.config.workspace_root):
                    raise ValueError(f"Path fora da workspace: {raw_path}")
                path = workspace_path
        else:
            path = self._resolve(raw_path)

        text = path.read_text(encoding="utf-8")

        if start_line is not None or end_line is not None:
            lines = text.splitlines(keepends=True)
            total = len(lines)
            start = (int(start_line) - 1) if start_line is not None else 0
            end = int(end_line) if end_line is not None else total
            if start < 0:
                start = 0
            if end > total:
                end = total
            if start >= end:
                return ToolResult(
                    ok=False,
                    tool_name=call.name,
                    error=f"Intervalo inválido: start_line={start_line}, end_line={end_line}. "
                          f"Arquivo tem {total} linhas.",
                )
            text = "".join(lines[start:end])

        truncated = len(text) > self.config.max_file_read_chars
        text = text[: self.config.max_file_read_chars]
        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=text,
            truncated=truncated,
            data={"path": str(path)},
        )

    def write_file(self, call: ToolCall) -> ToolResult:
        """Escreve file."""
        path = self._resolve(call.arguments["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = str(call.arguments.get("mode", "overwrite"))
        content = str(call.arguments["content"])
        replace_existing = bool(call.arguments.get("replace_existing", False))
        if mode == "create" and path.exists():
            return ToolResult(ok=False, tool_name=call.name, error=f"Arquivo já existe: {path}")
        if mode == "overwrite" and path.exists() and not replace_existing:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=(
                    "write_file não pode sobrescrever arquivo existente sem replace_existing=true; "
                    "para edições parciais use apply_patch"
                ),
            )
        if mode == "append":
            with path.open("a", encoding="utf-8") as fh:
                fh.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        return ToolResult(ok=True, tool_name=call.name, content=f"Arquivo salvo: {path}")

    def remove_file(self, call: ToolCall) -> ToolResult:
        """Remove um arquivo dentro do workspace.

        Por segurança, apenas remove arquivos (não diretórios) e exige
        confirmação explícita via dry_run=False.
        """
        raw_path = call.arguments["path"]
        path = self._resolve(raw_path)

        dry_run = bool(call.arguments.get("dry_run", True))

        if not path.exists():
            return ToolResult(ok=False, tool_name=call.name,
                              error=f"Arquivo não encontrado: {raw_path}")

        if path.is_dir():
            return ToolResult(ok=False, tool_name=call.name,
                              error=f"remove_file não remove diretórios: {raw_path}")

        if not path.is_file():
            return ToolResult(ok=False, tool_name=call.name,
                              error=f"Caminho não é um arquivo regular: {raw_path}")

        if dry_run:
            return ToolResult(ok=True, tool_name=call.name,
                              content=f"[dry-run] Removeria: {path}")

        try:
            path.unlink()
            return ToolResult(ok=True, tool_name=call.name,
                              content=f"Arquivo removido: {path}")
        except OSError as exc:
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

    def grep_search(self, call: ToolCall) -> ToolResult:
        """Executa grep search."""
        staging = get_staging_root()
        workspace = self.config.workspace_root
        raw_path = call.arguments.get("path", ".")
        base = self._resolve(raw_path)
        pattern = str(call.arguments["pattern"])
        results: list[str] = []

        # We always want to search the resolved path (which might be in staging)
        search_paths = [base]

        # If we are in staging, we ALSO want to search the corresponding path in the real workspace
        if staging and base.is_relative_to(staging):
            rel = base.relative_to(staging)
            workspace_base = (workspace / rel).resolve()
            if workspace_base.exists() and workspace_base != base:
                search_paths.append(workspace_base)

        seen_results = set()
        for search_path in search_paths:
            if not search_path.exists():
                continue
            for file_path in search_path.rglob("*"):
                if not file_path.is_file():
                    continue
                try:
                    text = file_path.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    continue

                # Determine display path
                try:
                    display_path = file_path.relative_to(workspace)
                except ValueError:
                    if staging and file_path.is_relative_to(staging):
                        display_path = file_path.relative_to(staging)
                    else:
                        display_path = file_path

                for line_no, line in enumerate(text.splitlines(), start=1):
                    if pattern in line:
                        res_key = (str(display_path), line_no, line)
                        if res_key in seen_results:
                            continue
                        seen_results.add(res_key)

                        results.append(f"{display_path}:{line_no}:{line}")
                        if len(results) >= self.config.max_search_results:
                            return ToolResult(
                                ok=True,
                                tool_name=call.name,
                                content="\n".join(results),
                                truncated=True,
                            )
        return ToolResult(ok=True, tool_name=call.name, content="\n".join(results))


class FileToolsValidator(ValidatableTool):
    """Validação de policy para as ferramentas de arquivo."""

    def _resolve_workspace_or_staging_path(self, raw_path: str) -> Path:
        """Resolve path no workspace e aceita também o staging root quando ativo."""
        path = self._resolve_workspace_path(raw_path)
        staging = get_staging_root()
        if staging is None:
            return path
        normalized = raw_path.lstrip("/") or "."
        staging_path = (staging / normalized).resolve()
        if is_path_inside(staging_path, staging):
            return staging_path
        return path

    def _validate_list_files(self, call: ToolCall) -> None:
        """Valida list_files."""
        self._resolve_workspace_or_staging_path(str(call.arguments.get("path", ".")))

    def _validate_read_file(self, call: ToolCall) -> None:
        """Valida read_file."""
        raw_path = call.arguments.get("path")
        if not raw_path:
            raise ToolPolicyError("read_file requer 'path'")
        workspace_path = self._resolve_workspace_path(str(raw_path))
        staging_path = self._resolve_workspace_or_staging_path(str(raw_path))
        if not workspace_path.is_file() and not staging_path.is_file():
            raise ToolPolicyError(f"Arquivo inválido para leitura: {workspace_path}")

    def _validate_write_file(self, call: ToolCall) -> None:
        """Valida write_file."""
        raw_path = call.arguments.get("path")
        if not raw_path:
            raise ToolPolicyError("write_file requer 'path'")
        path = self._resolve_workspace_or_staging_path(str(raw_path))
        if "content" not in call.arguments:
            raise ToolPolicyError("write_file requer 'content'")
        mode = str(call.arguments.get("mode", "overwrite"))
        replace_existing = bool(call.arguments.get("replace_existing", False))
        if mode == "overwrite" and path.exists() and not replace_existing:
            raise ToolPolicyError(
                "write_file não pode sobrescrever arquivo existente sem replace_existing=true; "
                "para edições parciais use apply_patch"
            )

    def _validate_grep_search(self, call: ToolCall) -> None:
        """Valida grep_search."""
        self._resolve_workspace_or_staging_path(str(call.arguments.get("path", ".")))
        pattern = str(call.arguments.get("pattern", "")).strip()
        if not pattern:
            raise ToolPolicyError("grep_search requer um padrão não vazio")

    def _validate_remove_file(self, call: ToolCall) -> None:
        """Valida remove_file."""
        raw_path = call.arguments.get("path")
        if not raw_path:
            raise ToolPolicyError("remove_file requer 'path'")
        self._resolve_workspace_or_staging_path(str(raw_path))
        dry_run = call.arguments.get("dry_run", True)
        if dry_run is not False:
            raise ToolPolicyError(
                "remove_file requer dry_run=False explícito para confirmar a remoção"
            )


def register(registry, policy, config) -> None:
    """Registra todas as ferramentas de arquivo e sua validação na policy."""
    file_tools = FileTools(config)
    file_validator = FileToolsValidator(config)
    for name in _FILE_TOOL_NAMES:
        registry.register(name, getattr(file_tools, name))
    policy.register_tool_validator(_FILE_TOOL_NAMES, file_validator)

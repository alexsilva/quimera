"""Componentes de `quimera.runtime.tools.files`."""
from __future__ import annotations

import logging
import ast
import fnmatch
import os
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
    "replace_text",
    "remove_file",
    "grep_search",
    "inspect_symbols",
]
_DEFAULT_GREP_EXCLUDED_DIRS = frozenset({
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
})


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

    def _resolve_mutable_path(self, raw_path: str) -> Path:
        """Resolve path para operações que alteram arquivos.

        Leituras podem usar allowed_read_roots, mas escrita e remoção ficam
        restritas à workspace ou ao staging ativo. Isso mantém autonomia total
        no projeto sem transformar roots somente-leitura em superfície mutável.
        """
        normalized = raw_path.lstrip("/") or "."
        staging = get_staging_root()
        base = staging if staging else self.config.workspace_root
        path = (base / normalized).resolve()

        if staging and is_path_inside(path, staging):
            return path
        if is_path_inside(path, self.config.workspace_root):
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
        all_lines = text.splitlines(keepends=True)
        total_lines = len(all_lines)
        selected_start_line = 1 if total_lines else 0
        selected_end_line = total_lines

        if start_line is not None or end_line is not None:
            lines = all_lines
            total = total_lines
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
            selected_start_line = start + 1
            selected_end_line = end

        truncated = len(text) > self.config.max_file_read_chars
        text = text[: self.config.max_file_read_chars]
        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=text,
            truncated=truncated,
            data={
                "path": str(path),
                "start_line": selected_start_line,
                "end_line": selected_end_line,
                "total_lines": total_lines,
                "truncated": truncated,
            },
        )

    def write_file(self, call: ToolCall) -> ToolResult:
        """Escreve file."""
        path = self._resolve_mutable_path(call.arguments["path"])
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

    def replace_text(self, call: ToolCall) -> ToolResult:
        """Substitui texto literal em um arquivo com contagem exata de ocorrências."""
        raw_path = call.arguments["path"]
        path = self._resolve_mutable_path(raw_path)
        if not path.is_file():
            return ToolResult(ok=False, tool_name=call.name, error=f"Arquivo inválido: {raw_path}")

        old = str(call.arguments.get("old", ""))
        new = str(call.arguments.get("new", ""))
        expected_count = self._resolve_replace_count(call.arguments.get("count"))
        if not old:
            return ToolResult(ok=False, tool_name=call.name, error="replace_text requer 'old' não vazio")

        text = path.read_text(encoding="utf-8")
        actual_count = text.count(old)
        if actual_count != expected_count:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=(
                    f"replace_text encontrou {actual_count} ocorrências, "
                    f"mas esperava {expected_count}. Nenhuma alteração aplicada."
                ),
                data={"path": str(path), "occurrences": actual_count, "expected_count": expected_count},
            )

        updated = text.replace(old, new, expected_count)
        path.write_text(updated, encoding="utf-8")
        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=f"Texto substituído em: {path}",
            data={"path": str(path), "occurrences": actual_count},
        )

    def remove_file(self, call: ToolCall) -> ToolResult:
        """Remove um arquivo dentro do workspace.

        Por segurança, apenas remove arquivos (não diretórios) e exige
        confirmação explícita via dry_run=False.
        """
        raw_path = call.arguments["path"]
        path = self._resolve_mutable_path(raw_path)

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
        include_glob = call.arguments.get("include_glob") or call.arguments.get("glob")
        max_results = self._resolve_search_limit(call.arguments.get("max_results"))
        context_lines = self._resolve_context_lines(call.arguments.get("context_lines"))
        excluded_dirs = self._resolve_excluded_search_dirs(call.arguments.get("exclude_dirs"))
        results: list[str] = []
        match_count = 0

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
            for file_path in self._iter_search_files(search_path, excluded_dirs=excluded_dirs):
                try:
                    resolved_file = file_path.resolve()
                except OSError:
                    continue
                if not self._is_allowed_path(resolved_file):
                    continue

                display_path = self._display_path(file_path, workspace=workspace, staging=staging)
                if include_glob and not self._matches_search_glob(display_path, include_glob):
                    continue

                try:
                    text = file_path.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    continue

                lines = text.splitlines()
                for line_no, line in enumerate(lines, start=1):
                    if pattern in line:
                        match_key = (str(display_path), line_no, line)
                        if match_key in seen_results:
                            continue
                        match_count += 1
                        start = max(1, line_no - context_lines)
                        end = min(len(lines), line_no + context_lines)
                        for context_no in range(start, end + 1):
                            context_line = lines[context_no - 1]
                            res_key = (str(display_path), context_no, context_line)
                            if res_key in seen_results:
                                continue
                            seen_results.add(res_key)
                            results.append(f"{display_path}:{context_no}:{context_line}")
                        if match_count >= max_results:
                            return ToolResult(
                                ok=True,
                                tool_name=call.name,
                                content="\n".join(results),
                                truncated=True,
                            )
        return ToolResult(ok=True, tool_name=call.name, content="\n".join(results))

    def inspect_symbols(self, call: ToolCall) -> ToolResult:
        """Lista símbolos Python de alto nível usando AST, sem executar o arquivo."""
        raw_path = call.arguments["path"]
        path = self._resolve(raw_path)
        if path.suffix != ".py" or not path.is_file():
            return ToolResult(ok=False, tool_name=call.name, error=f"Arquivo Python inválido: {raw_path}")

        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            return ToolResult(ok=False, tool_name=call.name, error=f"SyntaxError: {exc}")
        except OSError as exc:
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

        symbols: list[dict] = []
        rendered: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                symbol = {"kind": kind, "name": node.name, "line": node.lineno}
                symbols.append(symbol)
                rendered.append(f"{kind} {node.name}:{node.lineno}")
            elif isinstance(node, ast.ClassDef):
                symbol = {"kind": "class", "name": node.name, "line": node.lineno, "methods": []}
                rendered.append(f"class {node.name}:{node.lineno}")
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_kind = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                        method = {"kind": method_kind, "name": child.name, "line": child.lineno}
                        symbol["methods"].append(method)
                        rendered.append(f"  {method_kind} {child.name}:{child.lineno}")
                symbols.append(symbol)

        return ToolResult(
            ok=True,
            tool_name=call.name,
            content="\n".join(rendered),
            data={"path": str(path), "symbols": symbols},
        )

    def _iter_search_files(self, search_path: Path, *, excluded_dirs: set[str]):
        """Itera arquivos pesquisáveis podando diretórios ruidosos cedo."""
        if search_path.is_file():
            yield search_path
            return
        for root, dirs, files in os.walk(search_path):
            dirs[:] = [name for name in dirs if name not in excluded_dirs]
            for name in files:
                yield Path(root) / name

    @staticmethod
    def _display_path(file_path: Path, *, workspace: Path, staging: Path | None) -> Path:
        """Normaliza o path exibido nos resultados de grep_search."""
        try:
            return file_path.relative_to(workspace)
        except ValueError:
            if staging and file_path.is_relative_to(staging):
                return file_path.relative_to(staging)
            return file_path

    @staticmethod
    def _matches_search_glob(display_path: Path, include_glob) -> bool:
        """Aplica filtro glob opcional ao path exibido."""
        patterns = include_glob if isinstance(include_glob, list) else [include_glob]
        path_text = display_path.as_posix()
        return any(fnmatch.fnmatch(path_text, str(pattern)) for pattern in patterns)

    def _resolve_search_limit(self, raw_limit) -> int:
        """Normaliza limite de resultados sem exceder a configuração global."""
        if raw_limit is None:
            return self.config.max_search_results
        try:
            requested = int(raw_limit)
        except (TypeError, ValueError):
            return self.config.max_search_results
        if requested <= 0:
            return self.config.max_search_results
        return min(requested, self.config.max_search_results)

    @staticmethod
    def _resolve_replace_count(raw_count) -> int:
        """Normaliza contagem esperada para replace_text."""
        if raw_count is None:
            return 1
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            return 1
        return max(1, count)

    @staticmethod
    def _resolve_context_lines(raw_context_lines) -> int:
        """Normaliza contexto de grep com limite conservador de segurança."""
        if raw_context_lines is None:
            return 0
        try:
            requested = int(raw_context_lines)
        except (TypeError, ValueError):
            return 0
        if requested <= 0:
            return 0
        return min(requested, 10)

    @staticmethod
    def _resolve_excluded_search_dirs(raw_exclude_dirs) -> set[str]:
        """Combina exclusões padrão com exclusões adicionais do chamador."""
        excluded = set(_DEFAULT_GREP_EXCLUDED_DIRS)
        if raw_exclude_dirs is None:
            return excluded
        if isinstance(raw_exclude_dirs, str):
            raw_items = [raw_exclude_dirs]
        else:
            raw_items = raw_exclude_dirs
        for item in raw_items:
            name = str(item).strip()
            if name:
                excluded.add(name)
        return excluded


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

    def _validate_replace_text(self, call: ToolCall) -> None:
        """Valida replace_text."""
        raw_path = call.arguments.get("path")
        if not raw_path:
            raise ToolPolicyError("replace_text requer 'path'")
        self._resolve_workspace_or_staging_path(str(raw_path))
        if not str(call.arguments.get("old", "")):
            raise ToolPolicyError("replace_text requer 'old' não vazio")
        if "new" not in call.arguments:
            raise ToolPolicyError("replace_text requer 'new'")

    def _validate_grep_search(self, call: ToolCall) -> None:
        """Valida grep_search."""
        self._resolve_workspace_or_staging_path(str(call.arguments.get("path", ".")))
        pattern = str(call.arguments.get("pattern", "")).strip()
        if not pattern:
            raise ToolPolicyError("grep_search requer um padrão não vazio")

    def _validate_inspect_symbols(self, call: ToolCall) -> None:
        """Valida inspect_symbols."""
        raw_path = call.arguments.get("path")
        if not raw_path:
            raise ToolPolicyError("inspect_symbols requer 'path'")
        self._resolve_workspace_or_staging_path(str(raw_path))

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

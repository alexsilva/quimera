from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult

_logger = logging.getLogger("quimera.staging")
_thread_local = threading.local()


def get_staging_root() -> Path | None:
    return getattr(_thread_local, "staging_root", None)


def set_staging_root(path: Path | None) -> None:
    _thread_local.staging_root = path
    if path:
        _logger.debug("staging initialized: %s (thread=%s)", path, threading.current_thread().name)
    else:
        _logger.debug("staging cleared (thread=%s)", threading.current_thread().name)


class FileTools:
    def __init__(self, config: ToolRuntimeConfig) -> None:
        self.config = config

    def _resolve(self, raw_path: str) -> Path:
        normalized = raw_path.lstrip("/") or "."
        staging = get_staging_root()
        base = staging if staging else self.config.workspace_root
        path = (base / normalized).resolve()
        if not str(path).startswith(str(base)):
            raise ValueError(f"Path fora da workspace: {raw_path}")
        return path

    def list_files(self, call: ToolCall) -> ToolResult:
        staging = get_staging_root()
        workspace = self.config.workspace_root
        raw_path = call.arguments.get("path", ".")
        
        path = self._resolve(raw_path)
        
        if staging and path.is_relative_to(staging):
            base = path
        else:
            base = path
        
        all_names: dict[str, tuple[Path, bool]] = {}
        
        if base.exists():
            for item in base.iterdir():
                all_names[item.name] = (item, item.is_dir())
        
        if staging and base != staging and str(path).startswith(str(workspace)):
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
        staging = get_staging_root()
        raw_path = call.arguments["path"]
        
        staging_path = (staging / raw_path.lstrip("/")) if staging else None
        if staging_path and staging_path.exists():
            path = staging_path
        else:
            path = self._resolve(raw_path)
        
        text = path.read_text(encoding="utf-8")
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
        path = self._resolve(call.arguments["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = str(call.arguments.get("mode", "overwrite"))
        content = str(call.arguments["content"])
        if mode == "create" and path.exists():
            return ToolResult(ok=False, tool_name=call.name, error=f"Arquivo já existe: {path}")
        if mode == "append":
            with path.open("a", encoding="utf-8") as fh:
                fh.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        return ToolResult(ok=True, tool_name=call.name, content=f"Arquivo salvo: {path}")

    def grep_search(self, call: ToolCall) -> ToolResult:
        staging = get_staging_root()
        workspace = self.config.workspace_root
        base = self._resolve(call.arguments.get("path", "."))
        pattern = str(call.arguments["pattern"])
        results: list[str] = []
        
        search_paths = [base]
        if staging:
            staging_base = staging / base.name if base.name != "." else staging
            if staging_base.exists():
                search_paths.append(staging_base)
        
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
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if pattern in line:
                        results.append(f"{file_path.relative_to(workspace)}:{line_no}:{line}")
                        if len(results) >= self.config.max_search_results:
                            return ToolResult(
                                ok=True,
                                tool_name=call.name,
                                content="\n".join(results),
                                truncated=True,
                            )
        return ToolResult(ok=True, tool_name=call.name, content="\n".join(results))

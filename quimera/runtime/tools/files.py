from __future__ import annotations

from pathlib import Path

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult


class FileTools:
    def __init__(self, config: ToolRuntimeConfig) -> None:
        self.config = config

    def _resolve(self, raw_path: str) -> Path:
        path = (self.config.workspace_root / raw_path).resolve()
        if not str(path).startswith(str(self.config.workspace_root)):
            raise ValueError(f"Path fora da workspace: {raw_path}")
        return path

    def list_files(self, call: ToolCall) -> ToolResult:
        path = self._resolve(call.arguments.get("path", "."))
        entries = []
        for item in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            suffix = "/" if item.is_dir() else ""
            entries.append(f"{item.name}{suffix}")
        return ToolResult(ok=True, tool_name=call.name, content="\n".join(entries))

    def read_file(self, call: ToolCall) -> ToolResult:
        path = self._resolve(call.arguments["path"])
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
        base = self._resolve(call.arguments.get("path", "."))
        pattern = str(call.arguments["pattern"])
        results: list[str] = []
        for file_path in base.rglob("*"):
            if not file_path.is_file():
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    results.append(f"{file_path.relative_to(self.config.workspace_root)}:{line_no}:{line}")
                    if len(results) >= self.config.max_search_results:
                        return ToolResult(
                            ok=True,
                            tool_name=call.name,
                            content="\n".join(results),
                            truncated=True,
                        )
        return ToolResult(ok=True, tool_name=call.name, content="\n".join(results))

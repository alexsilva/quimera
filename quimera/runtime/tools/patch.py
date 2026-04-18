"""Componentes de `quimera.runtime.tools.patch`."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .files import get_staging_root
from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult


class PatchApplyError(Exception):
    """Implementa `PatchApplyError`."""
    pass


@dataclass(slots=True)
class AddFileOp:
    """Implementa `AddFileOp`."""
    path: str
    content: str


@dataclass(slots=True)
class DeleteFileOp:
    """Implementa `DeleteFileOp`."""
    path: str


@dataclass(slots=True)
class UpdateFileOp:
    """Implementa `UpdateFileOp`."""
    path: str
    hunks: list[list[str]]
    move_to: str | None = None


def _join_patch_lines(lines: list[str]) -> str:
    """Executa join patch lines."""
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _parse_patch(patch: str) -> list[AddFileOp | DeleteFileOp | UpdateFileOp]:
    """Interpreta patch."""
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch":
        raise PatchApplyError("Patch deve começar com '*** Begin Patch'")
    if lines[-1] != "*** End Patch":
        raise PatchApplyError("Patch deve terminar com '*** End Patch'")

    ops: list[AddFileOp | DeleteFileOp | UpdateFileOp] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: "):].strip()
            i += 1
            content_lines: list[str] = []
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if not lines[i].startswith("+"):
                    raise PatchApplyError("Add File aceita apenas linhas prefixadas com '+'")
                content_lines.append(lines[i][1:])
                i += 1
            ops.append(AddFileOp(path=path, content=_join_patch_lines(content_lines)))
            continue

        if line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: "):].strip()
            ops.append(DeleteFileOp(path=path))
            i += 1
            continue

        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: "):].strip()
            i += 1
            move_to = None
            if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                move_to = lines[i][len("*** Move to: "):].strip()
                i += 1

            hunks: list[list[str]] = []
            current_hunk: list[str] = []
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                raw = lines[i]
                if raw.startswith("@@"):
                    if current_hunk:
                        hunks.append(current_hunk)
                    current_hunk = []
                elif raw == "*** End of File":
                    pass
                elif raw[:1] in {" ", "+", "-"}:
                    current_hunk.append(raw)
                else:
                    raise PatchApplyError(f"Linha inválida no update: {raw}")
                i += 1
            if current_hunk:
                hunks.append(current_hunk)
            if not hunks:
                raise PatchApplyError(f"Update sem hunks: {path}")
            ops.append(UpdateFileOp(path=path, hunks=hunks, move_to=move_to))
            continue

        raise PatchApplyError(f"Operação de patch inválida: {line}")

    return ops


class PatchTool:
    """Implementa `PatchTool`."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de PatchTool."""
        self.config = config

    def _resolve(self, raw_path: str) -> Path:
        """Resolve resolve."""
        normalized = raw_path.lstrip("/") or "."
        staging = get_staging_root()
        base = staging if staging else self.config.workspace_root
        path = (base / normalized).resolve()
        if not str(path).startswith(str(base)):
            raise PatchApplyError(f"Path fora da workspace: {raw_path}")
        return path

    def _display_path(self, path: Path) -> str:
        """Executa display path."""
        staging = get_staging_root()
        base = staging if staging else self.config.workspace_root
        return str(path.relative_to(base))

    @staticmethod
    def _find_subsequence(haystack: list[str], needle: list[str], start: int) -> int:
        """Executa find subsequence."""
        if not needle:
            return start
        max_start = len(haystack) - len(needle)
        for idx in range(start, max_start + 1):
            if haystack[idx: idx + len(needle)] == needle:
                return idx
        return -1

    def _apply_update(self, path: Path, op: UpdateFileOp) -> Path:
        """Executa apply update."""
        if not path.exists():
            raise PatchApplyError(f"Arquivo não existe para update: {op.path}")

        original_lines = path.read_text(encoding="utf-8").splitlines()
        cursor = 0

        for hunk in op.hunks:
            old_chunk = [line[1:] for line in hunk if line[:1] in {" ", "-"}]
            new_chunk = [line[1:] for line in hunk if line[:1] in {" ", "+"}]
            match_at = self._find_subsequence(original_lines, old_chunk, cursor)
            if match_at < 0:
                raise PatchApplyError(f"Hunk não encontrado em {op.path}")
            original_lines[match_at: match_at + len(old_chunk)] = new_chunk
            cursor = match_at + len(new_chunk)

        target = self._resolve(op.move_to) if op.move_to else path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_join_patch_lines(original_lines), encoding="utf-8")
        if op.move_to and target != path:
            path.unlink()
        return target

    def apply_patch(self, call: ToolCall) -> ToolResult:
        """Executa apply patch."""
        raw_patch = str(call.arguments.get("patch", ""))
        if not raw_patch.strip():
            return ToolResult(ok=False, tool_name=call.name, error="apply_patch requer 'patch'")

        try:
            ops = _parse_patch(raw_patch)
            changed: list[str] = []
            for op in ops:
                if isinstance(op, AddFileOp):
                    path = self._resolve(op.path)
                    if path.exists():
                        raise PatchApplyError(f"Arquivo já existe: {op.path}")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(op.content, encoding="utf-8")
                    changed.append(op.path)
                elif isinstance(op, DeleteFileOp):
                    path = self._resolve(op.path)
                    if not path.exists():
                        raise PatchApplyError(f"Arquivo não existe: {op.path}")
                    path.unlink()
                    changed.append(op.path)
                else:
                    path = self._resolve(op.path)
                    target = self._apply_update(path, op)
                    changed.append(self._display_path(target))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))

        return ToolResult(
            ok=True,
            tool_name=call.name,
            content="Patch aplicado com sucesso.",
            data={"changed_files": changed},
        )

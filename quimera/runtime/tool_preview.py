"""Formatador central de preview operacional e de aprovação para tools.

O contexto ``execution`` é a forma canônica de ``quimera.domain.tool_line``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from quimera.domain.tool_line import SENSITIVE_KEYS, build_tool_line, redact


class ToolPreview:
    """Constrói previews de tools com variação apenas de contexto visual."""

    _SENSITIVE_KEYS: ClassVar[frozenset[str]] = SENSITIVE_KEYS
    _PREVIEW_MAX_LINES = 6
    _PREVIEW_MAX_CHARS = 500
    # Limites de approval (caixa compacta de confirmação).
    _MAX_VALUE_LEN = 80
    _MAX_ITEMS = 4

    @classmethod
    def build(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: str = "execution",
        omit_fields: set[str] | None = None,
    ) -> str:
        context_name = str(context or "execution").strip().lower()
        omitted = {str(item).strip().lower() for item in (omit_fields or set()) if str(item).strip()}
        if context_name == "approval":
            return cls._build_approval(tool_name, arguments, omitted)
        return build_tool_line(tool_name, arguments).to_text()

    @classmethod
    def _build_approval(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        omitted: set[str],
    ) -> str:
        handler = getattr(cls, f"_format_approval_{tool_name}", None)
        if callable(handler):
            return handler(arguments, omitted)
        return cls._format_approval_unknown(tool_name, arguments)

    @classmethod
    def _format_approval_write_file(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        path = args.get("path", "?")
        content = str(args.get("content", ""))
        return cls._render_file_op(path, content)

    @classmethod
    def _format_approval_apply_patch(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        return cls._render_file_op("patch textual", str(args.get("patch", "")))

    @classmethod
    def _format_approval_remove_file(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        path = args.get("path", "?")
        dry_run = args.get("dry_run", True)
        warning = "⚠️  REMOÇÃO" if not dry_run else "🔍 dry-run"
        return f"{warning}  {path}"

    @classmethod
    def _format_approval_run_shell(cls, args: dict[str, Any], omitted: set[str]) -> str:
        cmd = args.get("command", "?")
        lines = []
        if "command" not in omitted and "cmd" not in omitted:
            lines.append(f"   comando: {cmd}")
        if "workdir" in args:
            lines.append(f"   workdir: {args['workdir']}")
        return "\n".join(lines)

    @classmethod
    def _format_approval_exec_command(cls, args: dict[str, Any], omitted: set[str]) -> str:
        cmd = args.get("cmd", "?")
        flags = []
        if args.get("login"):
            flags.append("login")
        if args.get("tty"):
            flags.append("tty")
        if args.get("yield_time_ms"):
            flags.append(f"yield={args['yield_time_ms']}ms")
        lines = []
        if "command" not in omitted and "cmd" not in omitted:
            lines.append(f"   comando: {cmd}")
        if flags:
            lines.append(f"   flags: {', '.join(flags)}")
        if "workdir" in args:
            lines.append(f"   workdir: {args['workdir']}")
        return "\n".join(lines)

    @classmethod
    def _format_approval_write_stdin(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        sid = args.get("session_id", "?")
        chars = str(args.get("chars", ""))
        lines = [f"session: {sid}"]
        if chars:
            lines.append(f"   dados: {cls._truncate(chars, cls._PREVIEW_MAX_CHARS)}")
        if args.get("close_stdin"):
            lines.append("   [fecha stdin]")
        return "\n".join(lines)

    @classmethod
    def _format_approval_close_command_session(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        sid = args.get("session_id", "?")
        terminate = args.get("terminate", False)
        extra = " [terminate]" if terminate else ""
        return f"session: {sid}{extra}"

    @classmethod
    def _format_approval_git_add(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        paths = args.get("paths", ".")
        return f"git add: {cls._format_paths(paths)}"

    @classmethod
    def _format_approval_read_file(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        path = args.get("path", "?")
        return f"path: {path}"

    @classmethod
    def _format_approval_list_files(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        path = args.get("path", "?")
        return f"path: {path}"

    @classmethod
    def _format_approval_grep_search(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        pattern = args.get("pattern", "?")
        path = args.get("path", ".")
        return f"pattern: {pattern!r}\npath: {path}"

    @classmethod
    def _format_approval_web_search(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        query = args.get("query", "?")
        n = args.get("num_results", 5)
        return f"query: {query!r}\nmax_results: {n}"

    @classmethod
    def _format_approval_web_fetch(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        url = args.get("url", "?")
        return f"url: {url}"

    @classmethod
    def _format_approval_unknown(cls, tool_name: str, args: dict[str, Any]) -> str:
        pairs = []
        for key, value in args.items():
            rendered = str(value)
            if str(key).lower() in cls._SENSITIVE_KEYS:
                rendered = cls._redact(value)
            if len(rendered) > cls._MAX_VALUE_LEN:
                rendered = rendered[: cls._MAX_VALUE_LEN] + "…"
            pairs.append(f"  {key}: {rendered}")
        return "\n".join(pairs) if pairs else "sem argumentos"

    @classmethod
    def _render_file_op(cls, path: str, content: str) -> str:
        lines = [f"path: {path}"]
        if content:
            preview = cls._preview(content)
            lines.append(f"   preview ({len(content)} chars):")
            for preview_line in preview:
                lines.append(f"     | {preview_line}")
        return "\n".join(lines)

    @classmethod
    def _preview(cls, text: str) -> list[str]:
        lines = text.splitlines()
        if len(lines) > cls._PREVIEW_MAX_LINES:
            lines = lines[: cls._PREVIEW_MAX_LINES]
            lines.append(f"... [truncado, {cls._PREVIEW_MAX_LINES} linhas exibidas]")
        return [cls._truncate(line, cls._PREVIEW_MAX_CHARS) for line in lines]

    @classmethod
    def _format_paths(cls, value: Any) -> str:
        if isinstance(value, (list, tuple, set)):
            items = [cls._truncate(str(item), cls._MAX_VALUE_LEN) for item in list(value)[: cls._MAX_ITEMS]]
            if len(value) > cls._MAX_ITEMS:
                items.append("...")
            return ", ".join(items)
        return cls._truncate(str(value), cls._MAX_VALUE_LEN)

    @staticmethod
    def _truncate(value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[:max_len] + "…"

    _redact = staticmethod(redact)


class ToolPreviewSummary(ToolPreview):
    """Alias compatível para migração gradual."""

    pass

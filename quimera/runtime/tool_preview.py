"""Formatador central de preview operacional e de aprovação para tools."""

from __future__ import annotations

from typing import Any


class ToolPreview:
    """Constrói previews de tools com variação apenas de contexto visual."""

    _SENSITIVE_KEYS = {
        "token", "api_key", "password", "secret",
        "authorization", "cookie", "headers",
    }
    _PREVIEW_MAX_LINES = 6
    _PREVIEW_MAX_CHARS = 500
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
        return cls._build_execution(tool_name, arguments)

    @classmethod
    def _build_execution(cls, tool_name: str, arguments: dict[str, Any]) -> str:
        handler = getattr(cls, f"_format_execution_{tool_name}", None)
        if callable(handler):
            return handler(arguments)
        return cls._format_execution_unknown(tool_name, arguments)

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
    def _format_execution_read_file(cls, args: dict[str, Any]) -> str:
        path = args.get("path", "?")
        return f"⚒ read_file {cls._truncate(str(path), cls._MAX_VALUE_LEN)}"

    @classmethod
    def _format_execution_list_files(cls, args: dict[str, Any]) -> str:
        path = args.get("path") or args.get("directory") or "?"
        return f"⚒ list_files {cls._truncate(str(path), cls._MAX_VALUE_LEN)}"

    @classmethod
    def _format_execution_grep_search(cls, args: dict[str, Any]) -> str:
        pattern = args.get("pattern", "?")
        path = args.get("path") or args.get("root") or "."
        return f"⚒ grep {pattern!r} {cls._truncate(str(path), cls._MAX_VALUE_LEN)}"

    @classmethod
    def _format_execution_web_search(cls, args: dict[str, Any]) -> str:
        query = args.get("query") or args.get("q") or "?"
        return f"⚒ web_search {query!r}"

    @classmethod
    def _format_execution_web_fetch(cls, args: dict[str, Any]) -> str:
        url = args.get("url", "?")
        return f"⚒ web_fetch {cls._truncate(str(url), cls._MAX_VALUE_LEN)}"

    @classmethod
    def _format_execution_run_shell(cls, args: dict[str, Any]) -> str:
        cmd = args.get("command") or args.get("cmd") or "?"
        return f"⚒ $ {cls._truncate(str(cmd), 120)}"

    @classmethod
    def _format_execution_exec_command(cls, args: dict[str, Any]) -> str:
        cmd = args.get("cmd", "?")
        return f"⚒ $ {cls._truncate(str(cmd), 120)}"

    @classmethod
    def _format_execution_write_stdin(cls, args: dict[str, Any]) -> str:
        sid = args.get("session_id", "?")
        chars = str(args.get("chars", ""))
        line = f"⚒ write_stdin session={sid}"
        if chars:
            line += f" {cls._truncate(chars, 80)!r}"
        if args.get("close_stdin"):
            line += " [close]"
        return line

    @classmethod
    def _format_execution_close_command_session(cls, args: dict[str, Any]) -> str:
        sid = args.get("session_id", "?")
        terminate = args.get("terminate", False)
        extra = " [terminate]" if terminate else ""
        return f"⚒ close_session {sid}{extra}"

    @classmethod
    def _format_execution_unknown(cls, tool_name: str, args: dict[str, Any]) -> str:
        parts = []
        for index, (key, value) in enumerate(args.items()):
            if index >= cls._MAX_ITEMS:
                parts.append("...")
                break
            parts.append(f"{cls._sanitize_value(key, value)}")
        suffix = f" {' '.join(parts)}" if parts else ""
        return f"⚒ {tool_name}{suffix}"

    @classmethod
    def _format_approval_write_file(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        path = args.get("path", "?")
        content = str(args.get("content", ""))
        return cls._render_file_op("write_file", path, content)

    @classmethod
    def _format_approval_apply_patch(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        return cls._render_file_op("apply_patch", "patch textual", str(args.get("patch", "")))

    @classmethod
    def _format_approval_remove_file(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        path = args.get("path", "?")
        dry_run = args.get("dry_run", True)
        warning = "⚠️  REMOÇÃO" if not dry_run else "🔍 dry-run"
        return f"{warning}  {path}"

    @classmethod
    def _format_approval_run_shell(cls, args: dict[str, Any], omitted: set[str]) -> str:
        cmd = args.get("command", "?")
        lines = [f"🖥️  run_shell"]
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
        lines = [f"🖥️  exec_command"]
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
        lines = [f"⌨️  write_stdin  session={sid}"]
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
        return f"❌ close_command_session  session={sid}{extra}"

    @classmethod
    def _format_approval_read_file(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        path = args.get("path", "?")
        return f"📖 read_file  {path}"

    @classmethod
    def _format_approval_list_files(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        path = args.get("path", "?")
        return f"📂 list_files  {path}"

    @classmethod
    def _format_approval_grep_search(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        pattern = args.get("pattern", "?")
        path = args.get("path", ".")
        return f"🔍 grep_search  pattern={pattern!r}  em {path}"

    @classmethod
    def _format_approval_web_search(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        query = args.get("query", "?")
        n = args.get("num_results", 5)
        return f"🌐 web_search  query={query!r}  (max {n} resultados)"

    @classmethod
    def _format_approval_web_fetch(cls, args: dict[str, Any], _omitted: set[str]) -> str:
        url = args.get("url", "?")
        return f"🌐 web_fetch  {url}"

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
        return f"{tool_name}\n" + "\n".join(pairs) if pairs else tool_name

    @classmethod
    def _render_file_op(cls, op: str, path: str, content: str) -> str:
        lines = [f"📝 {op}", f"   path: {path}"]
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
    def _sanitize_value(cls, key: str, value: Any) -> str:
        lowered = str(key).lower()
        if lowered in cls._SENSITIVE_KEYS:
            return cls._redact(value)
        if isinstance(value, dict):
            inner = []
            for index, (inner_key, inner_value) in enumerate(value.items()):
                if index >= cls._MAX_ITEMS:
                    inner.append("...")
                    break
                inner.append(f"{inner_key}={cls._sanitize_value(inner_key, inner_value)}")
            return cls._truncate("{" + ", ".join(inner) + "}", cls._MAX_VALUE_LEN)
        if isinstance(value, (list, tuple, set)):
            items = [cls._sanitize_value(key, item) for item in list(value)[: cls._MAX_ITEMS]]
            if len(value) > cls._MAX_ITEMS:
                items.append("...")
            return cls._truncate("[" + ", ".join(items) + "]", cls._MAX_VALUE_LEN)
        return cls._truncate(str(value), cls._MAX_VALUE_LEN)

    @staticmethod
    def _truncate(value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[:max_len] + "…"

    @staticmethod
    def _redact(value: Any) -> str:
        text = str(value)
        if not text:
            return "***"
        if len(text) <= 4:
            return "****"
        return text[:2] + "****" + text[-2:]


class ToolPreviewSummary(ToolPreview):
    """Alias compatível para migração gradual."""

    pass

"""
Formata um resumo legível para aprovação de ferramentas.

Cada ferramenta ganha um layout específico:
- write_file / apply_patch: path, preview (primeiras linhas), total de bytes/chars
- run_shell: comando + working dir
- exec_command: comando + flags (login, tty, yield_time_ms)
- remove_file: path com destaque de perigo
- read_file / list_files / grep_search / web_search / web_fetch: compacto (leitura)
- write_stdin / close_command_session: session_id + extras
- ferramentas desconhecidas: fallback limpo de chaves/valores
"""

from __future__ import annotations

from typing import Any, Dict


class ApproveSummary:
    """Constrói um summary multilinha, truncado e rico para o approval handler."""

    # ------------------------------------------------------------------
    # Constantes
    # ------------------------------------------------------------------
    _PREVIEW_MAX_LINES = 6
    _PREVIEW_MAX_CHARS = 500
    _MAX_VALUE_LEN = 80

    # ------------------------------------------------------------------
    # Ponto de entrada único
    # ------------------------------------------------------------------
    @classmethod
    def build(cls, tool_name: str, arguments: Dict[str, Any]) -> str:
        handler = getattr(cls, f"_format_{tool_name}", None)
        if callable(handler):
            return handler(arguments)
        return cls._format_unknown(tool_name, arguments)

    # ------------------------------------------------------------------
    # Ferramentas de escrita
    # ------------------------------------------------------------------
    @classmethod
    def _format_write_file(cls, args: Dict[str, Any]) -> str:
        path = args.get("path", "?")
        content = str(args.get("content", ""))
        return cls._render_file_op("write_file", path, content)

    @classmethod
    def _format_apply_patch(cls, args: Dict[str, Any]) -> str:
        path = "patch textual"  # apply_patch não tem path explícito
        content = str(args.get("patch", ""))
        return cls._render_file_op("apply_patch", path, content)

    @classmethod
    def _format_remove_file(cls, args: Dict[str, Any]) -> str:
        path = args.get("path", "?")
        dry_run = args.get("dry_run", True)
        warning = "⚠️  REMOÇÃO" if not dry_run else "🔍 dry-run"
        return f"{warning}  {path}"

    # ------------------------------------------------------------------
    # Ferramentas de shell
    # ------------------------------------------------------------------
    @classmethod
    def _format_run_shell(cls, args: Dict[str, Any]) -> str:
        cmd = args.get("command", "?")
        lines = [f"🖥️  run_shell", f"   comando: {cmd}"]
        return "\n".join(lines)

    @classmethod
    def _format_exec_command(cls, args: Dict[str, Any]) -> str:
        cmd = args.get("cmd", "?")
        flags = []
        if args.get("login"):
            flags.append("login")
        if args.get("tty"):
            flags.append("tty")
        if args.get("yield_time_ms"):
            flags.append(f"yield={args['yield_time_ms']}ms")
        lines = [f"🖥️  exec_command", f"   comando: {cmd}"]
        if flags:
            lines.append(f"   flags: {', '.join(flags)}")
        if "workdir" in args:
            lines.append(f"   workdir: {args['workdir']}")
        return "\n".join(lines)

    @classmethod
    def _format_write_stdin(cls, args: Dict[str, Any]) -> str:
        sid = args.get("session_id", "?")
        chars = str(args.get("chars", ""))
        lines = [f"⌨️  write_stdin  session={sid}"]
        if chars:
            lines.append(f"   dados: {cls._truncate(chars, cls._PREVIEW_MAX_CHARS)}")
        if args.get("close_stdin"):
            lines.append(f"   [fecha stdin]")
        return "\n".join(lines)

    @classmethod
    def _format_close_command_session(cls, args: Dict[str, Any]) -> str:
        sid = args.get("session_id", "?")
        terminate = args.get("terminate", False)
        extra = " [terminate]" if terminate else ""
        return f"❌ close_command_session  session={sid}{extra}"

    # ------------------------------------------------------------------
    # Ferramentas de leitura / pesquisa (compacto)
    # ------------------------------------------------------------------
    @classmethod
    def _format_read_file(cls, args: Dict[str, Any]) -> str:
        path = args.get("path", "?")
        return f"📖 read_file  {path}"

    @classmethod
    def _format_list_files(cls, args: Dict[str, Any]) -> str:
        path = args.get("path", "?")
        return f"📂 list_files  {path}"

    @classmethod
    def _format_grep_search(cls, args: Dict[str, Any]) -> str:
        pattern = args.get("pattern", "?")
        path = args.get("path", ".")
        return f"🔍 grep_search  pattern={pattern!r}  em {path}"

    @classmethod
    def _format_web_search(cls, args: Dict[str, Any]) -> str:
        query = args.get("query", "?")
        n = args.get("num_results", 5)
        return f"🌐 web_search  query={query!r}  (max {n} resultados)"

    @classmethod
    def _format_web_fetch(cls, args: Dict[str, Any]) -> str:
        url = args.get("url", "?")
        return f"🌐 web_fetch  {url}"

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------
    @classmethod
    def _format_unknown(cls, tool_name: str, args: Dict[str, Any]) -> str:
        pares = []
        for k, v in args.items():
            s = str(v)
            if len(s) > cls._MAX_VALUE_LEN:
                s = s[: cls._MAX_VALUE_LEN] + "…"
            pares.append(f"  {k}: {s}")
        return f"{tool_name}\n" + "\n".join(pares) if pares else tool_name

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------
    @classmethod
    def _render_file_op(cls, op: str, path: str, content: str) -> str:
        lines = [f"📝 {op}", f"   path: {path}"]
        if content:
            preview = cls._preview(content)
            lines.append(f"   preview ({len(content)} chars):")
            for pline in preview:
                lines.append(f"     | {pline}")
        return "\n".join(lines)

    @classmethod
    def _preview(cls, text: str) -> list:
        linhas = text.splitlines()
        if len(linhas) > cls._PREVIEW_MAX_LINES:
            linhas = linhas[: cls._PREVIEW_MAX_LINES]
            linhas.append(f"... [truncado, {cls._PREVIEW_MAX_LINES} linhas exibidas]")
        result = []
        for linha in linhas:
            result.append(cls._truncate(linha, cls._PREVIEW_MAX_CHARS))
        return result

    @staticmethod
    def _truncate(s: str, max_len: int) -> str:
        if len(s) <= max_len:
            return s
        return s[:max_len] + "…"

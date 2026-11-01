"""Linha estruturada de tool: o contrato entre quem chama a tool e o feed.

Os produtores (perfis dos CLIs, presenter, cliente MCP) descrevem a chamada
como dados — tool, categoria, status, verbo, assunto, título, duração — e o
consumidor monta o texto na largura real. ``ToolLine.to_text`` é a forma
canônica para o modo pipe/plain e para rótulos de status.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from quimera.domain.tool_activity import (
    COMMAND_TOOLS,
    ToolActivityCategory,
    classify_tool_activity,
    coerce_tool_activity,
    command_from_payload,
    normalize_tool_name,
    unwrap_shell_command,
)
from quimera.ui.messages import format_duration

# Um glifo por categoria; todos de 1 célula em toda tabela Unicode do Rich e
# sem apresentação emoji (ver tests/test_tool_line.py).
CATEGORY_GLYPHS: dict[str, str] = {
    ToolActivityCategory.INSPECTION.value: "⌕",
    ToolActivityCategory.MODIFICATION.value: "✎",
    ToolActivityCategory.VALIDATION.value: "⚙",
    ToolActivityCategory.VERSION_CONTROL.value: "⎇",
    ToolActivityCategory.RESEARCH.value: "◎",
    ToolActivityCategory.DELEGATION.value: "⇄",
    ToolActivityCategory.EXECUTION.value: "$",
}

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ERROR = "error"

TRANSPORT_MCP_HTTP = "mcp_http"
MCP_HTTP_MARKER = "◇"
STATUS_MARKS = {STATUS_OK: "✓", STATUS_ERROR: "✗"}

SENSITIVE_KEYS = frozenset({
    "token", "api_key", "password", "secret",
    "authorization", "cookie", "headers",
})
# Partes (separadas por ``_``) que tornam a chave sensível em qualquer posição:
# ``access_token``, ``client_secret``, ``X-Auth-Token``.
# ``tokens`` fica de fora de propósito: ``max_tokens`` é parâmetro comum.
_SENSITIVE_KEY_PARTS = frozenset({
    "token", "password", "passwords", "passwd", "pwd", "passphrase",
    "secret", "secrets", "authorization", "cookie", "cookies", "headers",
    "bearer", "credential", "credentials", "jwt", "dsn",
})
_SENSITIVE_KEY_SUFFIXES = (
    "api_key", "apikey", "private_key", "access_key", "secret_key",
    "signing_key", "encryption_key", "ssh_key", "client_key",
    "database_url", "connection_string",
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_OK_ALIASES = frozenset({"ok", "success", "succeeded", "finished", "completed", "complete", "done"})
_ERROR_ALIASES = frozenset({"error", "failed", "failure", "fail", "timeout", "cancelled", "aborted"})

# Teto de proteção contra payloads patológicos; a UI dobra a linha.
_SUBJECT_MAX = 400
_TITLE_MAX = 160
_TAIL_MAX = 160
_DELEGATION_REQUEST_MAX = 80
_QUESTION_MAX = 120
_STDIN_CHARS_MAX = 200
_UNKNOWN_MAX_VALUES = 3
_UNKNOWN_VALUE_MAX = 80

_READ_TOOLS = frozenset({"read", "read_file", "view", "cat", "readfile"})
_WRITE_TOOLS = frozenset({"write", "write_file", "create", "create_file", "overwrite", "writefile"})
_EDIT_TOOLS = frozenset({
    "edit", "edit_file", "multiedit", "str_replace_editor", "str_replace_based_edit_tool",
    "editfile", "notebookedit", "apply_patch", "replace_text",
})
_REMOVE_TOOLS = frozenset({"remove_file", "rm"})
_SEARCH_TOOLS = frozenset({"grep", "grep_search", "search", "search_files", "find_in_files", "rg"})
_LIST_TOOLS = frozenset({"glob", "find_files", "list_files", "ls"})
_WEB_SEARCH_TOOLS = frozenset({"websearch", "web_search", "search_web", "brave_search", "google"})
_URL_TOOLS = frozenset({"webfetch", "web_fetch", "fetch", "http_get", "curl", "http_request", "browser_navigate"})
_GIT_TOOLS = frozenset({
    "git_status", "git_diff", "git_log", "git_branch", "git_add",
    "git_commit", "git_checkout", "git_push", "git_fetch",
})
_CLI_DELEGATION_TOOLS = frozenset({"task", "agent", "delegate"})

_PATH_KEYS = ("file_path", "path", "filename")
_UNKNOWN_KEYS = (
    "path", "file_path", "command", "cmd", "query", "pattern", "url", "name",
    "task", "request", "description", "id", "session_id",
)


@dataclass(frozen=True)
class ToolLine:
    """Uma chamada de tool como dados; o texto sai de ``to_text``."""

    tool: str
    category: str
    status: str
    verb: str
    subject: str
    title: str
    tool_call_id: str
    duration_ms: int | None
    tail: str
    transport: str
    preview_lines: tuple[str, ...] = ()

    def to_text(self) -> str:
        parts: list[str] = []
        if self.transport == TRANSPORT_MCP_HTTP:
            parts.append(MCP_HTTP_MARKER)
        mark = STATUS_MARKS.get(self.status)
        if mark:
            parts.append(mark)
        parts.append(CATEGORY_GLYPHS.get(self.category, CATEGORY_GLYPHS[ToolActivityCategory.EXECUTION.value]))
        body = f"{self.verb} {self.subject}".strip() or self.tool
        if body:
            parts.append(body)
        text = " ".join(parts)
        if self.duration_ms is not None and self.status != STATUS_RUNNING:
            text += f" · {format_duration(self.duration_ms)}"
        if self.tail and (
            self.status == STATUS_ERROR or self.category == ToolActivityCategory.VALIDATION.value
        ):
            text += f" · {self.tail}"
        return text

    def to_payload(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "category": self.category,
            "status": self.status,
            "verb": self.verb,
            "subject": self.subject,
            "title": self.title,
            "tool_call_id": self.tool_call_id,
            "duration_ms": self.duration_ms,
            "tail": self.tail,
            "transport": self.transport,
            "preview_lines": list(self.preview_lines),
            "text": self.to_text(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> ToolLine | None:
        if not isinstance(payload, Mapping):
            return None
        if "tool" not in payload:
            if "text" not in payload:
                return None
            # Entrada legada: o texto pronto vira o assunto, sem interpretar
            # glifos ou prefixos que o produtor antigo tenha posto nele.
            return cls(
                tool="",
                category=ToolActivityCategory.EXECUTION.value,
                status=STATUS_RUNNING,
                verb="",
                subject=_text(payload.get("text")),
                title="",
                tool_call_id="",
                duration_ms=None,
                tail="",
                transport="",
            )
        tool = normalize_tool_name(payload.get("tool"))
        category = coerce_tool_activity(payload.get("category")) or classify_tool_activity(tool)
        raw_preview = payload.get("preview_lines")
        preview = tuple(str(line) for line in raw_preview) if isinstance(raw_preview, (list, tuple)) else ()
        return cls(
            tool=tool,
            category=category.value,
            status=normalize_status(payload.get("status")),
            verb=_text(payload.get("verb")),
            subject=_text(payload.get("subject")),
            title=_text(payload.get("title")),
            tool_call_id=_text(payload.get("tool_call_id")),
            duration_ms=coerce_duration_ms(payload.get("duration_ms")),
            tail=_text(payload.get("tail")),
            transport=_text(payload.get("transport")),
            preview_lines=preview,
        )

    def with_outcome(
        self,
        *,
        status: str,
        duration_ms: int | None = None,
        tail: str | None = None,
    ) -> ToolLine:
        return replace(
            self,
            status=normalize_status(status),
            duration_ms=coerce_duration_ms(duration_ms),
            tail=self.tail if tail is None else _tail_line(tail),
        )


def build_tool_line(
    tool_name: object,
    arguments: Mapping[str, Any] | None = None,
    *,
    status: str = STATUS_RUNNING,
    tool_call_id: object = None,
    description: object = None,
    duration_ms: int | None = None,
    tail: object = None,
    exit_code: int | None = None,
    error: object = None,
    transport: str = "",
    explicit_category: object = None,
    preview_lines: Iterable[str] = (),
) -> ToolLine:
    args: Mapping[str, Any] = arguments if isinstance(arguments, Mapping) else {}
    tool = normalize_tool_name(tool_name)
    category = classify_tool_activity(tool_name, args, explicit=explicit_category)
    verb, subject = _verb_and_subject(tool, args)
    # O Bash do Claude Code manda a intenção em ``description``: vira ``title``,
    # complemento do comando (que segue sendo o corpo). Nas demais famílias
    # ``description`` é o próprio assunto (ex.: Task) e não vira título.
    if description is None and tool in COMMAND_TOOLS:
        description = args.get("description")
    return ToolLine(
        tool=tool,
        category=category.value,
        status=normalize_status(status),
        verb=verb,
        subject=_clip(_one_line(subject), _SUBJECT_MAX),
        title=_clip(_one_line(description), _TITLE_MAX),
        tool_call_id=_text(tool_call_id),
        duration_ms=coerce_duration_ms(duration_ms),
        tail=_resolve_tail(tail, error, exit_code),
        transport=str(transport or ""),
        preview_lines=tuple(str(line) for line in preview_lines),
    )


def normalize_status(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _OK_ALIASES:
        return STATUS_OK
    if normalized in _ERROR_ALIASES:
        return STATUS_ERROR
    return STATUS_RUNNING


def tool_call_namespace(tool_call_id: object) -> str:
    """Origem do id: ``mcp`` em ``mcp:run:5``, ``toolu`` em ``toolu_01``.

    A mesma chamada física chega com um id do CLI e outro do executor MCP;
    ids do mesmo namespace são sempre chamadas distintas.
    """
    text = _text(tool_call_id).strip()
    if ":" in text:
        return text.split(":", 1)[0]
    head = text.split("_", 1)[0] if "_" in text else ""
    return head if head.isalpha() else ""


def is_sensitive_key(key: object) -> bool:
    """Chave cujo valor não pode aparecer: camelCase, caixa e ``-``/``_`` são indiferentes."""
    normalized = _CAMEL_BOUNDARY.sub("_", str(key)).replace("-", "_").lower()
    if normalized in SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES):
        return True
    # ``password1``: campos numerados contam como a parte sem o número.
    return any(part.rstrip("0123456789") in _SENSITIVE_KEY_PARTS for part in normalized.split("_"))


def redact(value: Any) -> str:
    text = str(value)
    if not text:
        return "***"
    if len(text) <= 4:
        return "****"
    return text[:2] + "****" + text[-2:]


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _one_line(value: object) -> str:
    return " ".join(_text(value).split())


def _clip(value: str, max_len: int) -> str:
    return value if len(value) <= max_len else value[:max_len] + "…"


def coerce_duration_ms(value: object) -> int | None:
    """Duração em ms válida; ``bool`` é ``int`` em Python e não é duração."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _tail_line(value: object) -> str:
    """Última linha não vazia da saída, em linha única."""
    lines = [line for line in _text(value).splitlines() if line.strip()]
    return _clip(_one_line(lines[-1]), _TAIL_MAX) if lines else ""


def _resolve_tail(tail: object, error: object, exit_code: int | None) -> str:
    explicit = _tail_line(tail)
    if explicit:
        return explicit
    message = error.get("message") if isinstance(error, Mapping) else error
    error_text = _clip(_one_line(message), _TAIL_MAX)
    if error_text:
        return error_text
    if exit_code is not None and exit_code != 0:
        return f"exit {exit_code}"
    return ""


def _scalar(value: object) -> bool:
    return isinstance(value, (str, int, float, bool)) and _text(value).strip() != ""


def _first(args: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = args.get(key)
        if _scalar(value):
            return str(value)
    return ""


def _quoted(value: str) -> str:
    return f'"{_one_line(value)}"'


def _first_line(value: object, max_len: int) -> str:
    for line in _text(value).splitlines():
        if line.strip():
            return _clip(_one_line(line), max_len)
    return ""


def _unknown_subject(args: Mapping[str, Any]) -> str:
    """Até ``_UNKNOWN_MAX_VALUES`` valores escalares, chaves conhecidas primeiro."""
    scalar_keys = [key for key, value in args.items() if _scalar(value)]
    preferred = [key for key in _UNKNOWN_KEYS if key in scalar_keys]
    ordered = preferred + [key for key in scalar_keys if key not in preferred]
    shown = [
        redact(args[key]) if is_sensitive_key(key) else _clip(_one_line(args[key]), _UNKNOWN_VALUE_MAX)
        for key in ordered[:_UNKNOWN_MAX_VALUES]
    ]
    remaining = len(args) - len(shown)
    if remaining:
        shown.append(f"+{remaining} args")
    return " ".join(shown)


def _paths(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value if _scalar(item))
    return str(value) if _scalar(value) else ""


def _git_subject(tool: str, args: Mapping[str, Any]) -> str:
    subcommand = tool[len("git_"):]
    extra: list[str] = []
    if subcommand == "add":
        extra.append(_paths(args.get("paths")))
    elif subcommand == "commit":
        message = _first_line(args.get("message"), _DELEGATION_REQUEST_MAX)
        if message:
            extra.append(_quoted(message))
    elif subcommand == "diff":
        extra.extend(_first(args, (key,)) for key in ("ref1", "ref2", "path"))
    elif subcommand in {"log", "checkout"}:
        extra.append(_first(args, ("branch",)))
    elif subcommand == "push":
        extra.extend(_first(args, (key,)) for key in ("remote", "branch"))
    elif subcommand == "fetch":
        extra.append(_first(args, ("remote",)))
    return " ".join([subcommand, *(item for item in extra if item)])


def _family_subject(tool: str, args: Mapping[str, Any]) -> tuple[str, str] | None:
    """Verbo e assunto da família da tool; ``None`` quando cabe a regra genérica."""
    if tool in COMMAND_TOOLS:
        command = command_from_payload(args)
        return ("", unwrap_shell_command(command)) if command else None
    if tool == "write_stdin":
        line = f"session={args.get('session_id', '?')}"
        chars = _text(args.get("chars"))
        if chars:
            line += f" {_clip(chars, _STDIN_CHARS_MAX)!r}"
        if args.get("close_stdin"):
            line += " [close]"
        return "", line
    if tool == "close_command_session":
        extra = " [terminate]" if args.get("terminate") else ""
        return "", f"session={args.get('session_id', '?')}{extra}"
    if tool in _READ_TOOLS:
        path = _first(args, _PATH_KEYS)
        return ("ler", path) if path else None
    if tool in _WRITE_TOOLS:
        path = _first(args, _PATH_KEYS)
        return ("escrever", path) if path else None
    if tool in _EDIT_TOOLS:
        path = _first(args, (*_PATH_KEYS, "notebook_path"))
        if path or tool == "apply_patch":
            return "editar", path
        return None
    if tool in _REMOVE_TOOLS:
        path = _first(args, _PATH_KEYS)
        return ("remover", path) if path else None
    if tool in _SEARCH_TOOLS:
        pattern = _first(args, ("pattern",))
        if not pattern:
            return None
        where = _first(args, ("path", "directory", "root"))
        return "buscar", _quoted(pattern) + (f" em {where}" if where else "")
    if tool in _LIST_TOOLS:
        target = _first(args, ("pattern", "glob", "path", "directory"))
        return ("listar", target) if target else None
    if tool in _WEB_SEARCH_TOOLS:
        query = _first(args, ("query", "q"))
        return ("pesquisar", _quoted(query)) if query else None
    if tool in _URL_TOOLS:
        url = _first(args, ("url", "uri"))
        return ("abrir", url) if url else None
    if tool in _GIT_TOOLS:
        return "git", _git_subject(tool, args)
    if tool == "tasks_delegate":
        target = _first(args, ("target_agent",))
        if not target:
            return None
        request = _first_line(args.get("request"), _DELEGATION_REQUEST_MAX)
        return "delegar", target + (f" · {request}" if request else "")
    if tool == "tasks_create":
        what = _first(args, ("description", "title", "task"))
        return ("delegar", what) if what else None
    if tool in {"workflows_start", "workflows_replan"}:
        workflow = _first(args, ("workflow_id", "name"))
        return ("delegar", workflow) if workflow else None
    if tool in _CLI_DELEGATION_TOOLS:
        what = _first(args, ("description", "prompt", "subagent_type"))
        return ("delegar", _first_line(what, _DELEGATION_REQUEST_MAX)) if what else None
    if tool == "ask_user":
        question = _first(args, ("question",))
        return ("perguntar", _clip(_one_line(question), _QUESTION_MAX)) if question else None
    return None


def _verb_and_subject(tool: str, args: Mapping[str, Any]) -> tuple[str, str]:
    family = _family_subject(tool, args)
    if family is not None:
        return family
    # Sem família, o nome da tool é o verbo: sem ele a linha vira só valores
    # soltos ("select:x +1 args") e não diz o que rodou.
    return tool, _unknown_subject(args)

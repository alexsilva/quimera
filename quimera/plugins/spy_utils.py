"""Helpers compartilhados para formatação de eventos spy."""

from quimera.agent_events import SpyEvent


def truncate_spy_text(value: str, limit: int = 160) -> str:
    """Normaliza texto em linha única com limite de tamanho."""
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def format_agent_message_lines(text: str) -> list[SpyEvent]:
    """Quebra mensagens multi-linha em eventos simples e preserva `clear`."""
    messages: list[SpyEvent] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower() == "clear":
            messages.append(SpyEvent(kind="clear", text="", transient=True))
            continue
        messages.append(SpyEvent(kind="response", text=truncate_spy_text(line), final=True))
    return messages


def is_diff_command(command: str) -> bool:
    """Identifica comandos cujo output é útil como preview de diff."""
    command = (command or "").strip().lower()
    return command.startswith(("git diff", "git show", "diff "))


def format_command_output_preview(
    command: str,
    output: str,
    limit: int = 20,
    tool_call_id: str | None = None,
) -> list[SpyEvent]:
    """Renderiza preview truncado de comandos de diff."""
    if not is_diff_command(command):
        return []

    events: list[SpyEvent] = []
    lines = [line.rstrip() for line in (output or "").splitlines() if line.strip()]
    for idx, line in enumerate(lines[:limit]):
        events.append(
            SpyEvent(
                kind="diff",
                text=truncate_spy_text(line, limit=240),
                final=True,
                data={
                    "tool": "exec_command",
                    "tool_call_id": tool_call_id,
                    "operation": "preview",
                    "line_index": idx,
                },
            )
        )
    if len(lines) > limit:
        events.append(
            SpyEvent(
                kind="diff",
                text=f"... diff truncado ({len(lines) - limit} linhas omitidas)",
                final=True,
                data={
                    "tool": "exec_command",
                    "tool_call_id": tool_call_id,
                    "operation": "preview_truncated",
                    "omitted_lines": len(lines) - limit,
                },
            )
        )
    return events

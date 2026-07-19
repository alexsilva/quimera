"""Helpers compartilhados para formatação de eventos spy."""

from quimera.agent_events import SpyEvent


def normalize_spy_text(value: str) -> str:
    """Normaliza texto em linha única, preservando todo o conteúdo."""
    return " ".join((value or "").split())


def truncate_spy_text(value: str, limit: int = 400) -> str:
    """Normaliza texto em linha única com limite de tamanho."""
    value = normalize_spy_text(value)
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
        # Mensagens intermediárias do agente devem atualizar no mesmo bloco visual
        # sem truncar o conteúdo (apenas normalização).
        messages.append(SpyEvent(kind="response", text=normalize_spy_text(line), transient=True))
    return messages


def describe_tool_input(tool_name: str, inp: dict) -> str:
    """Retorna descrição concisa do input de uma ferramenta para exibição no spy."""
    name = (tool_name or "").strip().lower()
    inp = inp or {}

    # Ferramentas de shell/exec
    if name in {"bash", "run", "execute", "exec", "shell", "computer"}:
        cmd = inp.get("command") or inp.get("cmd") or inp.get("input") or ""
        if cmd:
            return f"$ {truncate_spy_text(str(cmd))}"

    # Ferramentas de leitura de arquivo
    if name in {"read", "read_file", "view", "cat", "readfile"}:
        path = inp.get("file_path") or inp.get("path") or inp.get("filename") or ""
        if path:
            return truncate_spy_text(str(path))

    # Ferramentas de escrita/edição
    if name in {"write", "write_file", "edit", "edit_file", "str_replace_editor",
                "str_replace_based_edit_tool",
                "create", "create_file", "overwrite", "writefile", "editfile"}:
        path = inp.get("file_path") or inp.get("path") or inp.get("filename") or ""
        if path:
            return f"editar {truncate_spy_text(str(path))}"

    # Ferramentas de busca em arquivos
    if name in {"grep", "search", "search_files", "find_in_files", "rg"}:
        pattern = inp.get("pattern") or inp.get("query") or inp.get("regex") or ""
        path = inp.get("path") or inp.get("directory") or ""
        if pattern:
            loc = f" em {truncate_spy_text(str(path), limit=200)}" if path else ""
            return f'buscar "{truncate_spy_text(str(pattern), limit=240)}"{loc}'

    # Ferramentas de listagem de arquivos
    if name in {"glob", "find_files", "list_files", "ls"}:
        pattern = inp.get("pattern") or inp.get("glob") or inp.get("path") or ""
        if pattern:
            return f"listar {truncate_spy_text(str(pattern))}"

    # Ferramentas web
    if name in {"websearch", "web_search", "search_web", "brave_search", "google"}:
        query = inp.get("query") or inp.get("q") or ""
        if query:
            return f'pesquisar "{truncate_spy_text(str(query))}"'

    if name in {"webfetch", "web_fetch", "fetch", "http_get", "curl"}:
        url = inp.get("url") or inp.get("uri") or ""
        if url:
            return f"fetch {truncate_spy_text(str(url))}"

    return ""


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

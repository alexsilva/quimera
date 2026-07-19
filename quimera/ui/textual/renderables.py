"""Renderables e helpers visuais da interface Textual."""
# ruff: noqa: E402
from __future__ import annotations

import re
from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

_DEFAULT_SECTION_LINE_LIMIT = 20

import quimera.themes as themes
from quimera.ui.textual.constants import (
    APPROVAL_OPTIONS as _APPROVAL_OPTIONS,
    APPROVAL_TITLE as _APPROVAL_TITLE,
)
from quimera.ui.branding import banner_gradient_text
from quimera.ui.textual.events import TextualUiEvent
from quimera.clipboard_support import ClipboardManager

_clipboard_manager = ClipboardManager()

_RICH_MARKUP_TAG_RE = re.compile(r"\[/?[a-zA-Z][a-zA-Z0-9_#= .:-]*\]")
_TOOL_PREVIEW_LINE_LIMIT = 8
_THINKING_MARKER = "✻"
_THINKING_PULSE_FRAMES = ("✻", "✽", "✳", "✢", "·", "✢", "✳", "✽")
_thinking_pulse_index = 0
_DELEGATION_HINT_RE = re.compile(
    r"(\bdelegate\b|\bdelegar\b|\bdelegação\b|\bdelegacao\b|->|→|=>)",
    re.IGNORECASE,
)


def advance_thinking_pulse() -> None:
    """Avança um frame da animação do marcador de pensamento."""
    global _thinking_pulse_index
    _thinking_pulse_index = (_thinking_pulse_index + 1) % len(_THINKING_PULSE_FRAMES)


def reset_thinking_pulse() -> None:
    """Volta o marcador de pensamento ao frame base (``✻``)."""
    global _thinking_pulse_index
    _thinking_pulse_index = 0


def _thinking_pulse_marker() -> str:
    """Retorna o glifo atual do marcador de pensamento pulsante."""
    return _THINKING_PULSE_FRAMES[_thinking_pulse_index]


def _approval_options() -> list[str]:
    """Retorna as opções visuais padrão para confirmação de permissão."""
    return list(_APPROVAL_OPTIONS)


def _strip_rich_markup_tags(value: str) -> str:
    """Remove tags Rich inline que chegam como texto de eventos transitórios."""
    return _RICH_MARKUP_TAG_RE.sub("", str(value or ""))


def _styled_tool_line(line: str, style: str) -> Text:
    """Estiliza uma linha de tool com ícone de status, sem cortar conteúdo."""
    stripped = str(line or "").strip()
    rendered = Text(no_wrap=False, overflow="fold")
    rendered.append("  ")
    if stripped.startswith("✓ "):
        rendered.append("✓ ", style="bold green")
        rendered.append(stripped[2:], style="dim")
        return rendered
    if stripped.startswith("✗ "):
        rendered.append("✗ ", style="bold red")
        rendered.append(stripped[2:], style="red")
        return rendered
    if stripped.startswith(("⚒ ", "⌘ ")):
        stripped = stripped[2:].strip()
    if stripped.startswith("$ "):
        rendered.append("$ ", style=f"bold {style}")
        rendered.append(stripped[2:])
        return rendered
    if stripped.startswith("usando "):
        rendered.append("· ", style="dim")
        rendered.append(stripped, style="dim")
        return rendered
    name, _, args = stripped.partition(" ")
    rendered.append("⚒ ", style=f"bold {style}")
    rendered.append(name, style=f"bold {style}")
    if args:
        rendered.append(f" {args}", style="dim")
    return rendered


def _build_tools_renderable(tools, style: str):
    """Monta o bloco visual de tools do turno com linhas estilizadas por status.

    Cada entrada pode ser multi-linha (previews de diff/saída); a primeira linha
    recebe o tratamento de status e as demais aparecem indentadas, limitadas por
    quantidade de linhas — nunca por corte de caracteres.
    """
    if not isinstance(tools, list):
        return None
    entries = [str(tool) for tool in tools if str(tool).strip()]
    if not entries:
        return None
    parts = []
    for entry in entries:
        lines = entry.strip().splitlines()
        head, extra = lines[0], lines[1:]
        parts.append(_styled_tool_line(head, style))
        omitted = len(extra) - _TOOL_PREVIEW_LINE_LIMIT
        for continuation in extra[:_TOOL_PREVIEW_LINE_LIMIT]:
            parts.append(Text(f"    {continuation}", style="dim", no_wrap=False, overflow="fold"))
        if omitted > 0:
            parts.append(Text(f"    ⋮ +{omitted} linhas", style=f"dim {style}"))
    return Group(*parts)


def _build_agent_live_body(content: str, tools, style: str, *, thinking: bool = True):
    """Corpo do bloco transitório: pensamento em destaque e tools listadas abaixo.

    Mensagens de lifecycle (``thinking=False``) são status operacional e ficam
    discretas, sem o marcador de pensamento.
    """
    parts = []
    text = str(content or "").strip()
    if text:
        head = Text(no_wrap=False, overflow="fold")
        if thinking:
            head.append(f"{_thinking_pulse_marker()} ", style=f"bold {style}")
            head.append(text, style="italic")
        else:
            head.append("· ", style="dim")
            head.append(text, style="dim")
        parts.append(head)
    tools_renderable = _build_tools_renderable(tools, style)
    if tools_renderable is not None:
        parts.append(tools_renderable)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return Group(*parts)


def _build_agent_activity_renderable(payload, agent: str | None = None):
    """Renderiza atividade operacional compacta e vinculada ao agente."""
    data = payload if isinstance(payload, dict) else {"message": str(payload or "")}
    activity = str(data.get("activity") or "info").strip().lower()
    label = str(data.get("label") or agent or "Agente").strip()
    style = str(data.get("style") or "cyan").strip()
    message = str(data.get("message") or "").strip()

    icon, icon_style = {
        "retrying": ("↻", "bold yellow"),
        "reconnecting": ("↻", "bold yellow"),
        "failover": ("↪", "bold yellow"),
        "completed": ("✓", "bold green"),
        "failed": ("!", "bold red"),
        "cancelled": ("■", "bold yellow"),
        "aborted": ("■", "bold yellow"),
        "error": ("!", "bold red"),
        "warning": ("!", "bold yellow"),
    }.get(activity, ("·", "dim"))

    line = Text()
    line.append(f"  {icon} ", style=icon_style)
    line.append(label, style=f"bold {style}")

    if activity == "retrying":
        attempt = data.get("attempt")
        limit = data.get("limit")
        line.append(f" · {message or 'nova tentativa'}", style="yellow")
        if attempt is not None and limit is not None:
            line.append(f" · tentativa {attempt}/{limit}", style="dim yellow")
        detail = str(data.get("detail") or "").strip()
        if detail:
            line.append(f" · {detail}", style="dim")
        line.no_wrap = False
        line.overflow = "fold"
        return line

    if activity == "failover":
        target_label = str(data.get("target_label") or data.get("target") or "outro agente")
        target_style = str(data.get("target_style") or "cyan")
        line.append(f" · {message or 'indisponível'} · continuando com ", style="dim yellow")
        line.append(target_label, style=f"bold {target_style}")
        return line

    if activity in {"error", "failed", "warning", "cancelled", "aborted", "completed", "reconnecting"}:
        if activity in {"error", "failed"}:
            message_style = "red"
        elif activity == "completed":
            message_style = "green"
        else:
            message_style = "yellow"
        line.append(f" · {message}", style=message_style)
        return line

    if message:
        line.append(f" · {message}", style="dim")
    return line


def _build_turn_summary_renderable(payload, agent: str | None = None):
    """Monta resumo contextualizado das ferramentas usadas no turno."""
    if not isinstance(payload, dict):
        prefix = f"{agent} " if agent else ""
        return Text(f"{prefix}{payload}", style="dim")

    total = int(payload.get("total") or 0)
    ok_count = int(payload.get("ok_count") or 0)
    err_count = int(payload.get("err_count") or 0)
    duration = str(payload.get("duration") or "").strip()
    label = str(payload.get("label") or agent or "Agente")
    style = str(payload.get("style") or "cyan")
    icon = "✓" if err_count == 0 else "!"
    icon_style = "bold green" if err_count == 0 else "bold yellow"
    noun = "ferramenta" if total == 1 else "ferramentas"

    line = Text()
    line.append(f"  {icon} ", style=icon_style)
    line.append(label, style=f"bold {style}")
    line.append(f" · {total} {noun}", style="dim")
    line.append(f" · {ok_count} concluída{'s' if ok_count != 1 else ''}", style="green")
    if err_count:
        line.append(f" · {err_count} falha{'s' if err_count != 1 else ''}", style="yellow")
    if duration:
        line.append(f" · {duration}", style="dim")
    return line


def _breadcrumb_items(chain: list[str], from_label: str, to_label: str) -> list[str]:
    """Normaliza breadcrumb de delegação para sempre partir do humano."""
    items = [str(item).strip() for item in (chain or [from_label, to_label]) if str(item).strip()]
    if not items:
        return ["humano"]
    first = items[0].lower()
    if first in {"human", "humano", "user", "usuario", "usuário", ">>>"}:
        items[0] = "humano"
        return items
    return ["humano", *items]


def _orchestrator_section_name(line: str) -> str | None:
    """Detecta cabeçalhos simples para organizar respostas do orquestrador."""
    raw = str(line or "").strip()
    if not raw:
        return None
    cleaned = raw.strip("#*-: ").lower()
    cleaned = cleaned.removesuffix(":").strip()
    if len(cleaned) > 40:
        return None
    if cleaned in {"analise", "análise", "analysis", "raciocinio", "raciocínio"}:
        return "Análise"
    if cleaned in {"execucao", "execução", "execution", "plano", "delegacoes", "delegações"}:
        return "Execução"
    if cleaned in {"resultado", "result", "sintese", "síntese", "conclusao", "conclusão"}:
        return "Resultado"
    return None


def _split_orchestrator_sections(content: str) -> list[tuple[str, list[str]]]:
    """Agrupa texto em seções visuais estáveis para o painel de orquestração."""
    sections: list[tuple[str, list[str]]] = []
    current_title = "Análise"
    current_lines: list[str] = []
    for line in str(content or "").splitlines():
        title = _orchestrator_section_name(line)
        if title:
            if current_lines:
                sections.append((current_title, current_lines))
            current_title = title
            current_lines = []
            continue
        current_lines.append(line)
    if current_lines or not sections:
        sections.append((current_title, current_lines))
    return sections


def _orchestrator_line_renderable(line: str, style: str):
    """Indenta linhas que representam delegações dentro do painel do orquestrador."""
    text = str(line or "")
    if _DELEGATION_HINT_RE.search(text):
        rendered = Text()
        rendered.append("↳ ", style="dim")
        rendered.append(text.strip(), style=f"bold {style}")
        return Padding(rendered, pad=(0, 0, 0, 4))
    return Padding(Text(text, no_wrap=False, overflow="fold"), pad=(0, 0, 0, 2))


def _truncate_section_content(lines: list[str], *, line_limit: int = _DEFAULT_SECTION_LINE_LIMIT) -> tuple[list[str], int]:
    """Trunca linhas se excederem o limite. line_limit <=0 = sem limite. Retorna (linhas_truncadas, total_original)."""
    total = len(lines)
    if line_limit <= 0 or total <= line_limit:
        return lines, total
    return lines[:line_limit], total


def _build_section_panel(
    title: str,
    lines: list[str],
    style: str,
    *,
    expanded: bool = False,
    line_limit: int = _DEFAULT_SECTION_LINE_LIMIT,
):
    """Renderiza uma seção do orquestrador como painel expansível/retrátil."""
    display_lines, total = _truncate_section_content(lines, line_limit=line_limit)
    truncated = False
    if line_limit > 0:
        truncated = total > line_limit

    indicator = "▾" if expanded else "▸"
    panel_title = f"[bold {style}]{indicator} {title}[/bold {style}]"
    border = style if expanded else f"dim {style}"

    inner_parts = []
    for line in (display_lines or [""]):
        inner_parts.append(_orchestrator_line_renderable(line, style))

    if truncated:
        remaining = total - line_limit
        hint = Text()
        hint.append(f"\n  ... +{remaining} linhas", style=f"dim {style}")
        hint.append("  ", style="dim")
        hint.append("[expandir]", style=f"bold {style} underline")
        inner_parts.append(Padding(hint, pad=(0, 0, 0, 2)))

    body = Group(*inner_parts) if inner_parts else Text("")
    return Panel(body, title=panel_title, border_style=border, padding=(0, 1))


def _build_orchestrator_body(content: str, style: str):
    """Cria corpo visual do orquestrador com seções colapsáveis e truncamento."""
    sections = _split_orchestrator_sections(content)
    parts = []
    for idx, (title, lines) in enumerate(sections):
        expanded = title == "Resultado"
        line_limit = _DEFAULT_SECTION_LINE_LIMIT if not expanded else 0
        parts.append(_build_section_panel(title, lines, style, expanded=expanded, line_limit=line_limit))
    return Group(*parts) if parts else Text("")


def _build_question_overlay(payload) -> Panel:
    """Monta o overlay visual de pergunta usado pela UI Textual."""
    data = payload or {}
    question = str(data.get("question", "")).strip()
    kind = str(data.get("kind", "input")).strip().lower()
    title = str(data.get("title") or "").strip()
    options = list(data.get("options", []) or [])
    if kind == "approval":
        title = title or _APPROVAL_TITLE
        options = options or _approval_options()
    elif kind == "selection":
        title = title or "Seleção solicitada"
    elif not title:
        title = "input solicitado"

    lines = [question] if question else []
    if options:
        if lines:
            lines.append("")
        lines.append("Opções:")
        if kind == "selection":
            lines.extend(f"{index}. {option}" for index, option in enumerate(options, 1))
        else:
            lines.extend(f"- {option}" for option in options)

    body = "\n".join(lines) if lines else "Aguardando resposta..."
    border_style = "bold yellow" if kind == "approval" else "yellow"
    return Panel(body, title=title, border_style=border_style)


def _build_approval_line_renderable(payload) -> Text:
    """Renderiza aprovação no feed como linha compacta, sem caixa."""
    lines = [line.strip() for line in str(payload or "").splitlines() if line.strip()]
    if not lines:
        return Text("⚠ aprovação solicitada", style="bold orange1")
    title = lines[0]
    title = title.removeprefix("Aprovar ").strip() or title
    text = Text()
    text.append("⚠ ", style="bold orange1")
    text.append(title, style="bold orange1")
    for line in lines[1:]:
        text.append("\n  ", style="orange1")
        text.append(line, style="orange1")
    return text


def _build_window_overlay_payload(payload) -> dict[str, Any]:
    """Converte evento de janela interativa no payload do overlay."""
    data = dict(payload or {}) if isinstance(payload, dict) else {}
    metadata = dict(data.get("metadata") or {}) if isinstance(data.get("metadata"), dict) else {}
    kind = str(data.get("kind") or "input")
    title = str(data.get("title") or (_APPROVAL_TITLE if kind == "approval" else "input solicitado"))
    question = str(metadata.get("question") or data.get("question") or "")
    options = data.get("options") or metadata.get("options")
    if kind == "approval" and not options:
        options = _approval_options()
    return {
        "question": question,
        "options": list(options or []),
        "title": title,
        "kind": kind,
        "owner": data.get("owner"),
    }


def _clear_question_overlay_widget(overlay) -> None:
    """Remove o overlay de pergunta/permissão do widget Textual."""
    overlay.update("")
    overlay.display = False



def _render_event(event: TextualUiEvent):
    """Converte eventos do bridge para renderables Rich."""
    if event.kind == "user_message":
        payload = event.payload or {}
        content = str(payload.get("content", "")) if isinstance(payload, dict) else str(payload)
        if not content.strip():
            return None
        content = _clipboard_manager.humanize_markers(content)
        label = str(payload.get("label", "Alex")) if isinstance(payload, dict) else "Alex"
        style = str(payload.get("style", "green") or "green") if isinstance(payload, dict) else "green"
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME) if isinstance(payload, dict) else themes.DEFAULT_THEME
        return _render_turn_block(
            theme_name,
            label,
            style,
            content=content,
            render_mode="plain",
        )
    if event.kind == "agent_message":
        payload = event.payload or {}
        content = str(payload.get("content", ""))
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "cyan") or "cyan")
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        is_orchestrator = bool(payload.get("orchestrator", False))
        return _render_turn_block(
            theme_name,
            label,
            style,
            content=content,
            render_mode=str(payload.get("render_mode") or "auto"),
            is_orchestrator=is_orchestrator,
        )
    if event.kind == "stream_start":
        payload = event.payload or {}
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "cyan") or "cyan")
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        return _build_stream_renderable(theme_name, label, style, "gerando...")
    if event.kind == "stream_abort":
        payload = dict(event.payload or {}) if isinstance(event.payload, dict) else {}
        payload.update({"activity": "aborted", "message": "execução interrompida"})
        return _build_agent_activity_renderable(payload, event.agent)
    if event.kind == "stream_chunk":
        payload = event.payload if isinstance(event.payload, dict) else {}
        content = str(payload.get("content") or payload.get("text") or event.payload)
        tools = payload.get("tools") if isinstance(payload, dict) else None
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "cyan") or "cyan")
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        return _build_stream_renderable(theme_name, label, style, content, tools=tools)
    if event.kind == "pending_input":
        payload = event.payload if isinstance(event.payload, dict) else {}
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "cyan") or "cyan")
        question = str(payload.get("question") or "")
        kind = str(payload.get("kind") or "input")
        return _build_pending_card_renderable(label, style, question, kind=kind)
    if event.kind == "agent_lifecycle":
        payload = event.payload or {}
        raw_message = str(payload.get("message", "")) if isinstance(payload, dict) else str(payload)
        message = _strip_rich_markup_tags(raw_message)
        status = str(payload.get("status", "")).lower() if isinstance(payload, dict) else ""
        _TERMINAL_STATUSES = {"completed", "failed", "error", "cancelled", "aborted"}
        if status in _TERMINAL_STATUSES:
            if status == "failed" and ("reconect" in message.lower() or "tentativa" in message.lower()):
                status = "reconnecting"
            activity_payload = dict(payload) if isinstance(payload, dict) else {}
            activity_payload.update({"activity": status, "message": message})
            return _build_agent_activity_renderable(activity_payload, event.agent)
        tools = payload.get("tools") if isinstance(payload, dict) else None
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}")) if isinstance(payload, dict) else f"🤖 {event.agent or 'agente'}"
        style = str(payload.get("style", "cyan") or "cyan") if isinstance(payload, dict) else "cyan"
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME) if isinstance(payload, dict) else themes.DEFAULT_THEME
        return _build_stream_renderable(theme_name, label, style, message, tools=tools, thinking=False)
    if event.kind in {"warning", "error"}:
        if event.agent:
            return _build_agent_activity_renderable(
                {
                    "activity": event.kind,
                    "message": str(event.payload),
                },
                event.agent,
            )
        style = "yellow" if event.kind == "warning" else "red"
        return Text(str(event.payload), style=style)
    if event.kind == "banner":
        return Group(banner_gradient_text(str(event.payload)), Rule(style="dim cyan"))
    if event.kind == "approval":
        return _build_approval_line_renderable(event.payload)
    if event.kind == "delegation":
        payload = event.payload if isinstance(event.payload, dict) else {}
        task = str(payload.get("task", "")).strip()
        chain = [str(item).strip() for item in (payload.get("chain") or []) if str(item).strip()]
        from_label = str(payload.get("from_label", "agente"))
        from_style = str(payload.get("from_style", "cyan"))
        to_label = str(payload.get("to_label", "agente"))
        to_style = str(payload.get("to_style", "cyan"))
        delegation_id = str(payload.get("delegation_id") or "").strip()
        breadcrumb_items = _breadcrumb_items(chain, from_label, to_label)
        breadcrumb = " > ".join(breadcrumb_items)
        breadcrumb_title = f"  cadeia: {breadcrumb}"
        if delegation_id:
            breadcrumb_title += f"  ·  #{delegation_id[:8]}"
        body = Text()
        body.append(f"▸ {from_label}", style=f"bold {from_style}")
        body.append(" → ", style="dim")
        body.append(to_label, style=f"bold {to_style}")
        if task:
            body.append(f"\n  ·  {task}", style="dim")
        return Panel(body, title=f"[dim]{breadcrumb_title}[/dim]", border_style="dim", padding=(0, 1))
    if event.kind == "turn_summary":
        return _build_turn_summary_renderable(event.payload, event.agent)
    if event.kind == "agent_activity":
        return _build_agent_activity_renderable(event.payload, event.agent)
    if event.kind == "question":
        payload = event.payload or {}
        lines = [str(payload.get("question", ""))]
        for index, option in enumerate(payload.get("options", []) or [], 1):
            lines.append(f"{index}. {option}")
        return Panel("\n".join(lines), title="input solicitado", border_style="yellow")
    if event.kind == "agent_update":
        if isinstance(event.payload, dict):
            content = str(event.payload.get("content") or "")
            tools = event.payload.get("tools")
            label = str(event.payload.get("label", f"🤖 {event.agent or 'agente'}"))
            style = str(event.payload.get("style", "cyan") or "cyan")
            theme_name = str(event.payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        else:
            content = str(event.payload)
            tools = None
            label = f"🤖 {event.agent or 'agente'}"
            style = "cyan"
            theme_name = themes.DEFAULT_THEME
        if event.agent:
            return _build_stream_renderable(theme_name, label, style, content, tools=tools)
        if not content.strip():
            return None
        return Text(content, style="dim")
    if event.kind == "prompt":
        return None
    if event.kind == "input_active":
        return None
    if event.kind == "clear":
        return None
    if event.kind == "plain":
        content = str(event.payload or "")
        if event.agent:
            text = Text.assemble((f"◦ {event.agent} ", "dim cyan"), (content,))
            return text
        return Text(content)
    if event.kind == "muted":
        return Text(str(event.payload), style="dim")
    if event.kind == "system":
        if str(event.payload or "").strip().lower() == "cancelamento solicitado":
            return Text.assemble(
                ("  ■ ", "bold yellow"),
                ("Execução", "bold yellow"),
                (" · cancelamento solicitado", "dim yellow"),
            )
        return Text(str(event.payload), style="blue")
    if event.kind == "theme_changed":
        payload = event.payload if isinstance(event.payload, dict) else {}
        theme_name = str(payload.get("theme", "")).strip()
        return Text(f"tema: {theme_name}" if theme_name else "tema atualizado", style="dim cyan")
    return Text(str(event.payload))


def _render_themed_agent_block(theme_name: str, label: str, style: str, body, *, streaming: bool = False):
    """Renderiza bloco de agente na Textual usando os mesmos nomes de tema do renderer legado."""
    name = themes.get(theme_name).name
    if name == "panel":
        title = f"[bold {style}]{label}[/bold {style}]"
        return Panel(body, title=title, border_style=style, padding=(0, 1))
    if name == "chat":
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=2)
        table.add_column(ratio=1)
        table.add_row(
            Text("●", style=f"bold {style}"),
            Group(
                Text(label, style=f"bold {style}"),
                Padding(body, pad=(0, 0, 0, 2)),
            ),
        )
        return table
    if name == "rule":
        return Group(
            Rule(f"[bold {style}]{label}[/bold {style}]", style=f"dim {style}"),
            body,
            Rule(style="dim"),
        )
    if name == "minimal":
        return Group(Text(f"▶ {label}", style=f"bold {style}"), Padding(body, pad=(0, 0, 0, 2)))
    if name == "card":
        return Panel(
            body,
            title=f"[bold {style}]{label}[/bold {style}]",
            border_style=f"dim {style}",
            padding=(0, 1),
            subtitle="▸" if not streaming else None,
            subtitle_align="right",
        )
    if name == "line":
        return Group(Text(label, style=f"bold {style}"), body)
    return Panel(body, title=label, border_style=style)


def _build_turn_header(theme_name: str, label: str, style: str):
    """Monta cabeçalho de turno seguindo o renderer main-tui."""
    name = themes.get(theme_name).name
    if name == "chat":
        header = Table.grid(expand=True, padding=(0, 1))
        header.add_column(width=2)
        header.add_column(ratio=1)
        header.add_row(Text("●", style=f"bold {style}"), Text(label, style=f"bold {style}"))
        return header
    if name == "rule":
        return Rule(f"[bold {style}]{label}[/bold {style}]", style=f"dim {style}")
    if name == "minimal":
        return Text(f"▶ {label}", style=f"bold {style}")
    if name == "card":
        return Text(f"▎ {label}", style=f"bold {style}")
    if name == "line":
        return Text(label, style=f"bold {style}")
    return Text(label, style=f"bold {style}")


def _build_turn_body(
    theme_name: str,
    label: str,
    style: str,
    content: str,
    *,
    streaming: bool = False,
    render_mode: str = "auto",
    muted_body: bool = False,
):
    """Monta corpo textual do turno seguindo o renderer main-tui."""
    name = themes.get(theme_name).name
    mode = str(render_mode or "auto").strip().lower()
    if mode == "auto":
        mode = "markdown"
    _body_style = "dim" if muted_body else ""
    body_content = Text(content or "", style=_body_style, no_wrap=False, overflow="fold") if streaming or mode == "plain" else Markdown(content or "")
    if name == "panel":
        title = f"[bold {style}]{label}[/bold {style}]" if streaming else None
        return Panel(body_content, title=title, border_style=style, padding=(0, 1))
    if name == "chat":
        return Padding(body_content, pad=(0, 0, 0, 4))
    if name == "minimal":
        return Padding(body_content, pad=(0, 0, 0, 2))
    if name == "card":
        return Panel(body_content, border_style=f"dim {style}", padding=(0, 1))
    if name == "line":
        return body_content
    return body_content


def _render_turn_block(
    theme_name: str,
    label: str,
    style: str,
    *,
    content: str | None = None,
    tools_table=None,
    turn_id: str = "",
    include_header: bool = True,
    include_footer_rule: bool = False,
    streaming: bool = False,
    render_mode: str = "auto",
    muted_body: bool = False,
    is_orchestrator: bool = False,
):
    """Monta bloco estruturado de turno: header -> corpo -> tools."""
    if is_orchestrator and content:
        return Panel(
            _build_orchestrator_body(content or "", style),
            title=f"[bold {style}][Orquestrador] {label}[/bold {style}]",
            border_style="blue",
            padding=(0, 1),
        )
    parts = []
    if include_header:
        parts.append(_build_turn_header(theme_name, label, style))
    if content:
        parts.append(
            _build_turn_body(
                theme_name,
                label,
                style,
                content,
                streaming=streaming,
                render_mode=render_mode,
                muted_body=muted_body,
            )
        )
    if tools_table is not None:
        parts.append(_build_turn_tools(theme_name, label, style, tools_table, turn_id))
    if include_footer_rule and themes.get(theme_name).name == "rule":
        parts.append(Rule(style="dim"))
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return Group(*parts)


def _build_turn_tools(theme_name: str, label: str, style: str, tools_table, turn_id: str):
    """Monta seção de tools vinculada visualmente ao turno."""
    name = themes.get(theme_name).name
    title = f"tools · {turn_id}" if turn_id else "tools"
    if name == "panel":
        return Panel(
            tools_table,
            title=f"[bold {style}]{label} · {title}[/bold {style}]",
            border_style=style,
            padding=(0, 0),
        )
    if name == "chat":
        row = Table.grid(expand=True, padding=(0, 1))
        row.add_column(width=2)
        row.add_column(ratio=1)
        row.add_row(
            Text("◦", style=f"dim {style}"),
            Group(Text(title, style=f"bold {style}"), Padding(tools_table, pad=(0, 0, 0, 2))),
        )
        return row
    if name == "rule":
        return Group(Text(title, style=f"bold {style}"), tools_table)
    if name == "minimal":
        return Group(Text(f"◦ {title}", style=f"bold {style}"), Padding(tools_table, pad=(0, 0, 0, 2)))
    if name == "card":
        return Panel(
            tools_table,
            border_style=f"dim {style}",
            padding=(0, 1),
            title=f"[bold {style}]{title}[/bold {style}]" if turn_id else None,
        )
    if name == "line":
        return Group(Text(title, style=f"bold {style}"), tools_table)
    return tools_table


def _build_stream_renderable(theme_name: str, label: str, style: str, content: str, tools=None, *, thinking: bool = True):
    """Monta o renderable dinâmico usado no streaming, com pensamento em destaque."""
    body = _build_agent_live_body(content, tools, style, thinking=thinking)
    if body is None:
        return None
    return _render_themed_agent_block(theme_name, label, style, body, streaming=True)


def _build_pending_card_renderable(label: str, style: str, question: str, *, kind: str = "input"):
    """Monta badge inline de aprovação/input pendente."""
    icon = "⚠" if str(kind).strip().lower() == "approval" else "❓"
    fallback = "aguardando aprovação" if icon == "⚠" else "aguardando input"
    first_line = str(question or "").strip().splitlines()[0] if str(question or "").strip() else fallback
    content = Text.assemble(
        (f"\n{icon} ", "bold yellow"),
        (first_line, "bold yellow"),
        ("\n  Executar? [y/N/a=todas]\n" if icon == "⚠" else "\n  aguardando resposta do usuário\n", "dim yellow"),
    )
    return Panel(
        Padding(content, pad=(0, 0, 0, 2)),
        title=f"[bold {style}]{label}[/bold {style}] · pendente",
        border_style="yellow",
        padding=(0, 1),
    )

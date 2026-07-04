"""Renderables e helpers visuais da interface Textual."""
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

import quimera.themes as themes
from quimera.ui.textual.constants import (
    APPROVAL_OPTIONS as _APPROVAL_OPTIONS,
    APPROVAL_TITLE as _APPROVAL_TITLE,
)
from quimera.ui.textual.events import TextualUiEvent

_RICH_MARKUP_TAG_RE = re.compile(r"\[/?[a-zA-Z][a-zA-Z0-9_#= .:-]*\]")


def _approval_options() -> list[str]:
    """Retorna as opções visuais padrão para confirmação de permissão."""
    return list(_APPROVAL_OPTIONS)


def _strip_rich_markup_tags(value: str) -> str:
    """Remove tags Rich inline que chegam como texto de eventos transitórios."""
    return _RICH_MARKUP_TAG_RE.sub("", str(value or ""))


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
        return _render_turn_block(
            theme_name,
            label,
            style,
            content=content,
            render_mode=str(payload.get("render_mode") or "auto"),
        )
    if event.kind == "stream_start":
        payload = event.payload or {}
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "cyan") or "cyan")
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        return _build_stream_renderable(theme_name, label, style, "gerando...")
    if event.kind == "stream_abort":
        payload = event.payload or {}
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "red") or "red")
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        return _build_stream_renderable(theme_name, label, style, "interrompido")
    if event.kind == "stream_chunk":
        payload = event.payload if isinstance(event.payload, dict) else {}
        content = str(payload.get("content") or payload.get("text") or event.payload)
        tools = payload.get("tools") if isinstance(payload, dict) else None
        if isinstance(tools, list) and tools:
            tool_block = "\n".join(str(tool) for tool in tools if str(tool).strip())
            if tool_block:
                content = f"{content.rstrip()}\n{tool_block}" if content.strip() else tool_block
        if not content.strip():
            return None
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "cyan") or "cyan")
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        return _build_stream_renderable(theme_name, label, style, content)
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
        tools = payload.get("tools") if isinstance(payload, dict) else None
        if isinstance(tools, list) and tools:
            tool_block = "\n".join(str(tool) for tool in tools if str(tool).strip())
            if tool_block:
                message = f"{message.rstrip()}\n{tool_block}" if message.strip() else tool_block
        if not message.strip():
            return None
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}")) if isinstance(payload, dict) else f"🤖 {event.agent or 'agente'}"
        style = str(payload.get("style", "cyan") or "cyan") if isinstance(payload, dict) else "cyan"
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME) if isinstance(payload, dict) else themes.DEFAULT_THEME
        return _build_stream_renderable(theme_name, label, style, message)
    if event.kind in {"warning", "error"}:
        style = "yellow" if event.kind == "warning" else "red"
        return Text(str(event.payload), style=style)
    if event.kind == "banner":
        return Group(Text(str(event.payload), style="bold cyan"), Rule(style="dim cyan"))
    if event.kind == "approval":
        return _build_approval_line_renderable(event.payload)
    if event.kind == "delegation":
        payload = event.payload if isinstance(event.payload, dict) else {}
        task = str(payload.get("task", "")).strip()
        text = Text()
        text.append(str(payload.get("from_label", "agente")), style=f"bold {payload.get('from_style', 'cyan')}")
        text.append(" → ", style="dim")
        text.append(str(payload.get("to_label", "agente")), style=f"bold {payload.get('to_style', 'cyan')}")
        if task:
            text.append(f" · {task}", style="dim")
        return Rule(text, style="dim")
    if event.kind == "turn_summary":
        prefix = f"{event.agent} " if event.agent else ""
        return Text(f"{prefix}{event.payload}", style="dim")
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
            if isinstance(tools, list) and tools:
                tool_block = "\n".join(str(tool) for tool in tools if str(tool).strip())
                if tool_block:
                    content = f"{content.rstrip()}\n{tool_block}" if content.strip() else tool_block
            label = str(event.payload.get("label", f"🤖 {event.agent or 'agente'}"))
            style = str(event.payload.get("style", "cyan") or "cyan")
            theme_name = str(event.payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        else:
            content = str(event.payload)
            label = f"🤖 {event.agent or 'agente'}"
            style = "cyan"
            theme_name = themes.DEFAULT_THEME
        if not content.strip():
            return None
        if event.agent:
            return _build_stream_renderable(theme_name, label, style, content)
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
):
    """Monta bloco estruturado de turno: header -> corpo -> tools."""
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


def _build_stream_renderable(theme_name: str, label: str, style: str, content: str):
    """Monta o renderable dinâmico usado no streaming."""
    return _render_turn_block(
        theme_name,
        label,
        style,
        content=content,
        include_header=True,
        streaming=True,
        render_mode="plain",
        muted_body=True,
    )


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


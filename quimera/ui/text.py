"""Pure text helpers extracted from the terminal renderer.

These functions have no Rich, threading, or I/O dependencies — they
only transform strings and renderables. Safe to import from anywhere.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rich.text import Text

from quimera.runtime.streaming import apply_stream_diff, normalize_stream_diff

_UNICODE_CONTROL_RE = re.compile(
    r"[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u061C\u200B-\u200F\u202A-\u202E\u2060-\u2069\uFEFF]"
)
_PREVIEW_LIMIT = 160
_TAG_HIGHLIGHT_RE = re.compile(r'(</?[\w-]+(?:\s+[^>]*?)?\s*/?>)')


def strip_ansi(text: str) -> str:
    ansi_real = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')
    text = ansi_real.sub('', text)
    ansi_orphaned = re.compile(r'\[[0-9;?]+[A-Za-z]')
    text = ansi_orphaned.sub('', text)
    text = _UNICODE_CONTROL_RE.sub('', text)
    return text


def _normalize_completed_content(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def _normalize_stream_diff(diff) -> list[dict[str, str]]:
    return normalize_stream_diff(diff, transform_text=strip_ansi)


def _apply_stream_diff(content: str, diff: list[dict[str, str]]) -> str:
    return apply_stream_diff(content, diff)


def _extract_text_from_renderable(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if hasattr(value, "title") and hasattr(value, "characters"):
        return _extract_text_from_renderable(value.title)
    if hasattr(value, "columns") and hasattr(value, "rows"):
        parts = []
        for column in value.columns:
            parts.append(_extract_text_from_renderable(getattr(column, "header", "")))
            for cell in getattr(column, "_cells", ()):
                parts.append(_extract_text_from_renderable(cell))
        return " ".join(p for p in parts if p)
    if hasattr(value, "plain"):
        return str(value.plain)
    if hasattr(value, "renderables"):
        parts = []
        for child in value.renderables:
            parts.append(_extract_text_from_renderable(child))
        return " ".join(p for p in parts if p)
    if hasattr(value, "__rich_text__"):
        return str(value.__rich_text__())
    markup = getattr(value, "markup", None)
    if isinstance(markup, str):
        return markup
    panel_renderable = getattr(value, "renderable", None)
    if panel_renderable is not None:
        parts = []
        title = getattr(value, "title", None) or ""
        if title:
            parts.append(_extract_text_from_renderable(title))
        parts.append(_extract_text_from_renderable(panel_renderable))
        return " ".join(p for p in parts if p)
    return str(value)


def _preview_text(value: Any, limit: int = _PREVIEW_LIMIT) -> str:
    text = strip_ansi(_extract_text_from_renderable(value)).replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _preview_chunk(chunk: Any) -> str:
    if isinstance(chunk, dict):
        text = chunk.get("text")
        if text:
            return _preview_text(text)
        diff = chunk.get("diff")
        if diff:
            try:
                normalized = _normalize_stream_diff(diff)
            except Exception:
                normalized = []
            parts = []
            for item in normalized:
                if not isinstance(item, dict):
                    continue
                if item.get("op") not in {"append", "replace"}:
                    continue
                part_text = item.get("text")
                if part_text:
                    parts.append(str(part_text))
            if parts:
                return _preview_text(" | ".join(parts))
            return _preview_text(diff)
    return _preview_text(chunk)


def _highlight_tags(text: str) -> "Text":
    from rich.text import Text as _RichText

    result = _RichText()
    for token in _TAG_HIGHLIGHT_RE.split(text):
        if not token:
            continue
        if _TAG_HIGHLIGHT_RE.fullmatch(token):
            result.append(token, style="bold magenta")
        else:
            result.append(token)
    return result

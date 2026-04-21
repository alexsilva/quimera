"""Helpers compartilhados para diffs incrementais de streaming."""
from __future__ import annotations

from typing import Any, Callable


def normalize_stream_diff(
    diff: Any,
    *,
    transform_text: Callable[[str], str] | None = None,
) -> list[dict[str, str]]:
    """Normaliza o payload incremental aceito pelos consumidores."""
    if diff is None:
        return []
    if isinstance(diff, dict):
        diff = [diff]

    normalized: list[dict[str, str]] = []
    for item in diff:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op") or "add").lower()
        text = item.get("text")
        if text is None:
            text = item.get("content")
        if text is None:
            text = item.get("value")
        if text is None:
            continue
        normalized_text = str(text)
        if transform_text is not None:
            normalized_text = transform_text(normalized_text)
        normalized.append({
            "op": "replace" if op == "replace" else "add",
            "text": normalized_text,
        })
    return normalized


def apply_stream_diff(content: str, diff: list[dict[str, str]]) -> str:
    """Aplica operações incrementais de texto no buffer atual."""
    updated = content
    for item in diff:
        if item["op"] == "replace":
            updated = item["text"]
        else:
            updated += item["text"]
    return updated

from __future__ import annotations

from enum import Enum


class PromptKind(str, Enum):
    CHAT = "chat"
    TASK_EXECUTOR = "task_executor"
    TASK_REVIEWER = "task_reviewer"


def coerce_prompt_kind(value: PromptKind | str | None) -> PromptKind:
    """Normaliza valores externos para um kind conhecido com fallback seguro."""
    if isinstance(value, PromptKind):
        return value
    normalized = str(value or PromptKind.CHAT.value).strip().lower()
    try:
        return PromptKind(normalized)
    except ValueError:
        return PromptKind.CHAT

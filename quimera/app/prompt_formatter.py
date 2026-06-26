"""Formatador do prompt visível ao humano."""
from __future__ import annotations

from ..config import DEFAULT_USER_NAME


class PromptFormatter:
    """Formata o prompt visível ao humano."""

    @staticmethod
    def format_user_prompt(user_name: str | None, mode_name: str | None = None) -> str:
        """Formata prompt humano, exibindo `[mode]` apenas fora do modo default."""
        normalized_name = str(user_name or "").strip()
        if not normalized_name:
            normalized_name = DEFAULT_USER_NAME
        if normalized_name not in {">", ">>>"}:
            normalized_name = normalized_name.rstrip(":").rstrip(">").strip() or DEFAULT_USER_NAME

        normalized_mode = str(mode_name or "").strip().lower() or "default"
        if normalized_mode in {"default", "execute"}:
            if normalized_name in {">", ">>>"}:
                return f"{normalized_name} "
            return f"{normalized_name}: "
        if normalized_name in {">", ">>>"}:
            return f"{normalized_name} [{normalized_mode}]: "
        return f"{normalized_name} [{normalized_mode}]: "

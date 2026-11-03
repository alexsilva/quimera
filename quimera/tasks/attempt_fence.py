"""Fences de tentativa repassados por ``**kwargs`` ao repositório de tasks."""

from __future__ import annotations

from typing import TypedDict


class AttemptFence(TypedDict, total=False):
    """Gate otimista: a escrita só vale se a tentativa ainda for a esperada."""

    expected_attempt: int | None

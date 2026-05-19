"""Modelos para registro de evidências."""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(slots=True)
class Evidence:
    ts: str
    path: str
    digest: str
    type: str = ""
    summary: str = ""
    agent: str = ""
    session_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

"""Coordinated multi-agent debate subsystem."""

from .commands import DebateCommand, DebateCommandError, parse_debate_command
from .models import (
    DebateContribution,
    DebateLimits,
    DebateMode,
    DebateProtocolError,
    DebateSession,
    DebateStatus,
    DebateSynthesis,
    WorkItem,
)
from .repository import DebateRepository
from .service import DebateService

__all__ = [
    "DebateCommand",
    "DebateCommandError",
    "DebateContribution",
    "DebateLimits",
    "DebateMode",
    "DebateProtocolError",
    "DebateRepository",
    "DebateService",
    "DebateSession",
    "DebateStatus",
    "DebateSynthesis",
    "WorkItem",
    "parse_debate_command",
]

"""Pacote quimera.agents — re-exporta símbolos públicos para retrocompatibilidade."""
from quimera.agent_events import _SyntheticToolResult
from quimera.agents.client import AgentClient
from quimera.agents.text_filters import (
    _filter_stderr_lines,
    _is_rate_limit_signal,
    _should_ignore_stderr_line,
    _strip_spinner,
)

__all__ = [
    "AgentClient",
    "_SyntheticToolResult",
    "_filter_stderr_lines",
    "_is_rate_limit_signal",
    "_should_ignore_stderr_line",
    "_strip_spinner",
]

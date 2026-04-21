"""Configuração compartilhada para limite de hops de ferramentas."""

DEFAULT_MAX_TOOL_HOPS = 32
MAX_TOOL_HOPS_BY_RELIABILITY = {
    "low": 4,
    "medium": DEFAULT_MAX_TOOL_HOPS,
    "high": 64,
}


def get_max_tool_hops(tool_use_reliability: str | None) -> int:
    """Resolve o limite de hops a partir da confiabilidade declarada."""
    reliability = str(tool_use_reliability or "medium").lower()
    return MAX_TOOL_HOPS_BY_RELIABILITY.get(reliability, DEFAULT_MAX_TOOL_HOPS)

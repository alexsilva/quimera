"""Configuração compartilhada para limite de hops de ferramentas."""

DEFAULT_MAX_TOOL_HOPS = 64
MAX_TOOL_HOPS_BY_RELIABILITY = {
    "low": DEFAULT_MAX_TOOL_HOPS // 2,
    "medium": DEFAULT_MAX_TOOL_HOPS,
    "high": DEFAULT_MAX_TOOL_HOPS * 2,
}


def get_max_tool_hops(tool_use_reliability: str | None) -> int:
    """Resolve o limite de hops a partir da confiabilidade declarada."""
    reliability = str(tool_use_reliability or "medium").lower()
    return MAX_TOOL_HOPS_BY_RELIABILITY.get(reliability, DEFAULT_MAX_TOOL_HOPS)

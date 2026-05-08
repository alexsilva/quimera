"""Configuração compartilhada para limite de hops de ferramentas."""

DEFAULT_MAX_TOOL_HOPS = 24
MAX_TOOL_HOPS_BY_RELIABILITY = {
    "low": 12,
    "medium": 24,
    "high": 40,
}

DEFAULT_MAX_CONSECUTIVE_INVALID_TOOL_SIGNATURES = 3
MAX_CONSECUTIVE_INVALID_TOOL_SIGNATURES_BY_RELIABILITY = {
    "low": 5,
    "medium": 4,
    "high": 3,
}


def get_max_tool_hops(tool_use_reliability: str | None) -> int:
    """Resolve o limite de hops a partir da confiabilidade declarada."""
    reliability = str(tool_use_reliability or "medium").lower()
    return MAX_TOOL_HOPS_BY_RELIABILITY.get(reliability, DEFAULT_MAX_TOOL_HOPS)


def get_invalid_tool_loop_threshold(tool_use_reliability: str | None) -> int:
    """Resolve quantas ocorrências consecutivas do mesmo erro policy disparam abort."""
    reliability = str(tool_use_reliability or "medium").lower()
    return MAX_CONSECUTIVE_INVALID_TOOL_SIGNATURES_BY_RELIABILITY.get(
        reliability,
        DEFAULT_MAX_CONSECUTIVE_INVALID_TOOL_SIGNATURES,
    )

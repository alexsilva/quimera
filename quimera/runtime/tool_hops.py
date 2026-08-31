"""Configuração compartilhada para limite de hops de ferramentas."""

DEFAULT_MAX_TOOL_HOPS = 256
MAX_TOOL_HOPS_BY_RELIABILITY = {
    "low": 128,
    "medium": 256,
    "high": 512,
}

DEFAULT_MAX_MODEL_REQUESTS = DEFAULT_MAX_TOOL_HOPS + 1
MAX_MODEL_REQUESTS_BY_RELIABILITY = {
    reliability: max_hops + 1
    for reliability, max_hops in MAX_TOOL_HOPS_BY_RELIABILITY.items()
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


def get_max_model_requests(tool_use_reliability: str | None) -> int:
    """Resolve o orçamento padrão de chamadas ao modelo por execução."""
    reliability = str(tool_use_reliability or "medium").lower()
    return MAX_MODEL_REQUESTS_BY_RELIABILITY.get(
        reliability,
        DEFAULT_MAX_MODEL_REQUESTS,
    )


def get_invalid_tool_loop_threshold(tool_use_reliability: str | None) -> int:
    """Resolve quantas ocorrências consecutivas do mesmo erro policy disparam abort."""
    reliability = str(tool_use_reliability or "medium").lower()
    return MAX_CONSECUTIVE_INVALID_TOOL_SIGNATURES_BY_RELIABILITY.get(
        reliability,
        DEFAULT_MAX_CONSECUTIVE_INVALID_TOOL_SIGNATURES,
    )

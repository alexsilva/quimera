"""Estrutura de erros ricos para tools no Quimera."""


class ToolError(Exception):
    """Base exception para erros de tool."""
    def __init__(self, message: str, *, metadata: dict | None = None):
        super().__init__(message)
        self.metadata = metadata or {}


class ToolValidationError(ToolError):
    """Erro de validação de input (ex: formato inválido, campo obrigatório faltando)."""
    def __init__(self, message: str, *, field: str | None = None, hint: str | None = None):
        metadata = {}
        if field:
            metadata["field"] = field
        if hint:
            metadata["hint"] = hint
        super().__init__(message, metadata=metadata)


class ToolEnvironmentError(ToolError):
    """Erro de ambiente (ex: arquivo inexistente, permissão negada, comando não encontrado)."""
    def __init__(self, message: str, *, action: str | None = None, path: str | None = None):
        metadata = {}
        if action:
            metadata["action"] = action
        if path:
            metadata["path"] = path
        super().__init__(message, metadata=metadata)


class ToolLogicError(ToolError):
    """Erro de lógica (ex: operação inválida, estado incompatível, regra violada)."""
    def __init__(self, message: str, *, rule: str | None = None, context: dict | None = None):
        metadata = {"rule": rule} if rule else {}
        if context:
            metadata.update(context)
        super().__init__(message, metadata=metadata)


class ToolRateLimitError(ToolError):
    """Erro de rate limit ou throttling."""
    def __init__(self, message: str, *, retry_after: float | None = None):
        metadata = {}
        if retry_after:
            metadata["retry_after"] = retry_after
        super().__init__(message, metadata=metadata)


class ToolPolicyViolationError(ToolError):
    """Erro de política de ferramenta (comando bloqueado, allowlist, etc.)."""

    def __init__(self, message: str, *, hint: str | None = None, rule: str | None = None):
        metadata = {}
        if hint:
            metadata["hint"] = hint
        if rule:
            metadata["rule"] = rule
        super().__init__(message, metadata=metadata)


class DelegationDispatchError(RuntimeError):
    """Falha de despacho de uma delegação, com recibo estruturado da causa.

    ``receipt`` carrega o diagnóstico coletado na fronteira do AgentClient
    (error_kind, exit_code, signal, stderr_tail, duration_s, ...), para que o
    delegador receba a causa real em vez de apenas "no response".
    """

    def __init__(self, message: str, *, receipt: dict | None = None):
        super().__init__(message)
        self.receipt = dict(receipt) if receipt else {}


def format_dispatch_failure(agent, receipt: dict | None) -> str:
    """Resume o recibo de falha em uma frase legível para o delegador."""
    if not receipt:
        return f"Agent '{agent}' returned no response"
    kind = str(receipt.get("error_kind") or "unknown")
    parts = [f"Agent '{agent}' failed ({kind})"]
    message = str(receipt.get("message") or "").strip()
    if message:
        parts.append(message)
    exit_code = receipt.get("exit_code")
    signal_name = receipt.get("signal")
    if exit_code is not None:
        exit_label = f"exit={exit_code}"
        if signal_name:
            exit_label += f"/{signal_name}"
        parts.append(exit_label)
    duration = receipt.get("duration_s")
    if duration is not None:
        parts.append(f"após {duration}s")
    return ": ".join(parts[:2]) + (
        f" [{', '.join(parts[2:])}]" if len(parts) > 2 else ""
    )


# Mapeamento de tipos para facilitar roteamento
TOOL_ERROR_TYPES = {
    "validation": ToolValidationError,
    "environment": ToolEnvironmentError,
    "logic": ToolLogicError,
    "policy": ToolPolicyViolationError,
    "rate_limit": ToolRateLimitError,
}

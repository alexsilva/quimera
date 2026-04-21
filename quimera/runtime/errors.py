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


# Mapeamento de tipos para facilitar roteamento
TOOL_ERROR_TYPES = {
    "validation": ToolValidationError,
    "environment": ToolEnvironmentError,
    "logic": ToolLogicError,
    "rate_limit": ToolRateLimitError,
}

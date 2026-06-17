"""Base classes para ferramentas do runtime."""
from __future__ import annotations

from pathlib import Path

from ..config import ToolRuntimeConfig
from ..models import ToolCall
from ..policy import ToolPolicyError, is_path_inside


class ToolBase:
    """Base class mínima para ferramentas do runtime (apenas config).

    Subclasses podem declarar ``tool_prefix`` para garantir em tempo de import
    que todos os métodos públicos seguem o padrão ``<prefix>_<name>``.

    Exemplo::

        class GitTool(ToolBase, tool_prefix="git"):
            def git_status(self, call): ...
    """

    _tool_prefix: str = ""

    def __init_subclass__(cls, tool_prefix: str = "", **kw: object) -> None:
        super().__init_subclass__(**kw)
        if not tool_prefix:
            return
        cls._tool_prefix = tool_prefix
        bad = [
            name for name in vars(cls)
            if not name.startswith("_")
            and callable(getattr(cls, name))
            and not name.startswith(f"{tool_prefix}_")
        ]
        if bad:
            raise TypeError(
                f"{cls.__name__}: métodos públicos sem prefixo '{tool_prefix}_': {bad}"
            )

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de ToolBase."""
        self.config = config


class ValidatableTool(ToolBase):
    """Base class para validadores de ferramentas.

    Subclasses implementam métodos ``_validate_<tool_name>`` para cada
    operação que precisa de validação. O método ``validate`` despacha
    automaticamente pelo nome da chamada.
    """

    def validate(self, call: ToolCall) -> None:
        """Valida a chamada delegando para _validate_{call.name} se existir."""
        method = getattr(self, f"_validate_{call.name}", None)
        if method is not None:
            method(call)

    def _resolve_workspace_path(self, raw_path: str) -> Path:
        """Resolve e valida que o path está dentro do workspace."""
        normalized = raw_path.lstrip("/") or "."
        path = (self.config.workspace_root / normalized).resolve()
        if not is_path_inside(path, self.config.workspace_root):
            raise ToolPolicyError(f"Path fora da workspace: {raw_path}")
        return path

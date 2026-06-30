"""Política declarativa de autonomia no workspace."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from .approval_broker import RiskLevel

WORKSPACE_POLICY_PRESETS = ("strict", "autonomous")


class AutonomyLevel(str, Enum):
    """Nível de autonomia por categoria de risco."""

    DENY = "deny"
    PROMPT = "prompt"
    AUTO = "auto"


@dataclass(slots=True)
class WorkspacePolicy:
    """Política declarativa de autonomia no workspace, por categoria de risco.

    Cada campo de risco mapeia para um ``AutonomyLevel`` que determina
    se a ação é bloqueada, requer aprovação humana, ou é auto-aprovada.

    Os campos ``shell_allow_chaining`` e ``shell_skip_allowlist`` controlam
    a validação de comandos shell ortogonalmente à decisão de approval.
    """

    read: AutonomyLevel = AutonomyLevel.AUTO
    network: AutonomyLevel = AutonomyLevel.AUTO
    write: AutonomyLevel = AutonomyLevel.PROMPT
    shell: AutonomyLevel = AutonomyLevel.PROMPT
    destructive: AutonomyLevel = AutonomyLevel.PROMPT
    delegation: AutonomyLevel = AutonomyLevel.AUTO

    shell_allow_chaining: bool = False
    shell_skip_allowlist: bool = False

    _RISK_TO_FIELD: ClassVar[dict[RiskLevel, str]] = {
        RiskLevel.READ: "read",
        RiskLevel.NETWORK: "network",
        RiskLevel.WRITE: "write",
        RiskLevel.SHELL: "shell",
        RiskLevel.DESTRUCTIVE: "destructive",
        RiskLevel.DELEGATION: "delegation",
    }

    def level_for(self, risk: RiskLevel) -> AutonomyLevel:
        """Mapeia ``RiskLevel`` para ``AutonomyLevel``."""
        field_name = self._RISK_TO_FIELD.get(risk)
        if field_name is None:
            return AutonomyLevel.PROMPT
        return getattr(self, field_name)

    @classmethod
    def normalize_name(cls, name: str | None) -> str:
        """Normaliza nome de preset de policy."""
        value = str(name or "").strip().lower()
        if value in WORKSPACE_POLICY_PRESETS:
            return value
        return "strict"

    @classmethod
    def from_name(cls, name: str | None) -> WorkspacePolicy:
        """Cria policy a partir do nome persistido."""
        normalized = cls.normalize_name(name)
        if normalized == "autonomous":
            return cls.autonomous()
        return cls.strict()

    @classmethod
    def strict(cls) -> WorkspacePolicy:
        """Padrão restritivo — comportamento atual do sistema."""
        return cls()

    @classmethod
    def autonomous(cls) -> WorkspacePolicy:
        """Liberdade para codificar; denylist e path confinement mantidos."""
        return cls(
            write=AutonomyLevel.AUTO,
            shell=AutonomyLevel.AUTO,
            destructive=AutonomyLevel.PROMPT,
            shell_allow_chaining=True,
            shell_skip_allowlist=True,
        )

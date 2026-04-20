"""Componentes de `quimera.plugins.base`."""
from dataclasses import dataclass, field
from typing import Callable, FrozenSet, List, Optional, Tuple

from quimera.agent_events import SpyEvent


SpyFormatterOutput = List[SpyEvent]


@dataclass
class AgentPlugin:
    """Implementa `AgentPlugin`."""
    name: str
    prefix: str
    style: Tuple[str, str]  # (color, label) para UI
    aliases: List[str] = field(default_factory=list)
    icon: str = "🤖"
    runtime_rw_paths: List[str] = field(default_factory=list)
    # CLI fields
    cmd: List[str] = field(default_factory=list)
    prompt_as_arg: bool = False  # se True, prompt é passado como argumento CLI em vez de stdin
    # Agent capabilities
    capabilities: List[str] = field(default_factory=list)
    preferred_task_types: List[str] = field(default_factory=list)
    avoid_task_types: List[str] = field(default_factory=list)
    supports_tools: bool = True
    tool_use_reliability: str = "medium"
    supports_code_editing: bool = False
    supports_long_context: bool = False
    supports_task_execution: bool = True
    base_tier: int = 2  # 1: weak, 2: standard, 3: premium
    # API driver fields (driver != "cli" ignora cmd e usa a API diretamente)
    driver: str = "cli"  # "cli" | "openai_compat"
    output_format: Optional[str] = None  # "stream-json" para parsear output estruturado do CLI
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None  # nome da variável de ambiente com a API key
    spy_stdout_formatter: Optional[Callable[[str], SpyFormatterOutput]] = None
    stderr_noise: FrozenSet[str] = field(default_factory=frozenset)

    @property
    def render_style(self) -> Tuple[str, str]:
        """Retorna o estilo pronto para renderização na UI."""
        color, label = self.style
        return (color, f"{self.icon} {label}")


_registry: dict[str, AgentPlugin] = {}


def register(plugin: AgentPlugin) -> None:
    """Executa register."""
    _registry[plugin.name] = plugin


def get(name: str) -> Optional[AgentPlugin]:
    """Retorna get."""
    return _registry.get(name)


def all_names() -> List[str]:
    """Executa all names."""
    return list(_registry.keys())


def all_plugins() -> List[AgentPlugin]:
    """Executa all plugins."""
    return list(_registry.values())

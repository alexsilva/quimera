from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class AgentPlugin:
    name: str
    prefix: str
    style: Tuple[str, str]  # (color, label) para UI
    # CLI fields
    cmd: List[str] = field(default_factory=list)
    prompt_as_arg: bool = False  # se True, prompt é passado como argumento CLI em vez de stdin
    # Agent capabilities
    capabilities: List[str] = field(default_factory=list)
    preferred_task_types: List[str] = field(default_factory=list)
    avoid_task_types: List[str] = field(default_factory=list)
    supports_tools: bool = True
    supports_code_editing: bool = False
    supports_long_context: bool = False
    supports_task_execution: bool = True
    base_tier: int = 2  # 1: weak, 2: standard, 3: premium
    # API driver fields (driver != "cli" ignora cmd e usa a API diretamente)
    driver: str = "cli"  # "cli" | "openai_compat"
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None  # nome da variável de ambiente com a API key


_registry: dict[str, AgentPlugin] = {}


def register(plugin: AgentPlugin) -> None:
    _registry[plugin.name] = plugin


def get(name: str) -> Optional[AgentPlugin]:
    return _registry.get(name)


def all_names() -> List[str]:
    return list(_registry.keys())


def all_plugins() -> List[AgentPlugin]:
    return list(_registry.values())

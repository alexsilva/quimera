from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class AgentPlugin:
    name: str
    prefix: str
    cmd: List[str]
    style: Tuple[str, str]  # (color, label) para UI
    prompt_as_arg: bool = False  # se True, prompt é passado como argumento CLI em vez de stdin


_registry: dict[str, AgentPlugin] = {}


def register(plugin: AgentPlugin) -> None:
    _registry[plugin.name] = plugin


def get(name: str) -> Optional[AgentPlugin]:
    return _registry.get(name)


def all_names() -> List[str]:
    return list(_registry.keys())


def all_plugins() -> List[AgentPlugin]:
    return list(_registry.values())

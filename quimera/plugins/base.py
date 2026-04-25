"""Componentes de `quimera.plugins.base`."""
import json
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, FrozenSet, List, Optional, Tuple, Union

from ..paths import CANDIDATE_DIRS, find_base_writable
from quimera.agent_events import SpyEvent


SpyFormatterOutput = List[SpyEvent]


@dataclass
class CliConnection:
    """Conexão via CLI local."""
    cmd: List[str] = field(default_factory=list)
    prompt_as_arg: bool = False
    output_format: Optional[str] = None
    env: Optional[dict] = None
    cwd: Optional[str] = None


@dataclass
class OpenAIConnection:
    """Conexão via API OpenAI-compatible."""
    model: str = "gpt-4o"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    provider: str = "openai"
    supports_native_tools: bool = True


Connection = Union[CliConnection, OpenAIConnection]


def _get_connections_file() -> Path:
    """Retorna o arquivo de conexões persistidas."""
    base = find_base_writable(CANDIDATE_DIRS)
    return base / "connections.json"


def load_connections() -> dict:
    """Carrega conexões persistidas."""
    f = _get_connections_file()
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {}


def save_connections(connections: dict) -> None:
    """Salva conexões persistidas."""
    f = _get_connections_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(connections, indent=2, ensure_ascii=False), encoding="utf-8")


def get_connection_overrides() -> dict[str, dict]:
    """Retorna overrides de conexão persistidos por agente."""
    return load_connections()


def _connection_from_dict(data: dict) -> Connection:
    """Converte dict em Connection."""
    if data.get("type") == "cli":
        raw_cmd = data.get("cmd", [])
        if isinstance(raw_cmd, str):
            raw_cmd = shlex.split(raw_cmd)
        elif isinstance(raw_cmd, list) and len(raw_cmd) == 1 and isinstance(raw_cmd[0], str) and " " in raw_cmd[0]:
            raw_cmd = shlex.split(raw_cmd[0])
        return CliConnection(
            cmd=raw_cmd,
            prompt_as_arg=data.get("prompt_as_arg", False),
            output_format=data.get("output_format"),
            env=data.get("env"),
            cwd=data.get("cwd"),
        )
    return OpenAIConnection(
        model=data.get("model", "gpt-4o"),
        base_url=data.get("base_url", "https://api.openai.com/v1"),
        api_key_env=data.get("api_key_env", "OPENAI_API_KEY"),
        provider=data.get("provider", "openai"),
        supports_native_tools=data.get("supports_native_tools", True),
    )


def connection_to_dict(connection: Connection) -> dict:
    """Serializa uma conexão para persistência."""
    if isinstance(connection, CliConnection):
        data = asdict(connection)
        data["type"] = "cli"
        return data
    data = asdict(connection)
    data["type"] = "openai"
    return data


def apply_connection_overrides() -> None:
    """Aplica conexões persistidas aos plugins registrados."""
    overrides = get_connection_overrides()
    for name, conn_data in overrides.items():
        if _registry.get(name) is None:
            try:
                register_dynamic_plugin(name, metadata=conn_data.get("plugin"))
            except ValueError:
                continue
        plugin = _registry.get(name)
        if plugin is None:
            continue
        conn = _connection_from_dict(conn_data)
        object.__setattr__(plugin, "_connection_override", conn)


def reload_plugins() -> list:
    """Recarrega plugins e retorna nomes disponibles."""
    apply_connection_overrides()
    return all_names()


def set_connection_override(agent_name: str, connection: Connection, persist: bool = True) -> None:
    """Aplica um override de conexão em memória e opcionalmente persiste."""
    plugin = _registry.get(agent_name)
    if plugin is not None:
        object.__setattr__(plugin, "_connection_override", connection)
    if persist:
        connections = load_connections()
        payload = connection_to_dict(connection)
        if plugin is not None and getattr(plugin, "dynamic", False):
            plugin_meta: dict = {
                "dynamic": True,
                "prefix": plugin.prefix,
                "style": list(plugin.style),
                "icon": plugin.icon,
                "capabilities": list(plugin.capabilities),
                "preferred_task_types": list(plugin.preferred_task_types),
                "supports_tools": plugin.supports_tools,
                "has_builtin_tools": plugin.has_builtin_tools,
                "tool_use_reliability": plugin.tool_use_reliability,
                "supports_code_editing": plugin.supports_code_editing,
                "supports_long_context": plugin.supports_long_context,
                "supports_task_execution": plugin.supports_task_execution,
                "base_tier": plugin.base_tier,
                "runtime_rw_paths": list(plugin.runtime_rw_paths),
            }
            # Preserve base plugin reference for formatter inheritance on reload
            base_ref = (
                getattr(plugin, "_base_plugin_name", None)
                or connections.get(agent_name, {}).get("plugin", {}).get("base")
            )
            if base_ref:
                plugin_meta["base"] = base_ref
            payload["plugin"] = plugin_meta
        connections[agent_name] = payload
        save_connections(connections)


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
    has_builtin_tools: bool = False  # True = agente executa tools internamente (não precisa <tool ...> no chat)
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
    dynamic: bool = False
    # Connection override (carregado automaticamente do base_dir)
    _connection_override: Optional[Connection] = field(default=None, repr=False)
    # Base plugin name (usado para herança de formatter/rw_paths em plugins dinâmicos)
    _base_plugin_name: Optional[str] = field(default=None, repr=False)

    @property
    def render_style(self) -> Tuple[str, str]:
        """Retorna o estilo pronto para renderização na UI."""
        color, label = self.style
        return (color, f"{self.icon} {label}")

    def effective_connection(self) -> Optional[Connection]:
        """Retorna a conexão efetiva, priorizando override persistido."""
        if self._connection_override is not None:
            return self._connection_override
        if isinstance(self.driver, str) and self.driver != "cli":
            return OpenAIConnection(
                model=self.model or "gpt-4o",
                base_url=self.base_url or "https://api.openai.com/v1",
                api_key_env=self.api_key_env or "OPENAI_API_KEY",
                provider=self.driver,
                supports_native_tools=self.supports_tools,
            )
        return CliConnection(
            cmd=list(self.cmd),
            prompt_as_arg=self.prompt_as_arg,
            output_format=self.output_format,
        )

    def effective_driver(self) -> str:
        """Retorna o driver da conexão efetiva."""
        connection = self.effective_connection()
        if isinstance(connection, OpenAIConnection):
            return connection.provider or "openai_compat"
        return "cli"

    def effective_cmd(self) -> list[str]:
        """Retorna o comando CLI efetivo."""
        connection = self.effective_connection()
        if isinstance(connection, CliConnection):
            return list(connection.cmd)
        return list(self.cmd)

    def effective_prompt_as_arg(self) -> bool:
        """Retorna se o prompt deve ser enviado como argumento."""
        connection = self.effective_connection()
        if isinstance(connection, CliConnection):
            return connection.prompt_as_arg
        return self.prompt_as_arg

    def effective_output_format(self) -> Optional[str]:
        """Retorna o formato de saída efetivo."""
        connection = self.effective_connection()
        if isinstance(connection, CliConnection):
            return connection.output_format
        return self.output_format

    def effective_model(self) -> Optional[str]:
        """Retorna o modelo efetivo de drivers compatíveis com OpenAI."""
        connection = self.effective_connection()
        if isinstance(connection, OpenAIConnection):
            return connection.model
        return self.model

    def effective_base_url(self) -> Optional[str]:
        """Retorna a base URL efetiva."""
        connection = self.effective_connection()
        if isinstance(connection, OpenAIConnection):
            return connection.base_url
        return self.base_url

    def effective_api_key_env(self) -> Optional[str]:
        """Retorna o nome da variável de ambiente efetiva."""
        connection = self.effective_connection()
        if isinstance(connection, OpenAIConnection):
            return connection.api_key_env
        return self.api_key_env


def format_connection_label(connection: Connection) -> str:
    """Retorna uma descrição curta para UI/CLI."""
    if isinstance(connection, CliConnection):
        cmd = shlex.join(connection.cmd) if connection.cmd else "(sem comando)"
        return f"cli: {cmd}"
    return f"{connection.provider}: model={connection.model} base_url={connection.base_url}"


_registry: dict[str, AgentPlugin] = {}


_DYNAMIC_AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def is_valid_agent_name(name: str) -> bool:
    """Valida nomes canônicos de agentes dinâmicos."""
    return bool(name and _DYNAMIC_AGENT_RE.fullmatch(name))


def _humanize_agent_name(name: str) -> str:
    """Gera um label legível a partir do nome canônico."""
    parts = re.split(r"[-_]+", name)
    return " ".join(part.capitalize() for part in parts if part) or name


def _dynamic_plugin_metadata(name: str) -> dict:
    """Retorna metadados padrão para agentes registrados via conexão."""
    return {
        "dynamic": True,
        "prefix": f"/{name}",
        "style": ("bright_cyan", _humanize_agent_name(name)),
        "icon": "🧩",
        "capabilities": ["general", "code_edit", "code_review", "bug_investigation", "test_execution", "tool_use"],
        "preferred_task_types": ["general", "code_edit", "code_review", "bug_investigation", "test_execution"],
        "supports_tools": True,
        "tool_use_reliability": "medium",
        "supports_code_editing": True,
        "supports_long_context": True,
        "supports_task_execution": True,
        "base_tier": 2,
    }


def register_dynamic_plugin(name: str, connection: Connection | None = None, metadata: dict | None = None) -> AgentPlugin:
    """Cria ou atualiza um plugin dinâmico e o registra no registry."""
    normalized = (name or "").strip().lower()
    if not is_valid_agent_name(normalized):
        raise ValueError(f"Nome de agente inválido: {name}")

    plugin_data = _dynamic_plugin_metadata(normalized)
    if metadata:
        plugin_data.update(metadata)

    # Inherit non-serializable fields (spy formatter) from a named base plugin
    base_name = plugin_data.pop("base", None)
    if base_name:
        base = _registry.get(base_name)
        if base is not None:
            plugin_data.setdefault("spy_stdout_formatter", base.spy_stdout_formatter)
            plugin_data.setdefault("has_builtin_tools", base.has_builtin_tools)
            plugin_data.setdefault("runtime_rw_paths", list(base.runtime_rw_paths))

    prefix = plugin_data.pop("prefix", f"/{normalized}")
    style = tuple(plugin_data.pop("style", ("bright_cyan", _humanize_agent_name(normalized))))
    plugin = AgentPlugin(name=normalized, prefix=prefix, style=style, **plugin_data)

    # Re-attach base reference so set_connection_override can persist it
    if base_name:
        object.__setattr__(plugin, "_base_plugin_name", base_name)

    if connection is not None:
        object.__setattr__(plugin, "_connection_override", connection)
    _registry[normalized] = plugin
    return plugin


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

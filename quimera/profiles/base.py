"""Componentes de `quimera.profiles.base`."""
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
    keep_stdin_open: bool = False


@dataclass
class OpenAIConnection:
    """Conexão via API OpenAI-compatible."""
    model: str = "gpt-4o"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    provider: str = "openai"
    supports_native_tools: bool = True
    extra_body: Optional[dict] = None
    max_connections: int = 4
    """Número máximo de conexões concorrentes ao backend para este agente.
    Evita estouro de rate-limit quando múltiplos agentes chamam a API em paralelo."""
    """Parâmetros extras mesclados no corpo da requisição à API.
    Exemplo para DeepSeek: {"thinking": {"type": "enabled"}}.
    None significa não enviar extra_body (comportamento padrão da API)."""


Connection = Union[CliConnection, OpenAIConnection]


def extract_model_from_cli_cmd(cmd) -> Optional[str]:
    """Extrai model id de argumentos CLI como --model=<id> e --model <id>."""
    if not cmd:
        return None
    try:
        args = [str(part).strip() for part in cmd if str(part).strip()]
    except Exception:
        return None

    for idx, arg in enumerate(args):
        if arg.startswith("--model="):
            model = arg.split("=", 1)[1].strip()
            if model:
                return model
        if arg.startswith("-m="):
            model = arg.split("=", 1)[1].strip()
            if model:
                return model
        if arg in {"--model", "-m"} and idx + 1 < len(args):
            model = args[idx + 1].strip()
            if model and not model.startswith("-"):
                return model
    return None


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


def get_connections() -> dict[str, dict]:
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
            keep_stdin_open=data.get("keep_stdin_open", False),
        )
    return OpenAIConnection(
        model=data.get("model", "gpt-4o"),
        base_url=data.get("base_url", "https://api.openai.com/v1"),
        api_key_env=data.get("api_key_env", "OPENAI_API_KEY"),
        provider=data.get("provider", "openai"),
        supports_native_tools=data.get("supports_native_tools", True),
        extra_body=data.get("extra_body"),
        max_connections=data.get("max_connections", 4),
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


def _resolve_registry(registry=None):
    """Retorna o registry explicitamente fornecido ou o global do módulo."""
    return registry if registry is not None else _registry


def apply_connections(registry=None, exclude_names: set[str] | frozenset[str] | None = None) -> None:
    """Aplica conexões persistidas aos perfis registrados."""
    target_registry = _resolve_registry(registry)
    excluded = {str(name).strip().lower() for name in (exclude_names or set())}
    overrides = get_connections()
    for name, conn_data in overrides.items():
        if str(name).strip().lower() in excluded:
            continue
        existing = target_registry.get(name)
        if existing is not None and not getattr(existing, "dynamic", False):
            continue
        if existing is None:
            try:
                register_connection_profile(name, metadata=conn_data.get("profile"), registry=target_registry)
            except ValueError:
                continue
        profile = target_registry.get(name)
        if profile is None:
            continue
        conn = _connection_from_dict(conn_data)
        object.__setattr__(profile, "_connection_override", conn)


def reload_profiles(registry=None) -> list:
    """Recarrega perfis e retorna nomes disponibles."""
    target_registry = _resolve_registry(registry)
    apply_connections(registry=target_registry)
    return target_registry.all_names()


def set_connection(agent_name: str, connection: Connection, persist: bool = True, registry=None) -> None:
    """Aplica um override de conexão em memória e opcionalmente persiste."""
    target_registry = _resolve_registry(registry)
    profile = target_registry.get(agent_name)
    if profile is not None:
        object.__setattr__(profile, "_connection_override", connection)
    if persist:
        connections = load_connections()
        payload = connection_to_dict(connection)
        if profile is not None and getattr(profile, "dynamic", False):
            profile_meta: dict = {
                "dynamic": True,
                "prefix": profile.prefix,
                "style": list(profile.style),
                "icon": profile.icon,
                "capabilities": list(profile.capabilities),
                "preferred_task_types": list(profile.preferred_task_types),
                "supports_tools": profile.supports_tools,
                "has_builtin_tools": profile.has_builtin_tools,
                "tool_use_reliability": profile.tool_use_reliability,
                "supports_code_editing": profile.supports_code_editing,
                "supports_long_context": profile.supports_long_context,
                "supports_task_execution": profile.supports_task_execution,
                "supports_warm_pool": profile.supports_warm_pool,
                "base_tier": profile.base_tier,
                "runtime_rw_paths": list(profile.runtime_rw_paths),
            }
            # Preserve profile reference for formatter inheritance on reload
            base_ref = (
                getattr(profile, "_profile_name", None)
                or connections.get(agent_name, {}).get("profile", {}).get("profile")
            )
            if base_ref:
                profile_meta["profile"] = base_ref
            payload["profile"] = profile_meta
        connections[agent_name] = payload
        save_connections(connections)


def remove_connection(agent_name: str, registry=None) -> bool:
    """Remove a conexão persistida de um agente.

    Args:
        agent_name: Nome do agente cuja conexão será removida.

    Returns:
        True se a conexão existia e foi removida, False se não existia.
    """
    connections = load_connections()
    if agent_name not in connections:
        return False
    del connections[agent_name]
    save_connections(connections)
    # Remove o override em memória se o perfil estiver registrado
    profile = _resolve_registry(registry).get(agent_name)
    if profile is not None:
        object.__setattr__(profile, "_connection_override", None)
    return True


@dataclass
class ExecutionProfile:
    """Implementa `ExecutionProfile`."""
    name: str
    prefix: str
    style: Tuple[str, str]  # (color, label) para UI
    aliases: List[str] = field(default_factory=list)
    icon: str = "🤖"
    runtime_rw_paths: List[str] = field(default_factory=list)
    # CLI fields
    cmd: List[str] = field(default_factory=list)
    prompt_as_arg: bool = False  # se True, prompt é passado como argumento CLI em vez de stdin
    keep_stdin_open: bool = False
    # Agent capabilities
    capabilities: List[str] = field(default_factory=list)
    preferred_task_types: List[str] = field(default_factory=list)
    avoid_task_types: List[str] = field(default_factory=list)
    supports_tools: bool = True
    has_builtin_tools: bool = False  # True = agente usa tool calling nativo do próprio driver
    tool_use_reliability: str = "medium"
    supports_code_editing: bool = False
    supports_long_context: bool = False
    supports_task_execution: bool = True
    supports_warm_pool: bool = True
    base_tier: int = 2  # 1: weak, 2: standard, 3: premium
    # API driver fields (driver != "cli" ignora cmd e usa a API diretamente)
    driver: str = "cli"  # "cli" | "openai_compat"
    output_format: Optional[str] = None  # "stream-json" para parsear output estruturado do CLI
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None  # nome da variável de ambiente com a API key
    spy_stdout_formatter: Optional[Callable[[str], SpyFormatterOutput]] = None
    stderr_noise: FrozenSet[str] = field(default_factory=frozenset)
    stderr_noise_patterns: Tuple[str, ...] = field(default_factory=tuple)
    dynamic: bool = False
    # Connection override (carregado automaticamente do base_dir)
    _connection_override: Optional[Connection] = field(default=None, repr=False)
    # Profile name (usado para herança de formatter/rw_paths em perfis de conexão)
    _profile_name: Optional[str] = field(default=None, repr=False)
    # Socket MCP do servidor local do Quimera (quando habilitado por --mcp).
    _mcp_socket_path: Optional[str] = field(default=None, repr=False)
    _mcp_http_url: Optional[str] = field(default=None, repr=False)
    # Token de autenticação gerado por sessão (não persistir, não logar).
    _mcp_token: Optional[str] = field(default=None, repr=False)

    @property
    def render_style(self) -> Tuple[str, str]:
        """Retorna o estilo pronto para renderização na UI."""
        color, label = self.style
        return (color, f"{self.icon}  {label}")

    def configure_with_model(self, model_id: str) -> "CliConnection":
        """Retorna nova CliConnection com model_id substituído no placeholder --model=."""
        conn = self.effective_connection()
        if not isinstance(conn, CliConnection):
            raise ValueError(f"Profile '{self.name}' não usa driver CLI.")
        if not (model_id or "").strip():
            raise ValueError("model_id não pode ser vazio.")
        if not any(arg.startswith("--model=") for arg in conn.cmd):
            raise ValueError(f"Profile '{self.name}' não tem placeholder --model= no cmd.")
        new_cmd = [
            f"--model={model_id}" if arg.startswith("--model=") else arg
            for arg in conn.cmd
        ]
        return CliConnection(
            cmd=new_cmd,
            prompt_as_arg=conn.prompt_as_arg,
            output_format=conn.output_format,
            env=conn.env,
            cwd=conn.cwd,
            keep_stdin_open=conn.keep_stdin_open,
        )

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
            keep_stdin_open=self.keep_stdin_open,
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
            return self._with_mcp_server_args(connection.cmd)
        return self._with_mcp_server_args(self.cmd)

    def set_mcp_socket_path(self, socket_path: Optional[str]) -> None:
        """Configura socket MCP injetado pelo runtime para esse perfil.

        Ao remover o socket (path None/vazio), o token também é limpo para
        evitar vazamento de estado entre reconfigurações.
        """
        normalized = (socket_path or "").strip()
        self._mcp_socket_path = normalized or None
        if self._mcp_socket_path is None and self._mcp_http_url is None:
            self._mcp_token = None

    def set_mcp_socket_config(self, socket_path: Optional[str], token: Optional[str]) -> None:
        """Configura socket MCP e token de autenticação para esse perfil."""
        self.set_mcp_socket_path(socket_path)
        normalized = (token or "").strip()
        self._mcp_token = normalized or None

    def set_mcp_http_config(self, url: Optional[str], token: Optional[str]) -> None:
        """Armazena endpoint e token MCP HTTP quando não há socket MCP ativo."""
        # Agentes CLI locais não usam este endpoint; eles sempre preferem o
        # socket interno configurado por ``set_mcp_socket_config``. O token HTTP
        # só é armazenado quando não há socket ativo, evitando sobrescrever o
        # token interno dos agentes locais em fluxos de compatibilidade.
        normalized = (url or "").strip()
        self._mcp_http_url = normalized or None
        if self._mcp_socket_path is None:
            normalized_token = (token or "").strip()
            self._mcp_token = normalized_token or None

    def _build_token_args(self) -> List[str]:
        """Retorna ['--token', <token>] se houver token, ou lista vazia.

        Centraliza a lógica de inclusão do token no comando do proxy MCP,
        eliminando duplicação em Codex, Claude e OpenCode.
        """
        token = (self._mcp_token or "").strip()
        if token:
            return ["--token", token]
        return []

    def env_for_cli(self) -> dict:
        """Retorna variáveis de ambiente extras a injetar no subprocess CLI.

        Chamado pelo AgentClient imediatamente antes de lançar o processo.
        Subclasses sobrescrevem para comportamento dinâmico de runtime.
        Não deve modificar os.environ — apenas retornar um dict plano.
        """
        return {}

    def format_stdin_input(self, prompt) -> str:
        """Transforma o prompt antes de enviá-lo ao stdin do CLI."""
        return prompt

    def mcp_server_args(self, socket_path: str) -> list[str]:
        """Retorna args CLI para conectar no MCP local (default: sem suporte)."""
        _ = socket_path
        return []

    def mcp_http_server_args(self, url: str) -> list[str]:
        """Retorna a lista padrão vazia de argumentos CLI para MCP HTTP."""
        _ = url
        return []

    def _with_mcp_server_args(self, cmd: list[str]) -> list[str]:
        """Anexa argumentos de MCP quando o perfil fornece integração."""
        base_cmd = list(cmd)
        socket_path = (self._mcp_socket_path or "").strip()
        if not socket_path:
            return base_cmd
        if any(
            part in ("--mcp-server", "--mcp-config") or str(part).startswith(("--mcp-server=", "--mcp-config="))
            for part in base_cmd
        ):
            return base_cmd
        mcp_args = self.mcp_server_args(socket_path)
        if not mcp_args:
            return base_cmd
        if base_cmd and base_cmd[-1] == "-":
            return [*base_cmd[:-1], *mcp_args, base_cmd[-1]]
        return [*base_cmd, *mcp_args]

    def resolve_runtime_model(self, *, cwd: Optional[str] = None) -> Optional[str]:
        """Resolve modelo em runtime para CLIs; perfis podem sobrescrever."""
        _ = cwd
        return extract_model_from_cli_cmd(self.effective_cmd())

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
    label = f"{connection.provider}: model={connection.model} base_url={connection.base_url}"
    if connection.extra_body:
        label += f" extra_body={json.dumps(connection.extra_body, ensure_ascii=False)}"
    return label


class ProfileRegistry:
    """Registry for ExecutionProfile instances."""

    def __init__(self):
        self._profiles: dict[str, ExecutionProfile] = {}

    def register(self, profile: ExecutionProfile) -> None:
        self._profiles[profile.name] = profile

    def get(self, name: str) -> Optional[ExecutionProfile]:
        return self._profiles.get(name)

    def all_names(self) -> List[str]:
        return list(self._profiles.keys())

    def all_profiles(self) -> List[ExecutionProfile]:
        return list(self._profiles.values())


_registry = ProfileRegistry()


_CONNECTION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SAFE_CONNECTION_PROFILE_METADATA_KEYS = frozenset({
    "avoid_task_types",
    "profile",
    "base_tier",
    "capabilities",
    "dynamic",
    "has_builtin_tools",
    "icon",
    "prefix",
    "preferred_task_types",
    "runtime_rw_paths",
    "style",
    "supports_code_editing",
    "supports_long_context",
    "supports_task_execution",
    "supports_tools",
    "supports_warm_pool",
    "tool_use_reliability",
})


def is_valid_agent_name(name: str) -> bool:
    """Valida nomes canônicos de agentes dinâmicos."""
    return bool(name and _CONNECTION_NAME_RE.fullmatch(name))


def _humanize_agent_name(name: str) -> str:
    """Gera um label legível a partir do nome canônico."""
    parts = re.split(r"[-_]+", name)
    return " ".join(part.capitalize() for part in parts if part) or name


def _connection_profile_metadata(name: str) -> dict:
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
        "supports_warm_pool": True,
        "base_tier": 2,
    }


def _sanitize_connection_profile_metadata(metadata: dict | None) -> dict:
    """Mantém apenas metadados persistíveis e seguros para perfis de conexão."""
    if not isinstance(metadata, dict):
        return {}
    return {
        key: value
        for key, value in metadata.items()
        if key in _SAFE_CONNECTION_PROFILE_METADATA_KEYS
    }


def _inherit_execution_profile_config(profile_data: dict, execution_profile: "ExecutionProfile") -> None:
    """Mescla contrato visual/runtime do perfil de execução na conexão nomeada."""
    current_style = tuple(profile_data.get("style") or ())
    current_label = current_style[1] if len(current_style) > 1 else None
    profile_data.update({
        "icon": execution_profile.icon,
        "style": (execution_profile.style[0], current_label or execution_profile.style[1]),
        "cmd": list(execution_profile.cmd),
        "prompt_as_arg": execution_profile.prompt_as_arg,
        "output_format": execution_profile.output_format,
        "capabilities": list(execution_profile.capabilities),
        "preferred_task_types": list(execution_profile.preferred_task_types),
        "avoid_task_types": list(execution_profile.avoid_task_types),
        "supports_tools": execution_profile.supports_tools,
        "has_builtin_tools": execution_profile.has_builtin_tools,
        "tool_use_reliability": execution_profile.tool_use_reliability,
        "supports_code_editing": execution_profile.supports_code_editing,
        "supports_long_context": execution_profile.supports_long_context,
        "supports_task_execution": execution_profile.supports_task_execution,
        "supports_warm_pool": execution_profile.supports_warm_pool,
        "base_tier": execution_profile.base_tier,
        "driver": execution_profile.driver,
        "model": execution_profile.model,
        "base_url": execution_profile.base_url,
        "api_key_env": execution_profile.api_key_env,
        "runtime_rw_paths": list(execution_profile.runtime_rw_paths),
        "spy_stdout_formatter": execution_profile.spy_stdout_formatter,
        "stderr_noise": execution_profile.stderr_noise,
        "stderr_noise_patterns": tuple(execution_profile.stderr_noise_patterns),
    })


def register_connection_profile(
    name: str,
    connection: Connection | None = None,
    metadata: dict | None = None,
    registry: ProfileRegistry | None = None,
) -> ExecutionProfile:
    """Cria ou atualiza um perfil de conexão e o registra no registry."""
    target_registry = _resolve_registry(registry)
    normalized = (name or "").strip().lower()
    if not is_valid_agent_name(normalized):
        raise ValueError(f"Nome de agente inválido: {name}")
    existing = target_registry.get(normalized)
    if existing is not None and not getattr(existing, "dynamic", False):
        raise ValueError(f"Nome de conexão conflita com perfil de execução: {name}")

    profile_data = _connection_profile_metadata(normalized)
    profile_data.update(_sanitize_connection_profile_metadata(metadata))

    # Inherit non-serializable fields (spy formatter) from a named profile
    profile_name = profile_data.pop("profile", None)
    profile_cls: type[ExecutionProfile] = ExecutionProfile
    if profile_name:
        profile = target_registry.get(profile_name)
        if profile is not None:
            profile_cls = type(profile)
            _inherit_execution_profile_config(profile_data, profile)

    prefix = profile_data.pop("prefix", f"/{normalized}")
    style = tuple(profile_data.pop("style", ("bright_cyan", _humanize_agent_name(normalized))))
    profile = profile_cls(name=normalized, prefix=prefix, style=style, **profile_data)

    # Re-attach base reference so set_connection can persist it
    if profile_name:
        object.__setattr__(profile, "_profile_name", profile_name)

    if connection is not None:
        object.__setattr__(profile, "_connection_override", connection)
    target_registry.register(profile)
    return profile


def register(profile: ExecutionProfile) -> None:
    """Executa register."""
    _registry.register(profile)


def get(name: str) -> Optional[ExecutionProfile]:
    """Retorna get."""
    return _registry.get(name)


def all_names() -> List[str]:
    """Executa all names."""
    return _registry.all_names()


def all_profiles() -> List[ExecutionProfile]:
    """Executa all profiles."""
    return _registry.all_profiles()

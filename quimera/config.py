"""Componentes de `quimera.config`."""
from pathlib import Path

from .config_store import read_json_object, update_json_object, write_json_object

from .themes import DEFAULT_THEME, DEFAULT_DENSITY, DENSITY_OPTIONS, names as theme_names

DEFAULT_USER_NAME = ">>>"
DEFAULT_HISTORY_WINDOW = 12
DEFAULT_AUTO_SUMMARIZE_THRESHOLD = 30
DEFAULT_IDLE_TIMEOUT_SECONDS = 360
DEFAULT_MAX_AGENT_EXECUTION_SECONDS = 3600
DEFAULT_MAX_CONVERSATION_ENTRY_CHARS = 8000
DEFAULT_MAX_PROMPT_CHARS = 128000
DEFAULT_WORKSPACE_POLICY = "strict"
WORKSPACE_POLICY_PRESETS = {"strict", "developer", "autonomous"}
DEFAULT_VISIBILITY = "summary"
VISIBILITY_OPTIONS = {"quiet", "summary", "full"}
DEFAULT_THREADS = 1
_MCP_HTTP_UNSET = object()


class ConfigManager:
    """Lê e grava um arquivo JSON de configuração global ou por workspace."""

    def __init__(self, path):
        """Inicializa uma instância de ConfigManager."""
        self._path = Path(path)

    def _load(self) -> dict:
        """Carrega load."""
        return read_json_object(self._path)

    def _save(self, data: dict):
        """Persiste save."""
        write_json_object(self._path, data)

    def _update(self, **values):
        def update(data):
            for key, value in values.items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
        update_json_object(self._path, update)

    @property
    def user_name(self) -> str:
        """Executa user name."""
        value = self._load().get("user_name")
        return value if isinstance(value, str) and value else DEFAULT_USER_NAME

    @property
    def history_window(self) -> int:
        """Executa history window."""
        value = self._load().get("history_window")
        if type(value) is int and value > 0:
            return value
        return DEFAULT_HISTORY_WINDOW

    @property
    def auto_summarize_threshold(self) -> int:
        """Executa auto summarize threshold."""
        value = self._load().get("auto_summarize_threshold")
        if type(value) is int and value > 0:
            return value
        return self.history_window * 2

    def set_auto_summarize_threshold(self, value: int | None):
        """Define auto summarize threshold."""
        self._update(auto_summarize_threshold=value if type(value) is int and value > 0 else None)

    @property
    def idle_timeout_seconds(self) -> int:
        """Executa idle timeout seconds."""
        value = self._load().get("idle_timeout_seconds")
        if type(value) is int and value > 0:
            return value
        return DEFAULT_IDLE_TIMEOUT_SECONDS

    def set_idle_timeout_seconds(self, value: int | None):
        """Define idle timeout seconds."""
        self._update(idle_timeout_seconds=value if type(value) is int and value > 0 else None)

    @property
    def max_agent_execution_seconds(self) -> int:
        """Retorna o tempo máximo total permitido para uma execução de agente."""
        value = self._load().get("max_agent_execution_seconds")
        if type(value) is int and value > 0:
            return value
        return DEFAULT_MAX_AGENT_EXECUTION_SECONDS

    def set_max_agent_execution_seconds(self, value: int | None) -> None:
        """Persiste o tempo máximo total permitido para uma execução de agente."""
        self._update(
            max_agent_execution_seconds=(
                value if type(value) is int and value > 0 else None
            )
        )

    @property
    def workflow_limits(self) -> dict:
        """Limites operacionais de workflows; só chaves com inteiros positivos são aceitas."""
        raw = self._load().get("workflow_limits")
        if not isinstance(raw, dict):
            return {}
        return {key: value for key, value in raw.items() if type(value) is int and value > 0}

    @property
    def workspace_policy(self) -> str:
        """Retorna o preset de policy do workspace."""
        value = str(self._load().get("workspace_policy") or "").strip().lower()
        if value in WORKSPACE_POLICY_PRESETS:
            return value
        return DEFAULT_WORKSPACE_POLICY

    def set_workspace_policy(self, value: str | None):
        """Persiste o preset de policy do workspace."""
        normalized = str(value or "").strip().lower()
        self._update(workspace_policy=normalized if normalized in WORKSPACE_POLICY_PRESETS else None)

    @property
    def sandbox_enabled(self) -> bool:
        """Retorna se o sandbox de confinamento ao workspace está ativo."""
        return self._load().get("sandbox_enabled") is True

    def set_sandbox_enabled(self, value: bool | None):
        """Persiste o flag do sandbox; False/None remove a chave."""
        self._update(sandbox_enabled=True if value else None)

    def set_user_name(self, name: str):
        """Define user name."""
        self._update(user_name=name or None)

    def set_history_window(self, value: int | None):
        """Define history window."""
        self._update(history_window=value if type(value) is int and value > 0 else None)

    @property
    def theme(self) -> str:
        """Retorna o tema ativo; fallback para o padrão."""
        value = self._load().get("theme")
        if value and value in theme_names():
            return value
        return DEFAULT_THEME

    def set_theme(self, name: str):
        """Persiste o tema padrão."""
        self._update(theme=name if name and name in theme_names() else None)

    @property
    def density(self) -> str:
        """Retorna a densidade de layout ativa; fallback para o padrão."""
        value = self._load().get("density")
        if isinstance(value, str) and value in DENSITY_OPTIONS:
            return value
        return DEFAULT_DENSITY

    def set_density(self, value: str):
        """Persiste a densidade de layout."""
        self._update(density=value if isinstance(value, str) and value in DENSITY_OPTIONS else None)

    @property
    def visibility(self) -> str:
        """Retorna o nível de visibilidade persistido; fallback para o padrão."""
        value = str(self._load().get("visibility") or "").strip().lower()
        if value in VISIBILITY_OPTIONS:
            return value
        return DEFAULT_VISIBILITY

    def set_visibility(self, value: str | None):
        """Persiste o nível de visibilidade da execução dos agentes."""
        normalized = str(value or "").strip().lower()
        self._update(visibility=normalized if normalized in VISIBILITY_OPTIONS else None)

    @property
    def threads(self) -> int:
        """Retorna o máximo de agentes processados em paralelo por rodada."""
        value = self._load().get("threads")
        if type(value) is int and value > 0:
            return value
        return DEFAULT_THREADS

    def set_threads(self, value: int | None):
        """Persiste o máximo de agentes em paralelo por rodada."""
        self._update(threads=value if type(value) is int and value > 0 else None)

    @property
    def selected_agents(self) -> list[str] | None:
        """Retorna a seleção de agentes persistida; None significa todos."""
        value = self._load().get("selected_agents")
        if isinstance(value, list) and value and all(isinstance(s, str) and s for s in value):
            return value
        return None

    def set_selected_agents(self, agents: list[str] | None):
        """Persiste a seleção de agentes ativa; lista vazia remove a chave."""
        cleaned = [str(a) for a in (agents or []) if isinstance(a, str) and a]
        self._update(selected_agents=cleaned or None)

    @property
    def frozen_agent(self) -> str | None:
        """Retorna o agente congelado (s/<agente>) persistido, se houver."""
        value = self._load().get("frozen_agent")
        return value if isinstance(value, str) and value else None

    @property
    def orchestrator_agent(self) -> str | None:
        """Retorna o agente orquestrador (o/<agente>) persistido, se houver."""
        value = self._load().get("orchestrator_agent")
        return value if isinstance(value, str) and value else None

    def set_agent_routing(self, frozen: str | None, orchestrator: str | None):
        """Persiste congelamento/orquestrador do pool na mesma escrita atômica."""
        self._update(
            frozen_agent=frozen or None,
            orchestrator_agent=orchestrator or None,
        )

    @property
    def resumer_agent(self) -> str | None:
        """Retorna o agente preferido para resumir o contexto, se configurado."""
        value = self._load().get("resumer_agent")
        return value if isinstance(value, str) and value else None

    def set_resumer_agent(self, value: str | None):
        """Persiste o agente preferido para resumir o contexto; None remove a preferência."""
        self._update(resumer_agent=value if isinstance(value, str) and value else None)

    @property
    def mcp_clients(self) -> list[str] | None:
        """Retorna specs de MCP client persistidos."""
        value = self._load().get("mcp_clients")
        if isinstance(value, list) and all(isinstance(s, str) for s in value):
            return value
        return None

    def set_mcp_clients(self, specs: list[str] | None):
        """Persiste specs de MCP client."""
        self._update(mcp_clients=specs or None)

    @property
    def mcp_client_env(self) -> list[str] | None:
        """Retorna env vars de MCP client persistidos."""
        value = self._load().get("mcp_client_env")
        if isinstance(value, list) and all(isinstance(s, str) for s in value):
            return value
        return None

    def set_mcp_client_env(self, specs: list[str] | None):
        """Persiste env vars de MCP client."""
        self._update(mcp_client_env=specs or None)

    def set_mcp_configuration(self, specs: list[str] | None, env_specs: list[str] | None) -> None:
        """Persiste servidores e ambiente na mesma escrita atomica."""
        self._update(mcp_clients=specs or None, mcp_client_env=env_specs or None)

    @property
    def mcp_active(self) -> list[str] | None:
        """Nomes de conexões do catálogo global ativadas neste workspace."""
        value = self._load().get("mcp_active")
        if isinstance(value, list) and all(isinstance(s, str) for s in value):
            return value
        return None

    def set_mcp_active(self, names: list[str] | None) -> None:
        """Persiste as conexões globais ativadas; lista vazia remove a chave."""
        self._update(mcp_active=list(names) if names else None)

    @property
    def mcp_inactive(self) -> list[str] | None:
        """Nomes de conexões globais desligadas só neste workspace, contra o padrão global."""
        value = self._load().get("mcp_inactive")
        if isinstance(value, list) and all(isinstance(s, str) for s in value):
            return value
        return None

    def set_mcp_http_clients_active(self, names: list[str] | None) -> None:
        """Persiste os clients OAuth liberados; lista vazia remove a chave."""
        self._update(http_clients_active=list(names) if names else None)

    @property
    def mcp_http_settings(self) -> dict:
        """Chaves do servidor MCP HTTP presentes no arquivo, validadas por tipo.

        Só o que está gravado entra no dicionário: a ausência de uma chave é o
        que permite a um escopo herdar o valor do outro.
        """
        data = self._load()
        values: dict = {}
        enabled = data.get("http_enabled")
        if isinstance(enabled, bool):
            values["enabled"] = enabled
        host = data.get("http_host")
        if isinstance(host, str) and host.strip():
            values["host"] = host.strip()
        port = data.get("http_port")
        if type(port) is int and 0 < port < 65536:
            values["port"] = port
        allow_tools = data.get("http_allow_tools")
        if isinstance(allow_tools, str) and allow_tools.strip():
            values["allow_tools"] = allow_tools.strip()
        return values

    def set_mcp_http_settings(
        self,
        *,
        enabled: bool | None | object = _MCP_HTTP_UNSET,
        host: str | None | object = _MCP_HTTP_UNSET,
        port: int | None | object = _MCP_HTTP_UNSET,
        allow_tools: str | None | object = _MCP_HTTP_UNSET,
    ) -> None:
        """Atualiza só as chaves fornecidas; ``None`` apaga uma chave."""
        values = {}
        if enabled is not _MCP_HTTP_UNSET:
            values["http_enabled"] = enabled
        if host is not _MCP_HTTP_UNSET:
            values["http_host"] = host or None
        if port is not _MCP_HTTP_UNSET:
            values["http_port"] = port
        if allow_tools is not _MCP_HTTP_UNSET:
            values["http_allow_tools"] = allow_tools or None
        if values:
            self._update(**values)

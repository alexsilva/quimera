"""Componentes de `quimera.config`."""
import json

from .themes import DEFAULT_THEME, DEFAULT_DENSITY, DENSITY_OPTIONS, names as theme_names

DEFAULT_USER_NAME = ">>>"
DEFAULT_HISTORY_WINDOW = 12
DEFAULT_AUTO_SUMMARIZE_THRESHOLD = 30
DEFAULT_IDLE_TIMEOUT_SECONDS = 360
DEFAULT_MAX_CONVERSATION_ENTRY_CHARS = 8000
DEFAULT_MAX_PROMPT_CHARS = 128000
DEFAULT_WORKSPACE_POLICY = "strict"
WORKSPACE_POLICY_PRESETS = {"strict", "developer", "autonomous"}


class ConfigManager:
    """Lê e grava configurações globais do usuário em ~/.local/share/quimera/config.json."""

    def __init__(self, path):
        """Inicializa uma instância de ConfigManager."""
        self._path = path

    def _load(self) -> dict:
        """Carrega load."""
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self, data: dict):
        """Persiste save."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @property
    def user_name(self) -> str:
        """Executa user name."""
        return self._load().get("user_name") or DEFAULT_USER_NAME

    @property
    def history_window(self) -> int:
        """Executa history window."""
        value = self._load().get("history_window")
        if isinstance(value, int) and value > 0:
            return value
        return DEFAULT_HISTORY_WINDOW

    @property
    def auto_summarize_threshold(self) -> int:
        """Executa auto summarize threshold."""
        value = self._load().get("auto_summarize_threshold")
        if isinstance(value, int) and value > 0:
            return value
        return self.history_window * 2

    def set_auto_summarize_threshold(self, value: int | None):
        """Define auto summarize threshold."""
        data = self._load()
        if isinstance(value, int) and value > 0:
            data["auto_summarize_threshold"] = value
        else:
            data.pop("auto_summarize_threshold", None)
        self._save(data)

    @property
    def idle_timeout_seconds(self) -> int:
        """Executa idle timeout seconds."""
        value = self._load().get("idle_timeout_seconds")
        if isinstance(value, int) and value > 0:
            return value
        return DEFAULT_IDLE_TIMEOUT_SECONDS

    def set_idle_timeout_seconds(self, value: int | None):
        """Define idle timeout seconds."""
        data = self._load()
        if isinstance(value, int) and value > 0:
            data["idle_timeout_seconds"] = value
        else:
            data.pop("idle_timeout_seconds", None)
        self._save(data)

    @property
    def workspace_policy(self) -> str:
        """Retorna o preset de policy do workspace."""
        value = str(self._load().get("workspace_policy") or "").strip().lower()
        if value in WORKSPACE_POLICY_PRESETS:
            return value
        return DEFAULT_WORKSPACE_POLICY

    def set_workspace_policy(self, value: str | None):
        """Persiste o preset de policy do workspace."""
        data = self._load()
        normalized = str(value or "").strip().lower()
        if normalized in WORKSPACE_POLICY_PRESETS:
            data["workspace_policy"] = normalized
        else:
            data.pop("workspace_policy", None)
        self._save(data)

    def set_user_name(self, name: str):
        """Define user name."""
        data = self._load()
        if name:
            data["user_name"] = name
        else:
            data.pop("user_name", None)
        self._save(data)

    def set_history_window(self, value: int | None):
        """Define history window."""
        data = self._load()
        if isinstance(value, int) and value > 0:
            data["history_window"] = value
        else:
            data.pop("history_window", None)
        self._save(data)

    @property
    def theme(self) -> str:
        """Retorna o tema ativo; fallback para o padrão."""
        value = self._load().get("theme")
        if value and value in theme_names():
            return value
        return DEFAULT_THEME

    def set_theme(self, name: str):
        """Persiste o tema padrão."""
        data = self._load()
        if name and name in theme_names():
            data["theme"] = name
        else:
            data.pop("theme", None)
        self._save(data)

    @property
    def density(self) -> str:
        """Retorna a densidade de layout ativa; fallback para o padrão."""
        value = self._load().get("density")
        if value and value in DENSITY_OPTIONS:
            return value
        return DEFAULT_DENSITY

    def set_density(self, value: str):
        """Persiste a densidade de layout."""
        data = self._load()
        if value and value in DENSITY_OPTIONS:
            data["density"] = value
        else:
            data.pop("density", None)
        self._save(data)

    @property
    def mcp_clients(self) -> list[str] | None:
        """Retorna specs de MCP client persistidos."""
        value = self._load().get("mcp_clients")
        if isinstance(value, list) and all(isinstance(s, str) for s in value):
            return value
        return None

    def set_mcp_clients(self, specs: list[str] | None):
        """Persiste specs de MCP client."""
        data = self._load()
        if specs:
            data["mcp_clients"] = specs
        else:
            data.pop("mcp_clients", None)
        self._save(data)

    @property
    def mcp_client_env(self) -> list[str] | None:
        """Retorna env vars de MCP client persistidos."""
        value = self._load().get("mcp_client_env")
        if isinstance(value, list) and all(isinstance(s, str) for s in value):
            return value
        return None

    def set_mcp_client_env(self, specs: list[str] | None):
        """Persiste env vars de MCP client."""
        data = self._load()
        if specs:
            data["mcp_client_env"] = specs
        else:
            data.pop("mcp_client_env", None)
        self._save(data)

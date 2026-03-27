import json
from pathlib import Path

from .workspace import QUIMERA_BASE

_CONFIG_FILE = QUIMERA_BASE / "config.json"
DEFAULT_USER_NAME = "Você"


class ConfigManager:
    """Lê e grava configurações globais do usuário em ~/.local/share/quimera/config.json."""

    def __init__(self):
        self._path = _CONFIG_FILE

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self, data: dict):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @property
    def user_name(self) -> str:
        return self._load().get("user_name") or DEFAULT_USER_NAME

    def set_user_name(self, name: str):
        data = self._load()
        if name:
            data["user_name"] = name
        else:
            data.pop("user_name", None)
        self._save(data)

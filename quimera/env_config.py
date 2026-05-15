"""Componentes de `quimera.env_config`."""
import os
from pathlib import Path


class EnvConfig:
    """Configurações de modelo como variáveis de ambiente em ~/.local/share/quimera/.env.

    O arquivo usa o formato KEY=VALUE por linha (padrão .env).
    Linhas vazias e linhas começando com '#' são ignoradas.
    """

    def __init__(self, path: Path):
        """Inicializa uma instância de EnvConfig apontando para *path* (arquivo ``.env``)."""
        self._path = path

    def _load(self) -> dict:
        """Lê e parseia o arquivo ``.env``, retornando um dict KEY→valor.

        Linhas vazias e comentários (``#``) são ignorados.
        Retorna dict vazio se o arquivo não existir.
        """
        data = {}
        if not self._path.exists():
            return data
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                data[key.strip()] = value
        return data

    def _save(self, data: dict) -> None:
        """Persiste *data* no arquivo ``.env`` no formato ``KEY=VALUE``, ordenado alfabeticamente."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{k}={v}" for k, v in sorted(data.items())]
        self._path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def get(self, key: str, default: str | None = None) -> str | None:
        """Retorna o valor da chave ou o default."""
        return self._load().get(key, default)

    def set(self, key: str, value: str) -> None:
        """Define e persiste o valor da chave."""
        data = self._load()
        data[key] = value
        self._save(data)

    def delete(self, key: str) -> None:
        """Remove a chave do arquivo, sem erro se não existir."""
        data = self._load()
        data.pop(key, None)
        self._save(data)

    def all(self) -> dict:
        """Retorna cópia do estado atual."""
        return dict(self._load())

    def apply_to_environ(self) -> None:
        """Carrega as chaves em os.environ sem sobrescrever variáveis já definidas."""
        for key, value in self._load().items():
            os.environ.setdefault(key, value)

    def setenv(self, key: str, value: str) -> None:
        """Persiste a chave e atualiza os.environ imediatamente."""
        self.set(key, value)
        os.environ[key] = value

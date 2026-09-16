"""Escopos de ambiente e segredos usados pelo runtime do Quimera.

Há duas fontes deliberadamente distintas:

- ``~/.local/share/quimera/secrets.env`` (e o legado ``.env``): segredos do
  próprio runtime. Eles são consumidos pelo Quimera, mas não devem ser
  propagados para agentes ou ferramentas shell.
- ``<workspace>/.quimera/.env``: ambiente operacional do projeto. Esse arquivo
  pertence ao workspace e é explicitamente disponibilizado aos agentes/tools.

O armazenamento interno por hash do ``Workspace`` não participa dessa decisão.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING

from . import profiles
from .env_config import EnvConfig

if TYPE_CHECKING:
    from .workspace import Workspace


def configured_provider_secret_keys() -> set[str]:
    """Retorna nomes de credenciais declarados pelos providers configurados.

    A lista é derivada dos profiles/connections ativos, não de uma allowlist ou
    denylist fixa. Isso cobre também chaves exportadas no ambiente do processo:
    uma credencial usada para autenticar um provider continua privada mesmo se
    o usuário a exportou antes de iniciar o Quimera. O workspace ainda pode
    reexpô-la deliberadamente em ``.quimera/.env``.
    """
    keys: set[str] = set()
    for profile in profiles.all_profiles():
        resolver = getattr(profile, "effective_api_key_env", None)
        if not callable(resolver):
            continue
        key = resolver()
        if isinstance(key, str) and key.strip():
            keys.add(key.strip())
    return keys


class RuntimeSecrets:
    """Resolve segredos privados do runtime sem copiá-los para ``os.environ``."""

    def __init__(
        self,
        workspace: Workspace | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.workspace = workspace
        self._environ = os.environ if environ is None else environ

    @property
    def files(self) -> tuple[Path, ...]:
        """Arquivos privados sempre resolvidos pelo Workspace atual."""
        if self.workspace is None:
            return ()
        return self.workspace.runtime_secret_files

    def _file_values(self) -> dict[str, str]:
        """Combina os arquivos na ordem definida pelo ``Workspace``."""
        values: dict[str, str] = {}
        for path in self.files:
            values.update(EnvConfig(path).all())
        return values

    def get(self, key: str, default: str | None = None) -> str | None:
        """Retorna setting privado; ambiente explícito do processo tem precedência."""
        if key in self._environ:
            return self._environ[key]
        return self._file_values().get(key, default)

    def file_keys(self) -> set[str]:
        """Nomes que vieram dos arquivos privados e não devem vazar a filhos."""
        return set(self._file_values())

    def existing_files(self) -> tuple[Path, ...]:
        """Arquivos privados existentes, usados para isolamento de filesystem."""
        return tuple(path for path in self.files if path.is_file())

    def apply_to_environ(
        self,
        environ: MutableMapping[str, str] | None = None,
    ) -> None:
        """Disponibiliza settings privados somente ao processo do Quimera.

        Mantém a compatibilidade do ``.env`` global legado com consumidores
        internos que ainda consultam ``os.environ``. Os ambientes entregues a
        agentes/tools continuam sendo reconstruídos por ``build_env_vars``
        e removem essas mesmas chaves antes de criar subprocessos controlados
        pelo modelo.
        """
        target = os.environ if environ is None else environ
        for key, value in self._file_values().items():
            target.setdefault(key, value)


def load_workspace_environment(workspace: Workspace | None) -> dict[str, str]:
    """Lê o ambiente operacional definido pela instância atual de workspace."""
    if workspace is None:
        return {}
    return EnvConfig(workspace.project_env_file).all()


def build_env_vars(
    base_environment: Mapping[str, str],
    *,
    workspace: Workspace | None = None,
    runtime_secrets: RuntimeSecrets | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Monta as variáveis de ambiente públicas entregues a um processo filho.

    Remove segredos privados que o processo principal do Quimera pode manter em
    ``os.environ`` por compatibilidade. Quando ``workspace`` é informado,
    aplica depois o ambiente operacional explicitamente visível ao
    processo. O chamador decide onde esse ambiente tratado será usado; esta
    função não executa processos nem aplica política de subprocesso.

    ``extra_env`` é aplicado por último e representa uma concessão explícita do
    chamador, como a configuração específica de uma conexão MCP.
    """
    secrets = runtime_secrets or RuntimeSecrets(workspace)
    env = dict(base_environment)
    private_keys = secrets.file_keys() | configured_provider_secret_keys()
    for key in private_keys:
        env.pop(key, None)
    env.update(load_workspace_environment(workspace))
    if extra_env:
        env.update(extra_env)
    return env


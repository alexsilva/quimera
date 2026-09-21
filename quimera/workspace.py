"""Componentes de `quimera.workspace`."""
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .paths import CANDIDATE_DIRS, find_base_writable

logger = logging.getLogger(__name__)

_SSH_NON_SECRET_FILES = frozenset({
    "authorized_keys",
    "config",
    "known_hosts",
    "known_hosts.old",
})

_HOME_SENSITIVE_FILES = (
    ".env",
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".aws/credentials",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".gem/credentials",
    ".m2/settings.xml",
    ".gradle/gradle.properties",
    ".nuget/NuGet/NuGet.Config",
    ".kube/config",
    ".docker/config.json",
    ".config/containers/auth.json",
    ".config/gh/hosts.yml",
    ".config/glab-cli/config.yml",
    ".config/rclone/rclone.conf",
    ".terraform.d/credentials.tfrc.json",
    ".config/gcloud/application_default_credentials.json",
    ".config/gcloud/credentials.db",
    ".config/gcloud/access_tokens.db",
    ".azure/accessTokens.json",
    ".azure/msal_token_cache.json",
    ".config/composer/auth.json",
    ".config/helm/registry/config.json",
    ".config/sops/age/keys.txt",
    ".config/age/keys.txt",
    ".gnupg/secring.gpg",
    ".bash_history",
    ".zsh_history",
    ".python_history",
    ".psql_history",
    ".mysql_history",
    ".rediscli_history",
    ".config/fish/fish_history",
)

_HOME_SENSITIVE_GLOBS = (
    ".aws/sso/cache/*",
    ".aws/cli/cache/*",
    ".local/share/keyrings/*",
    ".password-store/**/*",
    ".config/gopass/stores/**/*",
    ".config/gcloud/legacy_credentials/**/*",
    ".gnupg/private-keys-v1.d/*",
)


def _sensitive_home_files(home: Path) -> list[Path]:
    """Retorna credenciais/segredos do usuário que agentes não devem ler."""
    candidates = [home / relative for relative in _HOME_SENSITIVE_FILES]
    for pattern in _HOME_SENSITIVE_GLOBS:
        candidates.extend(sorted(home.glob(pattern)))

    ssh_dir = home / ".ssh"
    if ssh_dir.is_dir():
        for path in sorted(ssh_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.name in _SSH_NON_SECRET_FILES or path.suffix == ".pub":
                continue
            candidates.append(path)

    return candidates


class DecisionsLogger:
    """Logger persistente para decisões por workspace."""

    def __init__(self, log_path: Path):
        """Inicializa uma instância de DecisionsLogger."""
        self._log_path = log_path

    def append(self, decision: str, context: Optional[dict] = None) -> None:
        """Adiciona uma decisão ao log."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "decision": decision,
            "context": context or {},
        }
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def load_recent(self, limit: int = 50) -> List[dict]:
        """Carrega as decisões mais recentes."""
        if not self._log_path.exists():
            return []
        entries = []
        with self._log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries[-limit:]


class Workspace:
    """Resolve e gerencia o diretório de dados de um projeto no armazenamento global do quimera."""

    def __init__(self, cwd: Path):
        """Inicializa uma instância de Workspace."""
        self.base_dir = find_base_writable(CANDIDATE_DIRS)
        self._branch: str | None = None
        self.cwd = Path(cwd).expanduser().resolve()
        self.cwd_hash = hashlib.sha256(str(self.cwd).encode()).hexdigest()[:16]
        self._root = self.base_dir / "workspaces" / self.cwd_hash
        self._ensure_dirs()
        self._write_metadata()
        self._update_index()
        self._restore_branch()

    def _restore_branch(self) -> None:
        """Restaura a branch persistida em workspace.json, se existir."""
        meta_file = self._root / "workspace.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            self._branch = meta.get("branch")

    @property
    def root(self) -> Path:
        """Raiz dos dados persistentes do workspace: ``~/.local/share/quimera/workspaces/{cwd_hash}/``."""
        return self._root

    @property
    def data_dir(self) -> Path:
        """Raiz dos dados persistentes do workspace."""
        return self._root / "data"

    @property
    def branch(self) -> str | None:
        """Branch de contexto ativa, ou ``None`` se nenhuma foi definida."""
        return self._branch

    def set_branch(self, branch: str) -> None:
        """Define manualmente a branch para o contexto persistente.

        O nome é sanitizado (troca '/' por '_') e armazenado.
        Use '/context branch <nome>' no chat.
        """
        sanitized = branch.replace("/", "_").strip()
        self._branch = sanitized if sanitized else "_default"
        self._persist_branch()

    def list_branches(self) -> list[str]:
        """Retorna branches de contexto existentes, incluindo a branch ativa."""
        branches: set[str] = set()
        ctx_dir = self._root / "data" / "context"
        if ctx_dir.exists():
            for d in ctx_dir.iterdir():
                if d.is_dir():
                    branches.add(d.name)
        if self._branch:
            branches.add(self._branch)
        return sorted(branches)

    def _persist_branch(self) -> None:
        """Persiste a branch atual em workspace.json."""
        meta_file = self._root / "workspace.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            meta["branch"] = self._branch
            meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @property
    def context_persistent(self) -> Path:
        """Contexto persistente isolado por branch (definida manualmente via set_branch)."""
        branch = self._branch or "_default"
        path = self.data_dir / "context" / branch / "persistent.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def prompt_persistent(self) -> Path:
        """Template de prompt isolado por branch."""
        branch = self._branch or "_default"
        path = self.data_dir / "prompts" / branch / "prompt.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def context_session(self) -> Path:
        """Arquivo de contexto de sessão (descartado ao final de cada chat)."""
        return self.data_dir / "context" / "session.md"

    @property
    def previous_session_file(self) -> Path:
        """Arquivo com o resumo da sessão anterior (warm-start)."""
        return self.data_dir / "context" / "previous_session.md"

    @property
    def logs_dir(self) -> Path:
        """Diretório de logs de sessões persistentes (JSONL por sessão)."""
        return self.data_dir / "logs" / "sessions"

    @property
    def tasks_db(self) -> Path:
        """Banco de dados SQLite de tasks do workspace."""
        return self.data_dir / "tasks.db"

    @property
    def state_dir(self) -> Path:
        """Diretório de estado interno do workspace (shared state, locks, etc.)."""
        return self._root / "state"

    @property
    def history_dir(self) -> Path:
        """Diretório persistente de histórico de input do workspace."""
        return self.data_dir / "history"

    def history_file_for(self, session_id: str) -> Path:
        """Caminho do arquivo de histórico de input do workspace.

        O histórico de input sobrevive a reinícios do app (como o histórico de
        um shell), por isso não é particionado por sessão — *session_id* é
        aceito por compatibilidade de assinatura, mas ignorado.
        """
        return self.history_dir / "prompt_history.jsonl"

    @property
    def decisions_log(self) -> Path:
        """Log JSONL de decisões registradas durante as sessões."""
        return self.data_dir / "decisions.jsonl"

    @property
    def memory_file(self) -> Path:
        """Arquivo JSON de memória estruturada do workspace."""
        return self._root / "state" / "memory.json"

    @property
    def ui_state_file(self) -> Path:
        """Estado visual da TUI persistido por workspace (tema do Textual, etc.)."""
        return self.state_dir / "ui.json"

    @property
    def config_file(self) -> Path:
        """Caminho do arquivo de configuração global do usuário."""
        return self.base_dir / "config.json"

    @property
    def connections_file(self) -> Path:
        """Configuração global de conexões/providers."""
        return self.base_dir / "connections.json"

    @property
    def mcp_config_file(self) -> Path:
        """Configuração de clientes MCP isolada para este workspace."""
        return self.workspace_config_file

    @property
    def workspace_config_file(self) -> Path:
        """Configurações isoladas do workspace (MCP, sandbox e afins)."""
        return self._root / "config.json"

    @property
    def env_file(self) -> Path:
        """Arquivo global legado de ambiente, preservado por compatibilidade."""
        return self.legacy_secrets_file

    @property
    def runtime_secret_files(self) -> tuple[Path, Path]:
        """Arquivos privados globais do runtime, em ordem de carregamento."""
        return self.base_dir / ".env", self.base_dir / "secrets.env"

    @property
    def project_env_file(self) -> Path:
        """Ambiente operacional local do projeto, visível a agentes/tools."""
        return self.cwd / ".quimera" / ".env"

    @property
    def secrets_file(self) -> Path:
        """Arquivo global canônico de segredos privados do runtime."""
        return self.runtime_secret_files[1]

    @property
    def legacy_secrets_file(self) -> Path:
        """Arquivo global legado de segredos, preservado para compatibilidade."""
        return self.runtime_secret_files[0]

    @property
    def oauth_store_file(self) -> Path:
        """Arquivo de persistência OAuth global (clients dinâmicos e refresh tokens).

        Fica em ``<base_dir>/state/mcp_oauth.json``, fora da árvore por-workspace,
        para que a autorização de clientes MCP HTTP sobreviva a troca de
        workspace e a reinícios de sessão.
        """
        return self.base_dir / "state" / "mcp_oauth.json"

    @property
    def protected_files(self) -> tuple[Path, ...]:
        """Arquivos privados do runtime/usuário que não devem ser visíveis a agentes.

        O ``Workspace`` é a fonte canônica dos caminhos persistentes. A camada
        de sandbox apenas recebe esta lista pronta; ela não reconstrói layout de
        storage nem conhece nomes de arquivos internos. Além do storage privado
        do Quimera, credenciais comuns do HOME são mascaradas quando existentes.
        Estados próprios dos agentes (ex.: ~/.codex e ~/.claude) permanecem sob
        responsabilidade dos respectivos profiles/runtime_rw_paths.
        """
        candidates: list[Path] = [
            self.legacy_secrets_file,
            self.secrets_file,
            self.mcp_config_file,
        ]
        candidates.extend(sorted(self.connections_file.parent.glob(f"{self.connections_file.name}*")))
        candidates.extend(sorted(self.oauth_store_file.parent.glob(f"{self.oauth_store_file.name}*")))
        candidates.extend(_sensitive_home_files(Path.home()))

        unique: list[Path] = []
        seen: set[Path] = set()
        for path in candidates:
            resolved = path.expanduser().resolve()
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            unique.append(resolved)
        return tuple(unique)

    def _ensure_dirs(self):
        """Cria os diretórios persistentes do workspace, registrando warnings em caso de falha."""
        dirs = [
            self.data_dir,
            self.context_session.parent,
            self.logs_dir,
            self.history_dir,
            self.state_dir,
            self.base_dir / "index",
            self.oauth_store_file.parent,
        ]
        for d in dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
                logger.debug("Created dir: %s", d)
            except OSError as e:
                logger.warning("Failed to create dir %s: %s", d, e)

    def _write_metadata(self):
        """Cria ou atualiza ``workspace.json`` com metadados do projeto (cwd, hash, timestamps)."""
        meta_file = self._root / "workspace.json"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        else:
            meta = {"created_at": now}

        meta.update({
            "version": 1,
            "cwd": str(self.cwd),
            "cwd_canonical": str(self.cwd),
            "cwd_hash": self.cwd_hash,
            "name": self.cwd.name,
            "last_used_at": now,
        })
        meta.setdefault("migrated_from", None)
        meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _update_index(self):
        """Atualiza o índice global de workspaces em ``~/.local/share/quimera/index/workspaces.json``."""
        index_file = self.base_dir / "index" / "workspaces.json"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if index_file.exists():
            try:
                index = json.loads(index_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                index = {}
        else:
            index = {}

        index[self.cwd_hash] = {
            "cwd": str(self.cwd),
            "name": self.cwd.name,
            "last_used_at": now,
        }
        index_file.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def migrate_from_legacy(self, project_dir: Path) -> list[str]:
        """Migra arquivos de contexto e logs antigos do diretório do projeto para o workspace."""
        migrated = []

        legacy_context = project_dir / "quimera_context.md"
        if legacy_context.exists() and not self.context_persistent.exists():
            self.context_persistent.write_text(legacy_context.read_text(encoding="utf-8"), encoding="utf-8")
            migrated.append("quimera_context.md -> context/persistent.md")

        legacy_session = project_dir / "quimera_session_context.md"
        if legacy_session.exists() and not self.context_session.exists():
            self.context_session.write_text(legacy_session.read_text(encoding="utf-8"), encoding="utf-8")
            migrated.append("quimera_session_context.md -> context/session.md")

        legacy_logs = project_dir / "logs"
        if legacy_logs.is_dir():
            dest_dir = self.logs_dir / "migrated"
            dest_dir.mkdir(parents=True, exist_ok=True)
            for log_file in legacy_logs.iterdir():
                if log_file.is_file():
                    dest = dest_dir / log_file.name
                    if not dest.exists():
                        dest.write_bytes(log_file.read_bytes())
                        migrated.append(f"logs/{log_file.name}")

        if migrated:
            meta_file = self._root / "workspace.json"
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            meta["migrated_from"] = {
                "path": str(project_dir),
                "files": migrated,
                "migrated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        return migrated

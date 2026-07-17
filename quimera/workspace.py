"""Componentes de `quimera.workspace`."""
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .paths import CANDIDATE_DIRS, TMP_BASE_DIR, find_base_writable

logger = logging.getLogger(__name__)


class DecisionsLogger:
    """Logger persistente para decisões por workspace."""

    def __init__(self, log_path: Path):
        """Inicializa uma instância de DecisionsLogger."""
        self._log_path = log_path
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

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


class WorkspaceTmp:
    """Subtree temporária do workspace em /tmp — logs de sessão, nunca dados persistentes."""

    def __init__(self, cwd_hash: str):
        """Inicializa a subtree temporária para o workspace identificado por *cwd_hash*."""
        self._root = TMP_BASE_DIR / cwd_hash
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Cria os subdiretórios temporários necessários, registrando warnings em caso de falha."""
        self._ensure_dir(self.logs_dir, "logs dir")
        self._ensure_dir(self.render_logs_dir, "render logs dir")
        self._ensure_dir(self.metrics_dir, "metrics dir")
        self._ensure_dir(self.clipboard_dir, "clipboard dir")

    def _ensure_dir(self, path: Path, label: str) -> None:
        """Cria *path* (incluindo pais) e registra warning se a criação falhar."""
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Failed to create %s %s: %s", label, path, e)

    @property
    def root(self) -> Path:
        """Raiz da subtree temporária: ``/tmp/quimera/{cwd_hash}/``."""
        return self._root

    @property
    def logs_dir(self) -> Path:
        """Diretório temporário base de logs da sessão atual."""
        return self._root / "data" / "logs"

    @property
    def render_logs_dir(self) -> Path:
        """Diretório de logs de auditoria de render (JSONL + ANSI bruto)."""
        return self.logs_dir / "render"

    @property
    def metrics_dir(self) -> Path:
        """Diretório de métricas de sessão (latência, tokens, etc.)."""
        return self.logs_dir / "metrics"

    @property
    def clipboard_dir(self) -> Path:
        """Diretório temporário de anexos colados no input."""
        return self._root / "clipboard"

    def render_log_path_for(self, session_id: str) -> Path:
        """Caminho do arquivo JSONL de auditoria de render para *session_id*."""
        return self.render_logs_dir / f"render-{session_id}.jsonl"

    def render_ansi_path_for(self, session_id: str) -> Path:
        """Caminho do arquivo ANSI bruto de render para *session_id*."""
        return self.render_logs_dir / f"render-{session_id}.ansi"

    def metrics_path_for(self, session_id: str) -> Path:
        """Caminho do arquivo JSONL de métricas para *session_id*."""
        return self.metrics_dir / f"{session_id}.jsonl"

    def app_log_path_for(self, session_id: str) -> Path:
        """Caminho do arquivo de log da aplicação para *session_id*."""
        return self.logs_dir / f"app-{session_id}.log"


class Workspace:
    """Resolve e gerencia o diretório de dados de um projeto no armazenamento global do quimera."""

    def __init__(self, cwd: Path):
        """Inicializa uma instância de Workspace."""
        self.base_dir = find_base_writable(CANDIDATE_DIRS)
        self.cwd = cwd.expanduser().resolve()
        self.cwd_hash = hashlib.sha256(str(self.cwd).encode()).hexdigest()[:16]
        self._root = self.base_dir / "workspaces" / self.cwd_hash
        self._branch: str | None = None
        self._tmp = WorkspaceTmp(self.cwd_hash)
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
    def tmp(self) -> WorkspaceTmp:
        """Subtree temporária em ``/tmp/quimera/{cwd_hash}/`` — logs de sessão, nunca dados persistentes."""
        return self._tmp

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
        path = self._root / "data" / "context" / branch / "persistent.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def prompt_persistent(self) -> Path:
        """Template de prompt isolado por branch."""
        branch = self._branch or "_default"
        path = self._root / "data" / "prompts" / branch / "prompt.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def context_session(self) -> Path:
        """Arquivo de contexto de sessão (descartado ao final de cada chat)."""
        return self._root / "data" / "context" / "session.md"

    @property
    def previous_session_file(self) -> Path:
        """Arquivo com o resumo da sessão anterior (warm-start)."""
        return self._root / "data" / "context" / "previous_session.md"

    @property
    def render_logs_dir(self) -> Path:
        """Diretório persistente de auditoria de render (JSONL + ANSI)."""
        return self._root / "data" / "logs" / "render"

    @property
    def metrics_dir(self) -> Path:
        """Diretório persistente de métricas de sessão."""
        return self._root / "data" / "logs" / "metrics"

    def render_log_path_for(self, session_id: str) -> Path:
        """Caminho do arquivo JSONL de auditoria de render para *session_id*."""
        return self.render_logs_dir / f"render-{session_id}.jsonl"

    def render_ansi_path_for(self, session_id: str) -> Path:
        """Caminho do arquivo ANSI bruto de render para *session_id*."""
        return self.render_logs_dir / f"render-{session_id}.ansi"

    def metrics_path_for(self, session_id: str) -> Path:
        """Caminho do arquivo JSONL de métricas para *session_id*."""
        return self.metrics_dir / f"{session_id}.jsonl"

    @property
    def logs_dir(self) -> Path:
        """Diretório de logs de sessões persistentes (JSONL por sessão)."""
        return self._root / "data" / "logs" / "sessions"

    @property
    def tasks_db(self) -> Path:
        """Banco de dados SQLite de tasks do workspace."""
        return self._root / "data" / "tasks.db"

    @property
    def state_dir(self) -> Path:
        """Diretório de estado interno do workspace (shared state, locks, etc.)."""
        return self._root / "state"

    @property
    def history_dir(self) -> Path:
        """Diretório persistente de histórico de input do workspace."""
        return self._root / "data" / "history"

    def history_file_for(self, session_id: str) -> Path:
        """Caminho do arquivo de histórico de input do workspace.

        O histórico de input sobrevive a reinícios do app (como o histórico de
        um shell), por isso não é particionado por sessão — *session_id* é
        aceito por compatibilidade de assinatura, mas ignorado.
        """
        return self.history_dir / "prompt_history.jsonl"

    @property
    def artifacts_dir(self) -> Path:
        """Diretório persistente para artefatos gerados por ferramentas (ex: screenshots de browser)."""
        return self._root / "data" / "artifacts"

    @property
    def decisions_log(self) -> Path:
        """Log JSONL de decisões registradas durante as sessões."""
        return self._root / "data" / "decisions.jsonl"

    @property
    def memory_file(self) -> Path:
        """Arquivo JSON de memória estruturada do workspace."""
        return self._root / "state" / "memory.json"

    @property
    def config_file(self) -> Path:
        """Caminho do arquivo de configuração global do usuário."""
        return self.base_dir / "config.json"

    @property
    def mcp_config_file(self) -> Path:
        """Configuração de clientes MCP isolada para este workspace."""
        return self._root / "config.json"

    @property
    def env_file(self) -> Path:
        """Caminho do arquivo de variáveis de ambiente de modelo."""
        return self.base_dir / ".env"

    def _ensure_dirs(self):
        """Cria os diretórios persistentes do workspace, registrando warnings em caso de falha."""
        dirs = [
            self._root / "data",
            self._root / "data" / "context",
            self._root / "data" / "logs" / "render",
            self._root / "data" / "logs" / "metrics",
            self._root / "data" / "logs" / "sessions",
            self._root / "data" / "artifacts",
            self._root / "state",
            self.base_dir / "index",
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

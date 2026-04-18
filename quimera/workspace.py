"""Componentes de `quimera.workspace`."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .paths import CANDIDATE_DIRS, find_base_writable


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


class Workspace:
    """Resolve e gerencia o diretório de dados de um projeto no armazenamento global do quimera."""

    def __init__(self, cwd: Path):
        """Inicializa uma instância de Workspace."""
        self.base_dir = find_base_writable(CANDIDATE_DIRS)
        self.cwd = cwd.expanduser().resolve()
        self.cwd_hash = hashlib.sha256(str(self.cwd).encode()).hexdigest()[:16]
        self._root = self.base_dir / "workspaces" / self.cwd_hash
        self._ensure_dirs()
        self._write_metadata()
        self._update_index()

    @property
    def root(self) -> Path:
        """Executa root."""
        return self._root

    @property
    def context_persistent(self) -> Path:
        """Executa context persistent."""
        return self._root / "data" / "context" / "persistent.md"

    @property
    def context_session(self) -> Path:
        """Executa context session."""
        return self._root / "data" / "context" / "session.md"

    @property
    def previous_session_file(self) -> Path:
        """Arquivo com o resumo da sessão anterior (warm-start)."""
        return self._root / "data" / "context" / "previous_session.md"

    @property
    def logs_dir(self) -> Path:
        """Executa logs dir."""
        return self._root / "data" / "logs" / "sessions"

    @property
    def tasks_db(self) -> Path:
        """Executa tasks db."""
        return self._root / "data" / "tasks.db"

    @property
    def metrics_dir(self) -> Path:
        """Executa metrics dir."""
        return self._root / "data" / "logs" / "metrics"

    @property
    def state_dir(self) -> Path:
        """Executa state dir."""
        return self._root / "state"

    @property
    def history_file(self) -> Path:
        """Executa history file."""
        return self._root / "history"

    @property
    def decisions_log(self) -> Path:
        """Executa decisions log."""
        return self._root / "data" / "decisions.jsonl"

    @property
    def config_file(self) -> Path:
        """Caminho do arquivo de configuração global do usuário."""
        return self.base_dir / "config.json"

    def _ensure_dirs(self):
        """Executa ensure dirs."""
        (self._root / "data" / "context").mkdir(parents=True, exist_ok=True)
        (self._root / "data" / "logs" / "sessions").mkdir(parents=True, exist_ok=True)
        (self._root / "data" / "logs" / "metrics").mkdir(parents=True, exist_ok=True)
        (self._root / "data").mkdir(parents=True, exist_ok=True)
        (self._root / "state").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "index").mkdir(parents=True, exist_ok=True)

    def _write_metadata(self):
        """Escreve metadata."""
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
        """Atualiza index."""
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

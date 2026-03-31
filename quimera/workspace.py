import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

QUIMERA_BASE = Path.home() / ".local" / "share" / "quimera"


class Workspace:
    """Resolve e gerencia o diretório de dados de um projeto no armazenamento global do quimera."""

    def __init__(self, cwd: Path):
        self.cwd = cwd.expanduser().resolve()
        self.cwd_hash = hashlib.sha256(str(self.cwd).encode()).hexdigest()[:16]
        self._root = QUIMERA_BASE / "workspaces" / self.cwd_hash
        self._ensure_dirs()
        self._write_metadata()
        self._update_index()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def context_persistent(self) -> Path:
        return self._root / "data" / "context" / "persistent.md"

    @property
    def context_session(self) -> Path:
        return self._root / "data" / "context" / "session.md"

    @property
    def logs_dir(self) -> Path:
        return self._root / "data" / "logs" / "sessions"

    @property
    def metrics_dir(self) -> Path:
        return self._root / "data" / "logs" / "metrics"

    @property
    def state_dir(self) -> Path:
        return self._root / "state"

    @property
    def history_file(self) -> Path:
        return self._root / "history"

    def _ensure_dirs(self):
        (self._root / "data" / "context").mkdir(parents=True, exist_ok=True)
        (self._root / "data" / "logs" / "sessions").mkdir(parents=True, exist_ok=True)
        (self._root / "data" / "logs" / "metrics").mkdir(parents=True, exist_ok=True)
        (self._root / "state").mkdir(parents=True, exist_ok=True)
        (QUIMERA_BASE / "index").mkdir(parents=True, exist_ok=True)

    def _write_metadata(self):
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
        index_file = QUIMERA_BASE / "index" / "workspaces.json"
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

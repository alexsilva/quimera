"""Componentes de `quimera.storage`."""
import json
import re
from datetime import datetime, timedelta, MINYEAR
from pathlib import Path

SHARED_STATE_TTL_HOURS = 24
HISTORY_TTL_HOURS = 72

_SESSION_TIMESTAMP_RE = re.compile(r"sessao-(\d{4}-\d{2}-\d{2}-\d{6})\.json$")


class SessionStorage:
    """Centraliza logs textuais e snapshots JSON de uma sessão."""

    def __init__(self, logs_dir: Path):
        """Inicializa uma instância de SessionStorage."""
        self._pending_restore_notice = None
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        self.session_id = f"sessao-{now.strftime('%Y-%m-%d-%H%M%S')}"
        self.session_dir = logs_dir / date_str
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.session_dir / f"sessao-{date_str}.txt"
        self.history_file = self.session_dir / f"{self.session_id}.json"
        self._logs_dir = logs_dir

    def get_log_file(self):
        """Retorna log file."""
        return self.log_file

    def get_history_file(self):
        """Retorna history file."""
        return self.history_file

    def get_session_id(self) -> str:
        """Getter callable que retorna o session_id."""
        return self.session_id

    def append_log(self, role, content):
        """Acrescenta log."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.get_log_file().open("a", encoding="utf-8") as file:
            file.write(f"[{timestamp}] [{role.upper()}] {content}\n")

    def save_history(self, history, shared_state=None):
        """Persiste history."""
        payload = {
            "session_id": self.history_file.stem,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "cwd": str(Path.cwd()),
            "messages": history,
            "shared_state": shared_state or {},
        }
        with self.history_file.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    @staticmethod
    def _normalize_sort_datetime(value: datetime) -> datetime:
        """Normaliza timestamps aware para o horário local naive usado pelo app."""
        if value.tzinfo is None:
            return value
        return value.astimezone().replace(tzinfo=None)

    @classmethod
    def _session_sort_key(cls, path: Path) -> datetime:
        """Chave de ordenação por datetime real: lê `saved_at` do payload, com fallback para nome do arquivo."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("saved_at"):
                return cls._normalize_sort_datetime(
                    datetime.fromisoformat(data["saved_at"])
                )
        except (json.JSONDecodeError, OSError, ValueError):
            pass
        m = _SESSION_TIMESTAMP_RE.search(path.name)
        if m:
            return datetime.strptime(m.group(1), "%Y-%m-%d-%H%M%S")
        return datetime(MINYEAR, 1, 1)

    def load_last_session(self):
        """Restaura o snapshot mais recente salvo em JSON, se existir."""
        self._pending_restore_notice = None
        now = datetime.now()
        history_ttl = timedelta(hours=HISTORY_TTL_HOURS)
        shared_state_ttl = timedelta(hours=SHARED_STATE_TTL_HOURS)
        json_files = sorted(
            self._logs_dir.rglob("sessao-*.json"),
            key=self._session_sort_key,
            reverse=True,
        )
        if not json_files:
            return {"messages": [], "shared_state": {}}

        current_cwd = str(Path.cwd())
        for json_file in json_files:
            try:
                with json_file.open(encoding="utf-8") as file:
                    data = json.load(file)
            except (json.JSONDecodeError, OSError):
                continue

            # Process this session
            if isinstance(data, list):
                # Formato legado (lista pura): sem cwd, sem saved_at — descartado.
                continue
            elif isinstance(data, dict):
                # New format (dict) - check cwd for workspace isolation.
                saved_cwd = data.get("cwd")
                if saved_cwd is not None and saved_cwd != current_cwd:
                    # Session from different workspace, skip.
                    continue
                messages = data.get("messages", [])
                shared_state = data.get("shared_state", {})
                saved_at_raw = data.get("saved_at")
                if not saved_at_raw:
                    # Snapshots sem saved_at não são confiáveis para restauração.
                    continue
                try:
                    snapshot_time = self._normalize_sort_datetime(datetime.fromisoformat(saved_at_raw))
                except (ValueError, TypeError):
                    continue
            else:
                continue

            if not isinstance(shared_state, dict):
                shared_state = {}

            if now - snapshot_time > history_ttl:
                # Ordenação é decrescente por tempo: snapshots seguintes serão ainda mais antigos.
                break

            if shared_state and now - snapshot_time > shared_state_ttl:
                shared_state = {}

            if messages:
                self._pending_restore_notice = (
                    f"[memória] histórico restaurado de {json_file.parent.name}/{json_file.name} ({len(messages)} mensagens)\n"
                )

            return {"messages": messages, "shared_state": shared_state}

        # No matching session found
        return {"messages": [], "shared_state": {}}

    def pop_restore_notice(self):
        """Retorna e limpa o aviso pendente de histórico restaurado."""
        notice = self._pending_restore_notice
        self._pending_restore_notice = None
        return notice

    def load_last_history(self):
        """Compatibilidade: retorna apenas as mensagens do snapshot mais recente."""
        return self.load_last_session()["messages"]

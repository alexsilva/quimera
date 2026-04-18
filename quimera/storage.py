"""Componentes de `quimera.storage`."""
import json
import re
from datetime import datetime, timedelta, MINYEAR
from pathlib import Path

SHARED_STATE_TTL_HOURS = 24

_SESSION_TIMESTAMP_RE = re.compile(r"sessao-(\d{4}-\d{2}-\d{2}-\d{6})\.json$")


class SessionStorage:
    """Centraliza logs textuais e snapshots JSON de uma sessão."""

    def __init__(self, logs_dir: Path, renderer):
        """Inicializa uma instância de SessionStorage."""
        self.renderer = renderer
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        self.session_dir = logs_dir / date_str
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.session_dir / f"sessao-{date_str}.txt"
        self.history_file = self.session_dir / f"sessao-{now.strftime('%Y-%m-%d-%H%M%S')}.json"
        self._logs_dir = logs_dir

    def get_log_file(self):
        """Retorna log file."""
        return self.log_file

    def get_history_file(self):
        """Retorna history file."""
        return self.history_file

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
        json_files = sorted(
            self._logs_dir.rglob("sessao-*.json"),
            key=self._session_sort_key,
            reverse=True,
        )
        if not json_files:
            return {"messages": [], "shared_state": {}}

        latest = json_files[0]
        try:
            with latest.open(encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError):
            return {"messages": [], "shared_state": {}}

        if isinstance(data, list):
            messages = data
            shared_state = {}
        elif isinstance(data, dict):
            messages = data.get("messages", [])
            shared_state = data.get("shared_state", {})
        else:
            messages = []
            shared_state = {}

        if messages:
            self.renderer.show_system(
                f"[memória] histórico restaurado de {latest.parent.name}/{latest.name} ({len(messages)} mensagens)\n"
            )
        if not isinstance(shared_state, dict):
            shared_state = {}

        # Descarta shared_state se o snapshot for mais antigo que o TTL
        # Snapshots sem saved_at são considerados expirados por segurança
        saved_at_raw = data.get("saved_at") if isinstance(data, dict) else None
        if shared_state and not saved_at_raw:
            shared_state = {}
        elif shared_state and saved_at_raw:
            try:
                saved_at = datetime.fromisoformat(saved_at_raw)
                saved_at = self._normalize_sort_datetime(saved_at)
                if datetime.now() - saved_at > timedelta(hours=SHARED_STATE_TTL_HOURS):
                    shared_state = {}
            except (ValueError, TypeError):
                shared_state = {}

        return {"messages": messages, "shared_state": shared_state}

    def load_last_history(self):
        """Compatibilidade: retorna apenas as mensagens do snapshot mais recente."""
        return self.load_last_session()["messages"]

"""Utilitários de caminho para resolução do diretório base do Quimera."""
import tempfile
from pathlib import Path

TMP_BASE_DIR = Path(tempfile.gettempdir()) / "quimera"

CANDIDATE_DIRS = [
    Path.home() / ".local" / "share" / "quimera",
    TMP_BASE_DIR,
]


def find_base_writable(candidates: list) -> Path:
    """Retorna o primeiro diretório gravável da lista."""
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except OSError:
            continue
    raise OSError("Não foi possível resolver um diretório gravável para o workspace do Quimera")

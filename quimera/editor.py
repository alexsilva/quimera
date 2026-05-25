"""Entidade Editor: abre editor externo para composição ou edição de arquivos."""
from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

from . import process_factory as subprocess


def _normalize(text: str) -> str | None:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if not normalized.strip():
        return None
    return normalized


class Editor:
    """Encapsula resolução e execução de um editor externo.

    Dois modos de uso:
    - compose()    — abre arquivo temporário, retorna conteúdo digitado
    - open_file()  — abre arquivo existente para edição in-place
    """

    _FALLBACKS = ("nano", "vim", "vi")

    def __init__(self, renderer, output_lock=None):
        self._renderer = renderer
        self._output_lock = output_lock

    def _resolve(self) -> list[str] | None:
        """Resolve o editor: $EDITOR ou fallback. Retorna None se nenhum disponível."""
        env = os.environ.get("EDITOR", "")
        if env:
            return shlex.split(env)
        fallback = next((e for e in self._FALLBACKS if shutil.which(e)), None)
        if not fallback:
            self._renderer.show_error(
                "\nNenhum editor encontrado. Defina $EDITOR ou instale nano/vim.\n"
            )
            return None
        return [fallback]

    def compose(self, initial_content: str = "") -> str | None:
        """Abre editor com arquivo temporário; retorna conteúdo digitado."""
        parts = self._resolve()
        if parts is None:
            return None

        with tempfile.NamedTemporaryFile(
            suffix=".md", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp.write(initial_content)
            tmp_path = tmp.name

        try:
            with (self._output_lock if self._output_lock is not None else nullcontext()):
                subprocess.run([*parts, tmp_path], check=True)
                sys.stdout.write("\n")
                sys.stdout.flush()
                content = Path(tmp_path).read_text(encoding="utf-8")
            return _normalize(content)
        except FileNotFoundError:
            self._renderer.show_error(f"\nEditor não encontrado: {parts[0]}\n")
            return None
        except subprocess.CalledProcessError as exc:
            self._renderer.show_error(
                f"\nEditor encerrou com erro (código {exc.returncode}).\n"
            )
            return None
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def open_file(self, path: str | Path) -> bool:
        """Abre arquivo existente no editor (edição in-place). Retorna True se ok."""
        parts = self._resolve()
        if parts is None:
            return False
        try:
            subprocess.run([*parts, str(path)], check=True)
            return True
        except FileNotFoundError:
            self._renderer.show_error(f"\nEditor não encontrado: {parts[0]}\n")
            return False
        except subprocess.CalledProcessError as exc:
            self._renderer.show_error(
                f"\nFalha ao abrir no editor (código {exc.returncode}).\n"
            )
            return False

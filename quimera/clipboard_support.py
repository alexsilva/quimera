"""Helpers para colar imagem do clipboard no prompt."""
from __future__ import annotations

import base64
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_ATTACHED_IMAGE_RE = re.compile(
    r'<attached_image\s+path="(?P<path>[^"]+)"(?:\s+mime="(?P<mime>[^"]+)")?\s*/>'
)
# Matcher tolerante: casa qualquer tag <attached_image ...>, sem depender da
# ordem/estilo dos atributos ou de a tag ser autofechada. Usado apenas na
# exibição (humanize/strip) para garantir que nenhum XML vaze no chat mesmo se o
# formato do marcador mudar; o envio ao agente continua usando o regex estrito.
_ANY_ATTACHED_IMAGE_RE = re.compile(r"<attached_image\b[^>]*?/?>", re.IGNORECASE)
# Extrai o atributo path de qualquer marcador, aceitando aspas simples ou duplas.
_ATTACHED_IMAGE_PATH_RE = re.compile(
    r"""path\s*=\s*(?P<quote>["'])(?P<path>.*?)(?P=quote)""",
    re.IGNORECASE,
)
_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp")

# Prefixo estável dos temporários de clipboard, usado tanto na criação quanto
# na varredura de limpeza.
_TEMP_IMAGE_PREFIX = "quimera-clipboard-"
_FALLBACK_TEMP_IMAGE_DIR = Path(tempfile.gettempdir()) / "quimera" / "clipboard"
# Temporários mais antigos que este TTL são removidos a cada nova colagem, para
# evitar acúmulo indefinido em /tmp.
_TEMP_IMAGE_TTL_SECONDS = 3600
# Limite de bytes para embutir a imagem inline como data URL; acima disso a
# imagem não é enviada inline (evita estourar limite de tamanho/tokens da API).
_MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class ClipboardPayload:
    """Payload lido do clipboard, podendo ser texto ou imagem."""
    text: str
    kind: str = "text"
    path: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True)
class AttachedImage:
    """Imagem anexada ao prompt com marcador e tipo MIME."""
    path: str
    mime_type: str
    start: int
    end: int
    marker: str


class ClipboardManager:
    """Lida com leitura do clipboard, anexos temporários e payload multimodal."""

    image_mime_types = _IMAGE_MIME_TYPES
    temp_image_prefix = _TEMP_IMAGE_PREFIX
    temp_image_ttl_seconds = _TEMP_IMAGE_TTL_SECONDS
    max_inline_image_bytes = _MAX_INLINE_IMAGE_BYTES
    attached_image_re = _ATTACHED_IMAGE_RE
    any_attached_image_re = _ANY_ATTACHED_IMAGE_RE
    attached_image_path_re = _ATTACHED_IMAGE_PATH_RE
    fallback_temp_image_dir = _FALLBACK_TEMP_IMAGE_DIR

    def __init__(self, temp_image_dir: str | Path | None = None) -> None:
        """Inicializa o gerenciador com o diretório temporário de anexos."""
        self.temp_image_dir = self._resolve_temp_image_dir(temp_image_dir)

    def marker_for(
        self,
        path: str | Path,
        mime_type: str = "image/png",
    ) -> str:
        """Gera o marcador XML de imagem anexada para o caminho e tipo informados."""
        return f'<attached_image path="{Path(path)}" mime="{mime_type}" />'

    def iter_images(self, text: str) -> list[AttachedImage]:
        """Extrai todos os marcadores de imagem anexada do texto."""
        images: list[AttachedImage] = []
        for match in self.attached_image_re.finditer(str(text or "")):
            path = match.group("path")
            mime_type = match.group("mime") or self._guess_mime_type(path)
            images.append(
                AttachedImage(
                    path=path,
                    mime_type=mime_type,
                    start=match.start(),
                    end=match.end(),
                    marker=match.group(0),
                )
            )
        return images

    def to_openai_content(self, text: str) -> str | list[dict]:
        """Converte texto com marcadores de imagem para o formato de conteúdo da OpenAI."""
        body = str(text or "")
        images = self.iter_images(body)
        if not images:
            return body.strip()

        content: list[dict] = []
        cursor = 0
        for image in images:
            prefix = body[cursor:image.start]
            if prefix.strip():
                content.append({"type": "text", "text": prefix.strip()})
            data_url = self._path_to_data_url(image.path, image.mime_type)
            if data_url is None:
                content.append({"type": "text", "text": image.marker})
            else:
                content.append({"type": "image_url", "image_url": {"url": data_url}})
            cursor = image.end

        suffix = body[cursor:]
        if suffix.strip():
            content.append({"type": "text", "text": suffix.strip()})
        return content

    def read(self) -> ClipboardPayload | None:
        """Lê o conteúdo atual do clipboard (imagem ou texto)."""
        image_payload = self._read_image()
        if image_payload is not None:
            return image_payload
        return self._read_text()

    def strip_markers(
        self,
        text: str,
        placeholder: str = "🖼 [imagem anexada]",
    ) -> str:
        """Substitui marcadores de imagem por um texto legível.

        Usa o matcher tolerante para não deixar nenhuma variação da tag vazar
        no histórico, mesmo que a ordem/estilo dos atributos mude.
        """
        return self.any_attached_image_re.sub(placeholder, str(text or ""))

    def humanize_markers(self, text: str) -> str:
        """Converte marcadores XML de imagem em apresentação amigável (com o nome do arquivo).

        Usado apenas para exibição no chat; o formato XML original é preservado
        para envio ao agente. Casa qualquer variação de ``<attached_image ...>``
        e cai num rótulo genérico quando o ``path`` não pode ser extraído, de
        modo que nenhum XML cru chegue ao chat.
        """
        def _replace(match: "re.Match[str]") -> str:
            attr = self.attached_image_path_re.search(match.group(0))
            name = Path(attr.group("path")).name if attr else ""
            return f"🖼 imagem anexada · {name}" if name else "🖼 imagem anexada"

        return self.any_attached_image_re.sub(_replace, str(text or ""))

    def write_temp(self, data: bytes, mime_type: str) -> Path:
        """Salva dados binários em arquivo temporário com prefixo do clipboard."""
        self.cleanup_stale()
        suffix = mimetypes.guess_extension(mime_type) or ".img"
        self.temp_image_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=self.temp_image_prefix,
            suffix=suffix,
            dir=self.temp_image_dir,
            delete=False,
        ) as handle:
            handle.write(data)
            return Path(handle.name)

    def cleanup_stale(self, now: float | None = None) -> None:
        """Remove temporários de clipboard mais antigos que o TTL (best-effort)."""
        reference = time.time() if now is None else now
        try:
            entries = list(self.temp_image_dir.glob(f"{self.temp_image_prefix}*"))
        except OSError:
            return
        for entry in entries:
            try:
                if reference - entry.stat().st_mtime > self.temp_image_ttl_seconds:
                    entry.unlink()
            except OSError:
                continue

    def _read_image(self) -> ClipboardPayload | None:
        for mime_type in self.image_mime_types:
            payload = self._read_image_via_wl_paste(mime_type)
            if payload is not None:
                return payload
            payload = self._read_image_via_xclip(mime_type)
            if payload is not None:
                return payload
        payload = self._read_image_via_pngpaste()
        if payload is not None:
            return payload
        return None

    def _read_image_via_wl_paste(self, mime_type: str) -> ClipboardPayload | None:
        if shutil.which("wl-paste") is None:
            return None
        result = self._run_clipboard_command(["wl-paste", "--no-newline", "--type", mime_type])
        if result is None or not result.stdout:
            return None
        path = self.write_temp(result.stdout, mime_type)
        marker = self.marker_for(path, mime_type)
        return ClipboardPayload(text=marker, kind="image", path=str(path), mime_type=mime_type)

    def _read_image_via_xclip(self, mime_type: str) -> ClipboardPayload | None:
        if shutil.which("xclip") is None:
            return None
        result = self._run_clipboard_command(
            ["xclip", "-selection", "clipboard", "-t", mime_type, "-o"]
        )
        if result is None or not result.stdout:
            return None
        path = self.write_temp(result.stdout, mime_type)
        marker = self.marker_for(path, mime_type)
        return ClipboardPayload(text=marker, kind="image", path=str(path), mime_type=mime_type)

    def _read_image_via_pngpaste(self) -> ClipboardPayload | None:
        if shutil.which("pngpaste") is None:
            return None
        result = self._run_clipboard_command(["pngpaste", "-"])
        if result is None or not result.stdout:
            return None
        mime_type = "image/png"
        path = self.write_temp(result.stdout, mime_type)
        marker = self.marker_for(path, mime_type)
        return ClipboardPayload(text=marker, kind="image", path=str(path), mime_type=mime_type)

    def _read_text(self) -> ClipboardPayload | None:
        for command in (
            ["wl-paste", "--no-newline"],
            ["xclip", "-selection", "clipboard", "-o"],
        ):
            if shutil.which(command[0]) is None:
                continue
            result = self._run_clipboard_command(command, text=True)
            if result is None:
                continue
            text = str(result.stdout or "").strip()
            if text:
                return ClipboardPayload(text=text, kind="text")
        return None

    def _run_clipboard_command(
        self,
        command: list[str],
        text: bool = False,
    ) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=text,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, OSError):
            return None

    def _resolve_temp_image_dir(self, temp_image_dir: str | Path | None = None) -> Path:
        if temp_image_dir is None:
            return self.fallback_temp_image_dir
        return Path(temp_image_dir).expanduser().resolve()

    def _guess_mime_type(self, path: str) -> str:
        mime_type, _ = mimetypes.guess_type(path)
        return mime_type or "image/png"

    def _path_to_data_url(self, path: str, mime_type: str) -> str | None:
        try:
            if os.path.getsize(path) > self.max_inline_image_bytes:
                return None
            raw = Path(path).read_bytes()
        except OSError:
            return None
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

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
_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp")

# Prefixo estável dos temporários de clipboard, usado tanto na criação quanto
# na varredura de limpeza.
_TEMP_IMAGE_PREFIX = "quimera-clipboard-"
# Temporários mais antigos que este TTL são removidos a cada nova colagem, para
# evitar acúmulo indefinido em /tmp.
_TEMP_IMAGE_TTL_SECONDS = 3600
# Limite de bytes para embutir a imagem inline como data URL; acima disso a
# imagem não é enviada inline (evita estourar limite de tamanho/tokens da API).
_MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class ClipboardPayload:
    text: str
    kind: str = "text"
    path: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True)
class AttachedImage:
    path: str
    mime_type: str
    start: int
    end: int
    marker: str


def build_attached_image_marker(path: str | Path, mime_type: str = "image/png") -> str:
    return f'<attached_image path="{Path(path)}" mime="{mime_type}" />'


def iter_attached_images(text: str) -> list[AttachedImage]:
    images: list[AttachedImage] = []
    for match in _ATTACHED_IMAGE_RE.finditer(str(text or "")):
        path = match.group("path")
        mime_type = match.group("mime") or _guess_mime_type(path)
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


def build_openai_multimodal_content(text: str) -> str | list[dict]:
    body = str(text or "")
    images = iter_attached_images(body)
    if not images:
        return body.strip()

    content: list[dict] = []
    cursor = 0
    for image in images:
        prefix = body[cursor:image.start]
        if prefix.strip():
            content.append({"type": "text", "text": prefix.strip()})
        data_url = _image_path_to_data_url(image.path, image.mime_type)
        if data_url is None:
            content.append({"type": "text", "text": image.marker})
        else:
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        cursor = image.end

    suffix = body[cursor:]
    if suffix.strip():
        content.append({"type": "text", "text": suffix.strip()})
    return content


def read_clipboard_payload() -> ClipboardPayload | None:
    image_payload = _read_clipboard_image()
    if image_payload is not None:
        return image_payload
    return _read_clipboard_text()


def _read_clipboard_image() -> ClipboardPayload | None:
    for mime_type in _IMAGE_MIME_TYPES:
        payload = _read_image_via_wl_paste(mime_type)
        if payload is not None:
            return payload
        payload = _read_image_via_xclip(mime_type)
        if payload is not None:
            return payload
    payload = _read_image_via_pngpaste()
    if payload is not None:
        return payload
    return None


def _read_image_via_wl_paste(mime_type: str) -> ClipboardPayload | None:
    if shutil.which("wl-paste") is None:
        return None
    result = _run_clipboard_command(["wl-paste", "--no-newline", "--type", mime_type])
    if result is None or not result.stdout:
        return None
    path = _write_temp_image(result.stdout, mime_type)
    marker = build_attached_image_marker(path, mime_type)
    return ClipboardPayload(text=marker, kind="image", path=str(path), mime_type=mime_type)


def _read_image_via_xclip(mime_type: str) -> ClipboardPayload | None:
    if shutil.which("xclip") is None:
        return None
    result = _run_clipboard_command(
        ["xclip", "-selection", "clipboard", "-t", mime_type, "-o"]
    )
    if result is None or not result.stdout:
        return None
    path = _write_temp_image(result.stdout, mime_type)
    marker = build_attached_image_marker(path, mime_type)
    return ClipboardPayload(text=marker, kind="image", path=str(path), mime_type=mime_type)


def _read_image_via_pngpaste() -> ClipboardPayload | None:
    if shutil.which("pngpaste") is None:
        return None
    result = _run_clipboard_command(["pngpaste", "-"])
    if result is None or not result.stdout:
        return None
    mime_type = "image/png"
    path = _write_temp_image(result.stdout, mime_type)
    marker = build_attached_image_marker(path, mime_type)
    return ClipboardPayload(text=marker, kind="image", path=str(path), mime_type=mime_type)


def _read_clipboard_text() -> ClipboardPayload | None:
    for command in (
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
    ):
        if shutil.which(command[0]) is None:
            continue
        result = _run_clipboard_command(command, text=True)
        if result is None:
            continue
        text = str(result.stdout or "").strip()
        if text:
            return ClipboardPayload(text=text, kind="text")
    return None


def _run_clipboard_command(command: list[str], text: bool = False) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=text,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        return None


def strip_attached_image_markers(text: str, placeholder: str = "🖼 [imagem anexada]") -> str:
    """Substitui marcadores de imagem por um texto legível.

    Usado antes de persistir o histórico: o ``path`` do marcador aponta para um
    temporário efêmero em ``/tmp`` que será limpo, então reusar a entrada em
    outra sessão vazaria XML cru (com path morto) para o modelo.
    """
    return _ATTACHED_IMAGE_RE.sub(placeholder, str(text or ""))


def _cleanup_stale_temp_images(now: float | None = None) -> None:
    """Remove temporários de clipboard mais antigos que o TTL (best-effort)."""
    reference = time.time() if now is None else now
    try:
        entries = list(Path("/tmp").glob(f"{_TEMP_IMAGE_PREFIX}*"))
    except OSError:
        return
    for entry in entries:
        try:
            if reference - entry.stat().st_mtime > _TEMP_IMAGE_TTL_SECONDS:
                entry.unlink()
        except OSError:
            continue


def _write_temp_image(data: bytes, mime_type: str) -> Path:
    _cleanup_stale_temp_images()
    suffix = mimetypes.guess_extension(mime_type) or ".img"
    with tempfile.NamedTemporaryFile(
        prefix=_TEMP_IMAGE_PREFIX,
        suffix=suffix,
        dir="/tmp",
        delete=False,
    ) as handle:
        handle.write(data)
        return Path(handle.name)


def _guess_mime_type(path: str) -> str:
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type or "image/png"


def _image_path_to_data_url(path: str, mime_type: str) -> str | None:
    try:
        if os.path.getsize(path) > _MAX_INLINE_IMAGE_BYTES:
            return None
        raw = Path(path).read_bytes()
    except OSError:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"

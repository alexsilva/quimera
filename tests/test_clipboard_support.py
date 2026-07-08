from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from quimera.clipboard_support import (
    build_attached_image_marker,
    build_openai_multimodal_content,
    iter_attached_images,
    read_clipboard_payload,
    strip_attached_image_markers,
)


def test_iter_attached_images_extracts_marker_metadata():
    text = 'Analise <attached_image path="/tmp/exemplo.png" mime="image/png" /> agora'

    images = iter_attached_images(text)

    assert len(images) == 1
    assert images[0].path == "/tmp/exemplo.png"
    assert images[0].mime_type == "image/png"


def test_build_openai_multimodal_content_upgrades_marker_to_image_url(tmp_path):
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    marker = build_attached_image_marker(image_path)

    content = build_openai_multimodal_content(f"Veja isto {marker} por favor")

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Veja isto"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2] == {"type": "text", "text": "por favor"}


def test_build_openai_multimodal_content_keeps_marker_when_file_is_missing():
    marker = build_attached_image_marker("/tmp/nao-existe.png")

    content = build_openai_multimodal_content(f"Veja {marker}")

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Veja"}
    assert content[1] == {"type": "text", "text": marker}


def test_strip_attached_image_markers_replaces_with_placeholder():
    marker = build_attached_image_marker("/tmp/quimera-clipboard-x.png")
    text = f"Veja {marker} agora"

    cleaned = strip_attached_image_markers(text)

    assert "<attached_image" not in cleaned
    assert "🖼" in cleaned
    assert cleaned.startswith("Veja ")
    assert cleaned.endswith(" agora")


def test_build_openai_multimodal_content_skips_oversized_image(tmp_path, monkeypatch):
    image_path = tmp_path / "big.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr("quimera.clipboard_support._MAX_INLINE_IMAGE_BYTES", 1)
    marker = build_attached_image_marker(image_path)

    content = build_openai_multimodal_content(f"Veja {marker}")

    # Sem data URL: imagem grande demais para inline, cai no marcador textual.
    assert isinstance(content, list)
    assert all(part.get("type") != "image_url" for part in content)


def test_read_clipboard_payload_prefers_image(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, check, capture_output, text):
        if command[:2] == ["wl-paste", "--no-newline"] and text is False:
            return SimpleNamespace(stdout=b"\x89PNG\r\n\x1a\nfake")
        raise AssertionError(command)

    def fake_write_temp_image(data, mime_type):
        captured["data"] = data
        captured["mime_type"] = mime_type
        path = tmp_path / "clipboard.png"
        path.write_bytes(data)
        return path

    monkeypatch.setattr("quimera.clipboard_support.shutil.which", lambda name: "/usr/bin/wl-paste" if name == "wl-paste" else None)
    monkeypatch.setattr("quimera.clipboard_support.subprocess.run", fake_run)
    monkeypatch.setattr("quimera.clipboard_support._write_temp_image", fake_write_temp_image)

    payload = read_clipboard_payload()

    assert payload is not None
    assert payload.kind == "image"
    assert payload.path == str(tmp_path / "clipboard.png")
    assert payload.mime_type == "image/png"
    assert captured["mime_type"] == "image/png"
    assert "<attached_image" in payload.text


def test_read_clipboard_payload_falls_back_to_text(monkeypatch):
    def fake_run(command, check, capture_output, text):
        if command == ["wl-paste", "--no-newline", "--type", "image/png"]:
            raise FileNotFoundError
        if command == ["wl-paste", "--no-newline", "--type", "image/jpeg"]:
            raise FileNotFoundError
        if command == ["wl-paste", "--no-newline", "--type", "image/webp"]:
            raise FileNotFoundError
        if command == ["wl-paste", "--no-newline"]:
            return SimpleNamespace(stdout="texto do clipboard")
        raise AssertionError(command)

    monkeypatch.setattr("quimera.clipboard_support.shutil.which", lambda name: "/usr/bin/wl-paste" if name == "wl-paste" else None)
    monkeypatch.setattr("quimera.clipboard_support.subprocess.run", fake_run)

    payload = read_clipboard_payload()

    assert payload is not None
    assert payload.kind == "text"
    assert payload.text == "texto do clipboard"

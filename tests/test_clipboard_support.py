from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from quimera.clipboard_support import (
    ClipboardManager,
)


def test_iter_images_extracts_marker_metadata():
    text = 'Analise <attached_image path="/tmp/exemplo.png" mime="image/png" /> agora'

    images = ClipboardManager().iter_images(text)

    assert len(images) == 1
    assert images[0].path == "/tmp/exemplo.png"
    assert images[0].mime_type == "image/png"


def test_to_openai_content_upgrades_marker_to_image_url(tmp_path):
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    clipboard = ClipboardManager()
    marker = clipboard.marker_for(image_path)

    content = clipboard.to_openai_content(f"Veja isto {marker} por favor")

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Veja isto"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2] == {"type": "text", "text": "por favor"}


def test_to_openai_content_keeps_marker_when_file_is_missing():
    clipboard = ClipboardManager()
    marker = clipboard.marker_for("/tmp/nao-existe.png")

    content = clipboard.to_openai_content(f"Veja {marker}")

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Veja"}
    assert content[1] == {"type": "text", "text": marker}


def test_strip_markers_replaces_with_placeholder():
    clipboard = ClipboardManager()
    marker = clipboard.marker_for("/tmp/quimera-clipboard-x.png")
    text = f"Veja {marker} agora"

    cleaned = clipboard.strip_markers(text)

    assert "<attached_image" not in cleaned
    assert "🖼" in cleaned
    assert cleaned.startswith("Veja ")
    assert cleaned.endswith(" agora")


def test_humanize_markers_shows_friendly_label_with_filename():
    clipboard = ClipboardManager()
    marker = clipboard.marker_for("/tmp/quimera-clipboard-abc.png")
    text = f"Olha {marker} aqui"

    humanized = clipboard.humanize_markers(text)

    assert "<attached_image" not in humanized
    assert "🖼 imagem anexada · quimera-clipboard-abc.png" in humanized
    assert humanized.startswith("Olha ")
    assert humanized.endswith(" aqui")


def test_humanize_markers_keeps_plain_text_untouched():
    clipboard = ClipboardManager()
    text = "mensagem sem imagem"

    assert clipboard.humanize_markers(text) == text


def test_humanize_markers_handles_reordered_and_single_quoted_attrs():
    clipboard = ClipboardManager()
    text = "Olha <attached_image mime='image/png' path='/tmp/foto.png'> aqui"

    humanized = clipboard.humanize_markers(text)

    assert "<attached_image" not in humanized
    assert "🖼 imagem anexada · foto.png" in humanized


def test_humanize_markers_falls_back_when_path_missing():
    clipboard = ClipboardManager()
    text = "Olha <attached_image mime=\"image/png\" /> aqui"

    humanized = clipboard.humanize_markers(text)

    assert "<attached_image" not in humanized
    assert "🖼 imagem anexada" in humanized


def test_strip_markers_handles_reordered_attrs():
    clipboard = ClipboardManager()
    text = 'Veja <attached_image mime="image/png" path="/tmp/x.png"> agora'

    cleaned = clipboard.strip_markers(text)

    assert "<attached_image" not in cleaned
    assert "🖼" in cleaned


def test_to_openai_content_skips_oversized_image(tmp_path, monkeypatch):
    image_path = tmp_path / "big.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr(ClipboardManager, "max_inline_image_bytes", 1)
    clipboard = ClipboardManager()
    marker = clipboard.marker_for(image_path)

    content = clipboard.to_openai_content(f"Veja {marker}")

    # Sem data URL: imagem grande demais para inline, cai no marcador textual.
    assert isinstance(content, list)
    assert all(part.get("type") != "image_url" for part in content)


def test_write_temp_uses_quimera_tmp_subtree(tmp_path):
    temp_image_dir = tmp_path / "quimera" / "clipboard"
    clipboard = ClipboardManager(temp_image_dir=temp_image_dir)

    image_path = clipboard.write_temp(
        b"\x89PNG\r\n\x1a\nfake",
        "image/png",
    )

    assert image_path.is_relative_to(temp_image_dir)
    assert image_path.name.startswith("quimera-clipboard-")
    assert image_path.read_bytes().startswith(b"\x89PNG")


def test_read_prefers_image(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, check, capture_output, text):
        if command[:2] == ["wl-paste", "--no-newline"] and text is False:
            return SimpleNamespace(stdout=b"\x89PNG\r\n\x1a\nfake")
        raise AssertionError(command)

    def fake_write_temp(data, mime_type):
        captured["data"] = data
        captured["mime_type"] = mime_type
        path = tmp_path / "clipboard.png"
        path.write_bytes(data)
        return path

    monkeypatch.setattr("quimera.clipboard_support.shutil.which", lambda name: "/usr/bin/wl-paste" if name == "wl-paste" else None)
    monkeypatch.setattr("quimera.clipboard_support.subprocess.run", fake_run)
    monkeypatch.setattr(
        ClipboardManager,
        "write_temp",
        lambda self, data, mime_type: fake_write_temp(data, mime_type),
    )

    payload = ClipboardManager().read()

    assert payload is not None
    assert payload.kind == "image"
    assert payload.path == str(tmp_path / "clipboard.png")
    assert payload.mime_type == "image/png"
    assert captured["mime_type"] == "image/png"
    assert "<attached_image" in payload.text


def test_read_falls_back_to_text(monkeypatch):
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

    payload = ClipboardManager().read()

    assert payload is not None
    assert payload.kind == "text"
    assert payload.text == "texto do clipboard"

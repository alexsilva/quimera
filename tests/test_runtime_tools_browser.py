from __future__ import annotations

import base64
import importlib.util
import shutil
from pathlib import Path

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall, ToolResult
from quimera.runtime.policy import ToolPolicyError
from quimera.runtime.tools.browser.tools import BrowserTool, BrowserToolValidator


def _config(tmp_path: Path) -> ToolRuntimeConfig:
    return ToolRuntimeConfig(workspace_root=tmp_path)


def test_browser_validator_accepts_local_file_inside_workspace(tmp_path: Path):
    page = tmp_path / "index.html"
    page.write_text("<h1>ok</h1>", encoding="utf-8")
    validator = BrowserToolValidator(_config(tmp_path))

    validator.validate(ToolCall("browser_start", {"url": page.as_uri()}))


def test_browser_validator_rejects_local_file_outside_workspace(tmp_path: Path):
    validator = BrowserToolValidator(_config(tmp_path))

    with pytest.raises(ToolPolicyError, match="fora da workspace"):
        validator.validate(ToolCall("browser_start", {"url": Path("/tmp/outside.html").as_uri()}))


def test_browser_validator_requires_click_target(tmp_path: Path):
    validator = BrowserToolValidator(_config(tmp_path))

    with pytest.raises(ToolPolicyError, match="selector.*x.*y"):
        validator.validate(ToolCall("browser_click", {"session_id": "abc"}))


def test_browser_screenshot_path_is_confined_to_workspace(tmp_path: Path):
    validator = BrowserToolValidator(_config(tmp_path))

    with pytest.raises(ToolPolicyError, match="Screenshot fora do diretório de artefatos"):
        validator.validate(
            ToolCall(
                "browser_screenshot",
                {"session_id": "abc", "path": "../../outside.png"},
            )
        )


def test_browser_screenshot_custom_path_stays_inside_session(tmp_path: Path, monkeypatch):
    tool = BrowserTool(_config(tmp_path))

    def execute_screenshot(call, operation, timeout_seconds=30):
        output_path = Path(call.arguments["path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
        return ToolResult(ok=True, tool_name=call.name)

    monkeypatch.setattr(tool, "_execute", execute_screenshot)
    result = tool.browser_screenshot(
        ToolCall(
            "browser_screenshot",
            {"session_id": "session-123", "path": "reports/home.png"},
        )
    )

    expected = (
        tool.config.artifacts_root
        / "browser"
        / "session-123"
        / "reports"
        / "home.png"
    ).resolve()
    assert result.data["path"] == str(expected)
    assert expected.is_file()
    tool.shutdown()


def test_browser_screenshot_includes_native_mcp_image(tmp_path: Path, monkeypatch):
    tool = BrowserTool(_config(tmp_path))
    image_bytes = b"\x89PNG\r\n\x1a\nimage"

    def execute_screenshot(call, operation, timeout_seconds=30):
        output_path = Path(call.arguments["path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(image_bytes)
        return ToolResult(ok=True, tool_name=call.name, data={"bytes": len(image_bytes)})

    monkeypatch.setattr(tool, "_execute", execute_screenshot)
    result = tool.browser_screenshot(
        ToolCall("browser_screenshot", {"session_id": "abc", "path": "shot.png"})
    )

    assert result.ok is True
    assert result.data["mimeType"] == "image/png"
    assert result.data["image_inline"] is True
    assert result.content_blocks[0]["type"] == "image"
    assert result.content_blocks[0]["mimeType"] == "image/png"
    assert base64.b64decode(result.content_blocks[0]["data"]) == image_bytes
    assert Path(result.data["path"]).parent.name == "abc"
    tool.shutdown()


def test_browser_screenshot_omits_oversized_inline_image(tmp_path: Path, monkeypatch):
    tool = BrowserTool(_config(tmp_path))
    monkeypatch.setattr(tool, "max_inline_screenshot_bytes", 3)

    def execute_screenshot(call, operation, timeout_seconds=30):
        output_path = Path(call.arguments["path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"large")
        return ToolResult(ok=True, tool_name=call.name)

    monkeypatch.setattr(tool, "_execute", execute_screenshot)
    result = tool.browser_screenshot(
        ToolCall("browser_screenshot", {"session_id": "abc", "path": "shot.jpg"})
    )

    assert result.ok is True
    assert result.content_blocks == []
    assert result.data["mimeType"] == "image/jpeg"
    assert result.data["image_inline"] is False
    assert "excede o limite" in result.content
    tool.shutdown()


@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None
    or not any(shutil.which(name) for name in ("google-chrome", "chromium", "chromium-browser")),
    reason="Playwright ou Chrome/Chromium indisponível",
)
def test_browser_tool_end_to_end_with_dom_console_and_screenshot(tmp_path: Path):
    page = tmp_path / "game.html"
    page.write_text(
        """
        <!doctype html>
        <html>
          <head><title>Browser Test</title></head>
          <body>
            <input
              id="name"
              oninput="const next = this.cloneNode(true); next.value = this.value; this.replaceWith(next)"
            />
            <button id="go" onclick="window.clicked = true; console.log('clicked')">Go</button>
            <canvas id="game" width="320" height="180"></canvas>
            <script>window.clicked = false; console.log('ready')</script>
          </body>
        </html>
        """,
        encoding="utf-8",
    )
    tool = BrowserTool(_config(tmp_path))
    session_id = None
    try:
        started = tool.browser_start(
            ToolCall(
                "browser_start",
                {"url": page.as_uri(), "width": 800, "height": 600},
            )
        )
        assert started.ok, started.error
        session_id = started.data["session_id"]
        assert started.data["title"] == "Browser Test"

        snapshot = tool.browser_snapshot(
            ToolCall("browser_snapshot", {"session_id": session_id})
        )
        assert snapshot.ok, snapshot.error
        assert any(item["selector"] == "#go" for item in snapshot.data["elements"])
        assert any(item["tag"] == "canvas" for item in snapshot.data["elements"])

        typed = tool.browser_type(
            ToolCall(
                "browser_type",
                {
                    "session_id": session_id,
                    "selector": "#name",
                    "text": "Quimera",
                    "delay_ms": 1,
                },
            )
        )
        assert typed.ok, typed.error
        assert typed.data["value"] == "Quimera"

        clicked = tool.browser_click(
            ToolCall("browser_click", {"session_id": session_id, "selector": "#go"})
        )
        assert clicked.ok, clicked.error

        evaluated = tool.browser_evaluate(
            ToolCall(
                "browser_evaluate",
                {
                    "session_id": session_id,
                    "expression": "() => ({clicked: window.clicked, value: document.querySelector('#name').value})",
                },
            )
        )
        assert evaluated.ok, evaluated.error
        assert evaluated.data["value"] == {"clicked": True, "value": "Quimera"}

        console = tool.browser_console(
            ToolCall("browser_console", {"session_id": session_id, "limit": 20})
        )
        assert console.ok, console.error
        texts = [event.get("text") for event in console.data["events"]]
        assert "ready" in texts
        assert "clicked" in texts

        screenshot = tool.browser_screenshot(
            ToolCall(
                "browser_screenshot",
                {"session_id": session_id, "path": "test.png"},
            )
        )
        assert screenshot.ok, screenshot.error
        output = tmp_path / screenshot.data["path"]
        assert output.is_file()
        assert output.stat().st_size > 0
        assert output.parent.name == session_id
    finally:
        if session_id:
            tool.browser_close(ToolCall("browser_close", {"session_id": session_id}))
        tool.shutdown()
        tool.shutdown()

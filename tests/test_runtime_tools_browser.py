from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
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

    with pytest.raises(ToolPolicyError, match="Path fora da workspace"):
        validator.validate(
            ToolCall(
                "browser_screenshot",
                {"session_id": "abc", "path": "../../outside.png"},
            )
        )


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
                {"session_id": session_id, "path": "artifacts/browser/test.png"},
            )
        )
        assert screenshot.ok, screenshot.error
        output = tmp_path / screenshot.data["path"]
        assert output.is_file()
        assert output.stat().st_size > 0
    finally:
        if session_id:
            tool.browser_close(ToolCall("browser_close", {"session_id": session_id}))
        tool.shutdown()
        tool.shutdown()

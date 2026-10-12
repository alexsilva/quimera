"""Tool ``tui_screenshot``: gate por provider, saída em texto e SVG em disco."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.drivers.tool_schemas import resolve_tool_schemas
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicyError
from quimera.runtime.tools.tui import (
    TuiScreenshotProvider,
    TuiTools,
    TuiToolsValidator,
    _TUI_TOOL_NAMES,
)
from quimera.workspace import Workspace


def _shot(**overrides) -> SimpleNamespace:
    base = {
        "text": " ⭘ Quimera\n Projeto: /tmp/projeto\n\n ●  Alex: oi",
        "svg": "<svg class=\"rich-terminal\"></svg>",
        "width": 80,
        "height": 24,
        "feed": {"scroll_y": 0, "max_scroll_y": 0, "container_height": 19, "slots": 3, "first_slot_y": 1},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _provider(tmp_path: Path, capture=None, log_event=None) -> TuiScreenshotProvider:
    return TuiScreenshotProvider(
        capture=capture or (lambda title=None: _shot()),
        output_dir=tmp_path / "render" / "screenshots",
        session_id="sessao-x",
        log_event=log_event,
    )


def _executor(tmp_path: Path) -> ToolExecutor:
    return ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), approval_handler=None)


def _published(executor: ToolExecutor) -> set[str]:
    return {schema["function"]["name"] for schema in resolve_tool_schemas(executor)}


def test_tool_names_are_registered_and_prefixed():
    assert _TUI_TOOL_NAMES == ["tui_screenshot"]


def test_without_provider_tool_is_unavailable_and_hidden_from_catalog(tmp_path):
    executor = _executor(tmp_path)

    assert executor.is_tui_screenshot_available() is False
    assert "tui_screenshot" not in _published(executor)
    result = executor.execute(ToolCall(name="tui_screenshot", arguments={}))
    assert result.ok is False
    assert "--debug" in str(result.error)


def test_with_provider_tool_is_published_and_returns_screen_text(tmp_path):
    executor = _executor(tmp_path)
    events: list[tuple[str, dict]] = []
    executor.set_tui_screenshot_provider(
        _provider(tmp_path, log_event=lambda event, **payload: events.append((event, payload)))
    )

    assert executor.is_tui_screenshot_available() is True
    assert "tui_screenshot" in _published(executor)

    result = executor.execute(ToolCall(name="tui_screenshot", arguments={}))

    assert result.ok is True, result.error
    header, ruler, *lines = result.content.split("\n")
    assert header.startswith("Tela 80x24 · feed: scroll_y=0 max_scroll_y=0 container_height=19 slots=3 first_slot_y=1 · SVG: ")
    assert set(ruler) == {"─"}
    assert lines == [" ⭘ Quimera", " Projeto: /tmp/projeto", "", " ●  Alex: oi"]
    svg_path = Path(result.data["path"])
    assert svg_path.parent == tmp_path / "render" / "screenshots"
    assert svg_path.name.startswith("sessao-x-") and svg_path.suffix == ".svg"
    assert svg_path.read_text(encoding="utf-8") == "<svg class=\"rich-terminal\"></svg>"
    assert result.data["width"] == 80 and result.data["height"] == 24
    assert result.data["lines"] == 4
    assert result.data["feed"]["slots"] == 3
    assert events == [
        (
            "tui_screenshot",
            {
                "path": str(svg_path),
                "width": 80,
                "height": 24,
                "feed_scroll_y": 0,
                "feed_max_scroll_y": 0,
                "feed_container_height": 19,
                "feed_slots": 3,
                "feed_first_slot_y": 1,
            },
        )
    ]


def test_filename_and_title_are_forwarded(tmp_path):
    executor = _executor(tmp_path)
    titles: list[str | None] = []

    def capture(title=None):
        titles.append(title)
        return _shot()

    executor.set_tui_screenshot_provider(_provider(tmp_path, capture=capture))

    result = executor.execute(
        ToolCall(name="tui_screenshot", arguments={"filename": "depois-do-clear", "title": "Após /clear"})
    )

    assert result.ok is True, result.error
    assert Path(result.data["path"]).name == "depois-do-clear.svg"
    assert titles == ["Após /clear"]
    again = executor.execute(ToolCall(name="tui_screenshot", arguments={"filename": "x.SVG"}))
    assert Path(again.data["path"]).name == "x.SVG"


@pytest.mark.parametrize(
    "arguments",
    [
        {"filename": "../fora"},
        {"filename": "sub/dir.svg"},
        {"filename": ".oculto"},
        {"filename": 7},
        {"title": "t" * 121},
        {"title": ["lista"]},
    ],
)
def test_validator_rejects_paths_and_oversized_titles(tmp_path, arguments):
    validator = TuiToolsValidator(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    with pytest.raises(ToolPolicyError):
        validator.validate(ToolCall(name="tui_screenshot", arguments=arguments))


def test_validator_accepts_plain_names_and_empty_values(tmp_path):
    validator = TuiToolsValidator(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    for arguments in ({}, {"filename": ""}, {"filename": "tela_1.svg"}, {"title": ""}):
        validator.validate(ToolCall(name="tui_screenshot", arguments=arguments))


def test_capture_failure_is_reported_not_raised(tmp_path):
    executor = _executor(tmp_path)

    def capture(title=None):
        raise RuntimeError("A interface Textual não está ativa.")

    executor.set_tui_screenshot_provider(_provider(tmp_path, capture=capture))

    result = executor.execute(ToolCall(name="tui_screenshot", arguments={}))

    assert result.ok is False
    assert "A interface Textual não está ativa." in str(result.error)
    assert not (tmp_path / "render" / "screenshots").exists()


def test_audit_failure_does_not_break_the_tool(tmp_path):
    def log_event(event, **payload):
        raise OSError("disco cheio")

    tools = TuiTools(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    tools.set_screenshot_provider(_provider(tmp_path, log_event=log_event))

    result = tools.tui_screenshot(ToolCall(name="tui_screenshot", arguments={}))

    assert result.ok is True


def test_provider_survives_fork_and_delegation_wiring(tmp_path):
    """Forks do executor e executores de delegação enxergam a mesma captura."""
    executor = _executor(tmp_path)
    executor.set_tui_screenshot_provider(_provider(tmp_path))

    forked = executor.fork_for_concurrent_run()
    assert forked.is_tui_screenshot_available() is True

    delegated = _executor(tmp_path)
    executor._wiring.apply_delegation_to(delegated)
    assert delegated.is_tui_screenshot_available() is True

    executor.set_tui_screenshot_provider(None)
    assert executor.is_tui_screenshot_available() is False
    assert "tui_screenshot" not in _published(executor)


def test_default_filename_never_overwrites_a_previous_capture(tmp_path, monkeypatch):
    import quimera.runtime.tools.tui as tui_module

    monkeypatch.setattr(tui_module, "_default_filename", lambda session_id: f"{session_id}-fixo.svg")
    executor = _executor(tmp_path)
    executor.set_tui_screenshot_provider(_provider(tmp_path))

    first = executor.execute(ToolCall(name="tui_screenshot", arguments={}))
    second = executor.execute(ToolCall(name="tui_screenshot", arguments={}))
    third = executor.execute(ToolCall(name="tui_screenshot", arguments={}))

    assert [Path(r.data["path"]).name for r in (first, second, third)] == [
        "sessao-x-fixo.svg",
        "sessao-x-fixo-1.svg",
        "sessao-x-fixo-2.svg",
    ]
    # Nome explícito sobrescreve, como pedido.
    named = executor.execute(ToolCall(name="tui_screenshot", arguments={"filename": "sessao-x-fixo"}))
    assert Path(named.data["path"]).name == "sessao-x-fixo.svg"


def test_svg_write_failure_is_reported(tmp_path):
    blocker = tmp_path / "render" / "screenshots"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("não sou um diretório", encoding="utf-8")
    executor = _executor(tmp_path)
    executor.set_tui_screenshot_provider(_provider(tmp_path))

    result = executor.execute(ToolCall(name="tui_screenshot", arguments={"filename": "x"}))

    assert result.ok is False
    assert "Falha ao salvar o SVG" in str(result.error)

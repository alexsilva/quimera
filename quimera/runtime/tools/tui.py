"""Ferramentas de diagnóstico da interface Textual (TUI).

Existem só com ``--debug`` e a TUI Textual ativa: o wiring da aplicação injeta
um :class:`TuiScreenshotProvider` e, sem ele, ``tui_screenshot`` sai do
catálogo publicado (capability ``tui_screenshot``) e a chamada direta recusa.
Como o agente não vê o terminal, a tela chega em texto, linha a linha, junto
com a geometria do feed; o SVG (o mesmo que a paleta de comandos do Textual
salva) fica no diretório de screenshots da sessão para o humano abrir.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError
from .base import ToolBase, ValidatableTool

_logger = logging.getLogger(__name__)

_TUI_TOOL_NAMES = ["tui_screenshot"]
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_MAX_TITLE_CHARS = 120
_UNAVAILABLE_ERROR = (
    "tui_screenshot só existe com --debug e a interface Textual ativa."
)


@dataclass(frozen=True)
class TuiScreenshotProvider:
    """Captura injetada pela aplicação.

    ``capture(title=...)`` devolve um objeto com ``text``, ``svg``, ``width``,
    ``height`` e ``feed`` (ver ``quimera.ui.textual.screenshot``). O SVG é
    salvo em ``output_dir`` com o ``session_id`` no nome. ``log_event``, quando
    presente, recebe o evento ``tui_screenshot`` do audit de render.
    """

    capture: Callable[..., Any]
    output_dir: Path
    session_id: str
    log_event: Callable[..., None] | None = None


def _default_filename(session_id: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    millis = int(time.time() * 1000) % 1000
    return f"{session_id}-{stamp}-{millis:03d}.svg"


def _resolve_path(raw: object, output_dir: Path, session_id: str) -> Path:
    """Caminho do SVG: nome pedido (sobrescreve) ou gerado (nunca sobrescreve)."""
    name = str(raw or "").strip()
    if name:
        if not name.lower().endswith(".svg"):
            name = f"{name}.svg"
        return output_dir / name
    default = _default_filename(session_id)
    path = output_dir / default
    counter = 1
    while path.exists():
        path = output_dir / f"{default[:-4]}-{counter}.svg"
        counter += 1
    return path


def _format_summary(shot: Any, feed: dict[str, Any], path: Path) -> str:
    parts = [f"Tela {shot.width}x{shot.height}"]
    if feed:
        feed_parts = " ".join(f"{key}={value}" for key, value in feed.items())
        parts.append(f"feed: {feed_parts}")
    parts.append(f"SVG: {path}")
    return " · ".join(parts)


class TuiTools(
    ToolBase,
    tool_prefix="tui",
    tool_public_methods=("set_screenshot_provider", "is_tui_screenshot_available"),
):
    """Diagnóstico read-only da TUI: nada aqui altera o workspace."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        super().__init__(config)
        self._provider: TuiScreenshotProvider | None = None

    def set_screenshot_provider(self, provider: TuiScreenshotProvider | None) -> None:
        """Injeta (ou remove) a captura de tela fornecida pela aplicação."""
        self._provider = provider

    def is_tui_screenshot_available(self) -> bool:
        """Indica se ``tui_screenshot`` está operável no contexto atual."""
        return self._provider is not None

    def tui_screenshot(self, call: ToolCall) -> ToolResult:
        """Captura a tela atual da TUI: texto no ``content``, SVG em disco."""
        provider = self._provider
        if provider is None:
            return ToolResult(ok=False, tool_name=call.name, error=_UNAVAILABLE_ERROR)
        title = str(call.arguments.get("title") or "").strip() or None
        try:
            shot = provider.capture(title=title)
        except Exception as exc:  # noqa: BLE001 - a UI pode estar encerrando
            return ToolResult(
                ok=False, tool_name=call.name, error=f"Falha ao capturar a tela: {exc}"
            )
        path = _resolve_path(
            call.arguments.get("filename"), Path(provider.output_dir), provider.session_id
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(shot.svg, encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                ok=False, tool_name=call.name, error=f"Falha ao salvar o SVG em {path}: {exc}"
            )
        feed = dict(getattr(shot, "feed", None) or {})
        lines = shot.text.split("\n") if shot.text else []
        summary = _format_summary(shot, feed, path)
        ruler = "─" * max(1, min(int(shot.width), 80))
        content = f"{summary}\n{ruler}\n{shot.text}"
        if provider.log_event is not None:
            try:
                provider.log_event(
                    "tui_screenshot",
                    path=str(path),
                    width=shot.width,
                    height=shot.height,
                    **{f"feed_{key}": value for key, value in feed.items()},
                )
            except Exception as exc:  # noqa: BLE001 - audit nunca derruba a tool
                _logger.debug("tui_screenshot: registrar o evento de audit falhou: %s", exc)
        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=content,
            data={
                "path": str(path),
                "width": shot.width,
                "height": shot.height,
                "lines": len(lines),
                "feed": feed,
            },
        )


class TuiToolsValidator(ValidatableTool):
    """Validação de argumentos antes da execução."""

    def _validate_tui_screenshot(self, call: ToolCall) -> None:
        filename = call.arguments.get("filename")
        if filename is not None:
            if not isinstance(filename, str):
                raise ToolPolicyError("tui_screenshot.filename deve ser string")
            name = filename.strip()
            if name and not _FILENAME_RE.match(name):
                raise ToolPolicyError(
                    "tui_screenshot.filename deve ser um nome simples "
                    "(letras, dígitos, '.', '_' ou '-', sem diretórios)"
                )
        title = call.arguments.get("title")
        if title is not None:
            if not isinstance(title, str):
                raise ToolPolicyError("tui_screenshot.title deve ser string")
            if len(title) > _MAX_TITLE_CHARS:
                raise ToolPolicyError(
                    f"tui_screenshot.title deve ter no máximo {_MAX_TITLE_CHARS} caracteres"
                )


def register(registry, policy, config: ToolRuntimeConfig) -> TuiTools:
    """Registra as tools de diagnóstico da TUI."""
    tools = TuiTools(config)
    validator = TuiToolsValidator(config)
    for name in _TUI_TOOL_NAMES:
        registry.register(name, getattr(tools, name))
    policy.register_tool_validator(_TUI_TOOL_NAMES, validator)
    return tools

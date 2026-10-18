"""Renderer de texto puro: escreve cada mensagem como linhas num stream.

É o renderer dos caminhos sem TUI: ``quimera`` com stdin em pipe (ou
redirecionado) e ``--interactive-test``. Não há cores, janelas nem
transitórios; um transitório (thinking, status em andamento) não cabe num
stream sequencial e é descartado. O que sobra é o que um ``tee``, um log ou
um teste automatizado consegue ler na ordem em que aconteceu.

Com ``--debug`` recebe o ``RenderAuditLogger`` da sessão e grava um evento
``print`` por escrita, com preview, para o ``render-<sessão>.jsonl`` ter o
mesmo conteúdo estruturado que a TUI grava.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Callable, TextIO

import quimera.themes as themes
from quimera.ui.base import RendererBase
from quimera.ui.messages import NO_RESPONSE_MESSAGE, format_error_message
from quimera.ui.text import (
    _agent_style,
    _extract_text_from_renderable,
    _preview_text,
    strip_ansi,
)

_logger = logging.getLogger(__name__)


class PlainTextRenderer(RendererBase):
    """Renderer sequencial de texto puro para stdout (modo pipe e testes)."""

    supports_agent_feed = False
    supports_submission_status = False
    supports_screenshot = False
    supports_structured_agent_activity = False

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        theme: str | None = None,
        get_profile_style: Callable[[str], tuple[str, str] | None] | None = None,
        audit_logger=None,
    ) -> None:
        # ``stream=None`` resolve ``sys.stdout`` a cada escrita, para
        # redirecionamentos e patches feitos depois da construção valerem.
        self._stream = stream
        self._theme = themes.get(theme or themes.DEFAULT_THEME)
        self._get_profile_style = get_profile_style
        self._audit_logger = audit_logger
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------

    def _out(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stdout

    @staticmethod
    def _as_text(value: Any) -> str:
        return strip_ansi(_extract_text_from_renderable(value))

    def _write(self, value: Any, *, kind: str = "generic", **payload: Any) -> None:
        text = self._as_text(value)
        if not text.strip():
            return
        with self._lock:
            out = self._out()
            out.write(text if text.endswith("\n") else text + "\n")
            out.flush()
        self.log_debug_event("print", kind=kind, preview=_preview_text(text), **payload)

    def _label(self, agent: Any) -> str:
        _style, label = _agent_style(agent, self._get_profile_style)
        return label

    # ------------------------------------------------------------------
    # Contrato: exibições
    # ------------------------------------------------------------------

    def show_system(self, message):
        self._write(message, kind="system")

    def show_warning(self, message):
        self._write(f"⚠ {self._as_text(message)}", kind="warning")

    def show_error(self, message, **metadata):
        text = format_error_message(
            self._as_text(message),
            agent=metadata.get("agent"),
            command_name=metadata.get("command_name"),
            error_kind=metadata.get("error_kind"),
            return_code=metadata.get("return_code"),
        )
        self._write(f"✖ {text}", kind="error", **metadata)

    def show_approval(self, message):
        self._write(f"⚠ {self._as_text(message)}", kind="approval")

    def show_feed(self, message, agent=None, muted=False):
        self._write(message, kind="feed", agent=str(agent or ""), muted=bool(muted))

    def show_plain(self, message, agent=None, muted=False):
        self._write(message, kind="plain", agent=str(agent or ""), muted=bool(muted))

    def show_message(self, agent, content, render_mode="auto"):
        text = self._as_text(content).strip()
        label = self._label(agent)
        if "\n" in text:
            self._write(f"{label}:\n{text}", kind="agent_message", agent=str(agent))
        else:
            self._write(f"{label}: {text}", kind="agent_message", agent=str(agent))

    def show_no_response(self, agent):
        self._write(f"{self._label(agent)}: {NO_RESPONSE_MESSAGE}", kind="no_response", agent=str(agent))

    def show_delegation(self, from_agent, to_agent, task=None, *, delegation_id=None, chain=None):
        line = f"delegação {self._label(from_agent)} → {self._label(to_agent)}"
        task_text = self._as_text(task).strip()
        if task_text:
            line = f"{line}: {task_text}"
        self._write(line, kind="delegation", agent=str(from_agent))

    def show_text_window(self, title, content):
        self._write(f"── {self._as_text(title)} ──\n{self._as_text(content)}", kind="text_window")

    def print_direct(self, message):
        self._write(message, kind="direct")

    # ------------------------------------------------------------------
    # Contrato: infraestrutura
    # ------------------------------------------------------------------

    def set_profile_resolver(self, resolver):
        self._get_profile_style = resolver

    def set_audit_logger(self, audit_logger) -> None:
        """Liga o audit de render (``--debug``); só eventos, não há stream ANSI."""
        self._audit_logger = audit_logger

    @property
    def audit_logger(self):
        return self._audit_logger

    def log_debug_event(self, event, **payload):
        audit_logger = self._audit_logger
        if audit_logger is None:
            return None
        try:
            audit_logger.log_event(event, **payload)
        except Exception as exc:
            _logger.debug("audit de render: registrar %s falhou: %s", event, exc)
            return None
        return None

    def flush(self, timeout=5.0):
        with self._lock:
            try:
                self._out().flush()
            except (OSError, ValueError):
                return None
        return None

    def close(self, timeout=5.0):
        self.flush()
        audit_logger = self._audit_logger
        self._audit_logger = None
        if audit_logger is not None:
            try:
                audit_logger.close()
            except Exception as exc:
                _logger.debug("audit de render: fechar falhou: %s", exc)
                return None
        return None

    # ------------------------------------------------------------------
    # Temas: o renderer não estiliza, mas guarda o nome para /theme persistir
    # ------------------------------------------------------------------

    @property
    def theme_name(self) -> str:
        return self._theme.name

    def set_theme(self, name: str) -> str:
        self._theme = themes.get(name)
        return self._theme.name

    def cycle_theme(self) -> str:
        all_names = themes.names()
        try:
            idx = all_names.index(self._theme.name)
        except ValueError:
            idx = 0
        next_name = all_names[(idx + 1) % len(all_names)]
        self._theme = themes.get(next_name)
        return next_name

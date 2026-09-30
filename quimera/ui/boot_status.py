"""Linhas de status do bloco de inicialização, compartilhadas entre renderers.

Módulo deliberadamente sem dependências internas (como ``quimera.ui.messages``):
é importado pelo contrato base de renderers e pela UI Textual. Um bloco de
status é identificado por uma chave e composto por linhas ``(status, texto)``;
renderers com slot atualizável (Textual) substituem o bloco no lugar, os
demais imprimem apenas as linhas que mudaram.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

BOOT_STATUS_OK = "ok"
BOOT_STATUS_BUSY = "busy"
BOOT_STATUS_PENDING = "pending"
BOOT_STATUS_ERROR = "error"
BOOT_STATUS_OFF = "off"
BOOT_STATUS_INFO = "info"

#: Glifo textual de cada status, usado por todos os renderers.
BOOT_STATUS_GLYPHS = {
    BOOT_STATUS_OK: "●",
    BOOT_STATUS_BUSY: "◌",
    BOOT_STATUS_PENDING: "○",
    BOOT_STATUS_ERROR: "✗",
    BOOT_STATUS_OFF: "○",
    BOOT_STATUS_INFO: "·",
}

#: Estilo Rich do glifo de cada status (só renderers com cor usam).
BOOT_STATUS_STYLES = {
    BOOT_STATUS_OK: "green",
    BOOT_STATUS_BUSY: "yellow",
    BOOT_STATUS_PENDING: "dim",
    BOOT_STATUS_ERROR: "red",
    BOOT_STATUS_OFF: "dim",
    BOOT_STATUS_INFO: "dim",
}


@dataclass(frozen=True)
class BootStatusLine:
    """Uma linha de um bloco de status do boot.

    ``url`` é um link acionável exibido depois do texto (ex.: autorização
    OAuth pendente). Renderers com mouse o tornam clicável; os sequenciais
    imprimem a URL completa, para que possa ser copiada.
    """

    status: str
    text: str
    url: str = ""

    def as_payload(self) -> dict[str, str]:
        """Forma serializável para eventos de UI."""
        payload = {"status": self.status, "text": self.text}
        if self.url:
            payload["url"] = self.url
        return payload


def coerce_boot_status_line(value: object) -> BootStatusLine:
    """Aceita ``BootStatusLine``, tupla ``(status, texto[, url])``, dict ou string."""
    url = ""
    if isinstance(value, BootStatusLine):
        return value
    if isinstance(value, dict):
        status = str(value.get("status") or BOOT_STATUS_INFO)
        text = str(value.get("text") or "")
        url = str(value.get("url") or "")
    elif isinstance(value, (tuple, list)) and len(value) in {2, 3}:
        status, text = str(value[0]), str(value[1])
        url = str(value[2] or "") if len(value) == 3 else ""
    else:
        status, text = BOOT_STATUS_INFO, str(value)
    if status not in BOOT_STATUS_GLYPHS:
        status = BOOT_STATUS_INFO
    return BootStatusLine(status=status, text=text, url=url)


def coerce_boot_status_lines(lines: Iterable[object] | object) -> list[BootStatusLine]:
    """Normaliza qualquer coleção de linhas (ou uma linha solta) para a forma tipada."""
    if isinstance(lines, (str, BootStatusLine, dict)):
        return [coerce_boot_status_line(lines)]
    return [coerce_boot_status_line(item) for item in (lines or [])]


def format_boot_status_line(line: BootStatusLine) -> str:
    """Texto plano da linha: glifo do status, mensagem e, se houver, a URL completa."""
    glyph = BOOT_STATUS_GLYPHS.get(line.status, BOOT_STATUS_GLYPHS[BOOT_STATUS_INFO])
    if line.url:
        return f"{glyph} {line.text} {line.url}".rstrip()
    return f"{glyph} {line.text}"

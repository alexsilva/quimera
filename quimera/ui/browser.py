"""Abertura de URLs no navegador do usuário sem tocar no terminal.

O ``webbrowser`` da stdlib não serve para uma TUI: sem ``DISPLAY`` ele cai em
navegadores de console (``lynx``, ``w3m``…) que tomariam o terminal do
Textual, e o ``xdg-open`` que ele lança herda ``stdout``/``stderr`` do
processo. Aqui só abrimos um navegador gráfico quando há sessão gráfica e o
lançador roda desacoplado, com toda a saída descartada.

``QUIMERA_OPEN_BROWSER=0`` desliga a abertura automática (útil em SSH com
``DISPLAY`` encaminhado, quando o usuário prefere abrir o link localmente).
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import sys

from quimera import process_factory as subprocess

logger = logging.getLogger(__name__)

OPEN_BROWSER_ENV = "QUIMERA_OPEN_BROWSER"
_DISABLED_VALUES = {"0", "false", "no", "off"}
_LINUX_OPENERS: tuple[tuple[str, ...], ...] = (
    ("xdg-open",),
    ("gio", "open"),
    ("x-www-browser",),
    ("sensible-browser",),
)


def browser_opening_enabled(environ: dict[str, str] | None = None) -> bool:
    """False quando ``QUIMERA_OPEN_BROWSER`` desliga a abertura automática."""
    env = os.environ if environ is None else environ
    return str(env.get(OPEN_BROWSER_ENV, "")).strip().lower() not in _DISABLED_VALUES


def browser_available(environ: dict[str, str] | None = None) -> bool:
    """Há um navegador gráfico alcançável a partir deste processo?"""
    env = os.environ if environ is None else environ
    if not browser_opening_enabled(env):
        return False
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def _opener_command(url: str, environ: dict[str, str]) -> list[str] | None:
    browser = str(environ.get("BROWSER", "")).strip()
    if browser:
        parts = shlex.split(browser)
        if parts:
            if any("%s" in part for part in parts):
                return [part.replace("%s", url) for part in parts]
            return [*parts, url]
    if sys.platform == "darwin":
        return ["open", url]
    if sys.platform.startswith("win"):
        return ["cmd", "/c", "start", "", url]
    for candidate in _LINUX_OPENERS:
        if shutil.which(candidate[0]):
            return [*candidate, url]
    return None


def open_in_browser(url: str, environ: dict[str, str] | None = None) -> bool:
    """Abre ``url`` no navegador gráfico; True se o lançador foi disparado.

    Nunca levanta exceção e nunca escreve no terminal: o lançador roda em
    sessão própria com ``stdin``/``stdout``/``stderr`` descartados. Só URLs
    ``http(s)`` são aceitas.
    """
    env = os.environ if environ is None else environ
    target = str(url or "").strip()
    if not target.startswith(("http://", "https://")):
        return False
    if not browser_available(env):
        logger.info("navegador indisponível para abrir %s", target)
        return False
    command = _opener_command(target, env)
    if command is None:
        logger.info("nenhum lançador de navegador encontrado para %s", target)
        return False
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        logger.info("falha ao abrir o navegador com %s: %s", command[0], exc)
        return False
    logger.info("navegador aberto via %s", command[0])
    return True

"""Filtros e detecções de texto para saída de agentes externos."""
import re
from functools import lru_cache

import quimera.plugins as plugins

_BRALLE_RANGE = re.compile(r'[\u2800-\u28FF]')
_ANSI_ESCAPE = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')
_RATE_LIMIT_RE = re.compile(
    r"""
    \brate[\s-]?limit(?:ed|ing)?\b
    | \btoo\ many\ requests\b
    | \bthrottl(?:e|ed|ing)?\b
    | \b(?:http|status|status code|code)\b[^\n]{0,20}\b429\b
    | \b429\b[^\n]{0,20}\btoo\ many\ requests\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_RATE_LIMIT_YIELD_SECONDS = 5  # grace period after rate limit detection before yielding to other agents


def _strip_spinner(text: str) -> str:
    """Remove caracteres Braille de spinner do texto."""
    return _BRALLE_RANGE.sub('', text)


@lru_cache(maxsize=32)
def _compile_noise_patterns(patterns: tuple) -> tuple:
    """Compila e armazena em cache os padrões regex de ruído de stderr."""
    return tuple(re.compile(p) for p in patterns)


def _should_ignore_stderr_line(agent: str | None, line: str) -> bool:
    """Filtra ruído conhecido de stderr que não representa erro real."""
    if not agent:
        return False
    plugin = plugins.get(agent)
    if not plugin:
        return False
    cleaned = _ANSI_ESCAPE.sub("", _strip_spinner(line)).replace("\r", "").strip()
    if plugin.stderr_noise and cleaned in plugin.stderr_noise:
        return True
    if plugin.stderr_noise_patterns:
        compiled = _compile_noise_patterns(plugin.stderr_noise_patterns)
        return any(p.search(cleaned) for p in compiled)
    return False


def _filter_stderr_lines(agent: str | None, lines: list[str]) -> list[str]:
    """Remove linhas de stderr conhecidas como ruído para o agente."""
    return [line for line in lines if not _should_ignore_stderr_line(agent, line)]


def _is_rate_limit_signal(text: str | None) -> bool:
    """Detecta sinais explícitos de rate limit sem tratar qualquer `429` isolado como limite."""
    if not text:
        return False
    return bool(_RATE_LIMIT_RE.search(text))

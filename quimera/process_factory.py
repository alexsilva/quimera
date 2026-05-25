"""Camada única de subprocesso para o Quimera.

Expõe os mesmos pontos de entrada do stdlib subprocess
(`Popen`, `run`, constantes e exceções) e helpers de uso recorrente para
concentrar a lógica de execução em um só lugar.

Uso nos consumidores:
    from quimera import process_factory as subprocess
"""

from __future__ import annotations

import subprocess as _subprocess
from typing import Any, TypeAlias


ProcessHandle: TypeAlias = _subprocess.Popen[Any]

# Constantes — iguais ao stdlib
PIPE = _subprocess.PIPE
DEVNULL = _subprocess.DEVNULL
STDOUT = _subprocess.STDOUT

# Exceções e tipos — iguais ao stdlib
TimeoutExpired = _subprocess.TimeoutExpired
CalledProcessError = _subprocess.CalledProcessError
CompletedProcess = _subprocess.CompletedProcess
Popen = _subprocess.Popen


def run(*popenargs: Any, **kwargs: Any) -> _subprocess.CompletedProcess:
    """Executa um subprocesso e aguarda conclusão."""
    return _subprocess.run(*popenargs, **kwargs)


def popen_text(*popenargs: Any, **kwargs: Any) -> ProcessHandle:
    """Cria Popen textual com pipes e buffer de linha por padrão.

    Defaults:
    - stdin/stdout/stderr = PIPE
    - text = True
    - bufsize = 1
    """
    kwargs.setdefault("stdin", PIPE)
    kwargs.setdefault("stdout", PIPE)
    kwargs.setdefault("stderr", PIPE)
    kwargs.setdefault("text", True)
    kwargs.setdefault("bufsize", 1)
    return _subprocess.Popen(*popenargs, **kwargs)


def read_text(*popenargs: Any, **kwargs: Any) -> str:
    """Executa um subprocesso e retorna stdout como texto.

    Default: text=True.
    Lança CalledProcessError se o processo terminar com código != 0.
    """
    kwargs.setdefault("text", True)
    return _subprocess.check_output(*popenargs, **kwargs)

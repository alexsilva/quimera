"""Versão única da aplicação, gerada pelo Git durante o empacotamento."""
from importlib import metadata


PACKAGE_NAME = "quimera"
UNKNOWN_VERSION = "0.0.0+unknown"


def resolve_version() -> str:
    """Retorna a versão registrada no metadata do pacote instalado."""
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return UNKNOWN_VERSION


__version__ = resolve_version()


__all__ = ["__version__", "resolve_version"]

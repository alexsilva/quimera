"""Componentes de `quimera.runtime.drivers.__init__`."""


def __getattr__(name: str):
    if name == "OpenAICompatDriver":
        from .openai_compat import OpenAICompatDriver

        return OpenAICompatDriver
    raise AttributeError(name)

__all__ = ["OpenAICompatDriver"]

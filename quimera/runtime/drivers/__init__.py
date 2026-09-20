"""Componentes de `quimera.runtime.drivers.__init__`."""


def __getattr__(name: str):
    if name == "OpenAICompatDriver":
        from .openai_compat import OpenAICompatDriver

        return OpenAICompatDriver
    if name == "ToolCallingDriver":
        from .openai_compat import ToolCallingDriver

        return ToolCallingDriver
    if name == "CloudDriver":
        from .cloud import CloudDriver

        return CloudDriver
    if name == "create_api_driver":
        from .factory import create_api_driver

        return create_api_driver
    if name == "CodexCloudDriver":
        from .codexcloud import CodexCloudDriver

        return CodexCloudDriver
    if name == "ClaudeCloudDriver":
        from .claudecloud import ClaudeCloudDriver

        return ClaudeCloudDriver
    raise AttributeError(name)

__all__ = [
    "ClaudeCloudDriver",
    "CloudDriver",
    "CodexCloudDriver",
    "OpenAICompatDriver",
    "ToolCallingDriver",
    "create_api_driver",
]

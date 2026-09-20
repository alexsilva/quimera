"""Factory única para drivers de conexões API."""
from __future__ import annotations

from .cloud import create_cloud_driver, is_cloud_provider
from .openai_compat import OpenAICompatDriver


def create_api_driver(
    provider: str,
    *,
    api_key: str = "ollama",
    runtime_secrets=None,
    openai_driver_cls=None,
    **kwargs,
):
    """Constrói OpenAI-compatible ou cloud sem expor classes ao consumidor."""
    if is_cloud_provider(provider):
        return create_cloud_driver(
            provider,
            runtime_secrets=runtime_secrets,
            **kwargs,
        )
    driver_cls = openai_driver_cls or OpenAICompatDriver
    return driver_cls(api_key=api_key, **kwargs)

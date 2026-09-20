"""Contrato arquitetural do motor cloud compartilhado."""
from __future__ import annotations

import httpx

from quimera.runtime.drivers.claudecloud import (
    ClaudeCloudBackend,
    ClaudeCloudDriver,
)
from quimera.runtime.drivers.cloud import (
    CloudDriver,
    cloud_backend_ids,
    cloud_provider_options,
    create_cloud_driver,
)
from quimera.runtime.drivers.codexcloud import CodexCloudBackend, CodexCloudDriver
from quimera.runtime.drivers.factory import create_api_driver
from quimera.runtime.drivers.openai_compat import ToolCallingDriver


class _CodexAuth:
    def credentials(self, *, force_refresh=False):
        return "token", "account"


class _ClaudeAuth:
    def credentials(self, *, force_refresh=False):
        return "token"


def _http_client():
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))


def test_cloud_registry_is_the_single_source_for_factory_and_ui():
    assert cloud_backend_ids() == ("codexcloud", "claudecloud")
    assert cloud_provider_options() == (
        ("Codex Cloud", "codexcloud"),
        ("Claude Cloud", "claudecloud"),
    )


def test_both_legacy_constructors_return_the_same_concrete_driver():
    codex = CodexCloudDriver(
        model="gpt-5.5",
        auth=_CodexAuth(),
        http_client=_http_client(),
    )
    claude = ClaudeCloudDriver(
        model="claude-sonnet-4-5",
        auth=_ClaudeAuth(),
        http_client=_http_client(),
    )

    assert type(codex) is CloudDriver
    assert type(claude) is CloudDriver
    assert isinstance(codex, ToolCallingDriver)
    assert isinstance(claude, ToolCallingDriver)
    assert isinstance(codex._backend, CodexCloudBackend)
    assert isinstance(claude._backend, ClaudeCloudBackend)
    assert "_client" not in codex.__dict__
    assert "_client" not in claude.__dict__


def test_unknown_backend_is_rejected_by_central_factory():
    try:
        create_cloud_driver("missing", model="x")
    except ValueError as exc:
        assert "backend cloud desconhecido" in str(exc)
    else:  # pragma: no cover - defesa do contrato
        raise AssertionError("factory aceitou backend não registrado")


def test_api_factory_routes_cloud_and_openai_compatible():
    cloud = create_api_driver(
        "codexcloud",
        model="gpt-5.5",
        auth=_CodexAuth(),
        http_client=_http_client(),
    )
    calls = []

    def openai_driver(**kwargs):
        calls.append(kwargs)
        return "openai-driver"

    compat = create_api_driver(
        "openai_compat",
        model="local",
        base_url="http://localhost/v1",
        api_key="secret",
        openai_driver_cls=openai_driver,
    )

    assert type(cloud) is CloudDriver
    assert compat == "openai-driver"
    assert calls[0]["api_key"] == "secret"

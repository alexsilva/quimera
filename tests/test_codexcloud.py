"""Testes do driver/perfil codexcloud (backend Codex via conta do Codex CLI).

Nenhum teste faz chamada de rede real: HTTP é simulado com httpx.MockTransport.
"""
from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from quimera import profiles
from quimera.profiles.base import OpenAIConnection
from quimera.profiles.codexcloud import (
    CODEX_CLOUD_BASE_URL,
    CodexCloudProfile,
    _codex_config_defaults,
)
from quimera.runtime.codex_auth import CodexAuthError, CodexCloudAuth
from quimera.runtime.drivers.codexcloud import (
    PREAMBLE_INSTRUCTIONS,
    CodexCloudDriver,
    _chat_tools_to_responses_tools,
)
from quimera.runtime.drivers.openai_compat import (
    FatalAPIError,
    TransientAPIError,
    _categorize_api_exception,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_jwt(exp: float) -> str:
    """Gera um JWT não assinado com claim exp."""
    def _b64(data: dict) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_b64({'alg': 'none'})}.{_b64({'exp': exp})}.sig"


def _write_auth_file(tmp_path, access_token, refresh_token="refresh-1", account_id="acc-1"):
    auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "id-1",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": "2026-01-01T00:00:00.000Z",
    }
    (tmp_path / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    return auth


def _sse(events: list[dict]) -> str:
    lines = []
    for event in events:
        lines.append(f"event: {event['type']}")
        lines.append(f"data: {json.dumps(event)}")
        lines.append("")
    return "\n".join(lines) + "\n"


class _FakeAuth:
    def __init__(self):
        self.calls: list[bool] = []

    def credentials(self, *, force_refresh: bool = False):
        self.calls.append(force_refresh)
        return "token-abc", "acc-xyz"


def _make_driver(handler, auth=None, extra_body=None) -> CodexCloudDriver:
    transport = httpx.MockTransport(handler)
    return CodexCloudDriver(
        model="gpt-5.5",
        extra_body=extra_body,
        auth=auth or _FakeAuth(),
        http_client=httpx.Client(transport=transport),
    )


# ---------------------------------------------------------------------------
# CodexCloudAuth
# ---------------------------------------------------------------------------

def test_auth_returns_valid_token_without_refresh(tmp_path):
    token = _fake_jwt(time.time() + 3600)
    _write_auth_file(tmp_path, token)
    auth = CodexCloudAuth(codex_home=tmp_path)

    access_token, account_id = auth.credentials()

    assert access_token == token
    assert account_id == "acc-1"


def test_auth_refreshes_expired_token_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-from-environment")
    expired = _fake_jwt(time.time() - 10)
    fresh = _fake_jwt(time.time() + 3600)
    _write_auth_file(tmp_path, expired, refresh_token="refresh-old")
    requests_seen = []

    def handler(request):
        requests_seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "access_token": fresh,
            "id_token": "id-2",
            "refresh_token": "refresh-new",
        })

    auth = CodexCloudAuth(
        codex_home=tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    access_token, account_id = auth.credentials()

    assert access_token == fresh
    assert account_id == "acc-1"
    assert requests_seen[0]["grant_type"] == "refresh_token"
    assert requests_seen[0]["client_id"] == "client-from-environment"
    assert requests_seen[0]["refresh_token"] == "refresh-old"
    persisted = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    assert persisted["tokens"]["access_token"] == fresh
    assert persisted["tokens"]["refresh_token"] == "refresh-new"
    assert persisted["last_refresh"].endswith("Z")


def test_auth_force_refresh_ignores_valid_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-from-environment")
    valid = _fake_jwt(time.time() + 3600)
    fresh = _fake_jwt(time.time() + 7200)
    _write_auth_file(tmp_path, valid)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"access_token": fresh})

    auth = CodexCloudAuth(
        codex_home=tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    access_token, _ = auth.credentials(force_refresh=True)

    assert access_token == fresh
    assert calls == ["/oauth/token"]


def test_auth_missing_file_raises(tmp_path):
    auth = CodexCloudAuth(codex_home=tmp_path)
    with pytest.raises(CodexAuthError, match="codex login"):
        auth.credentials()


def test_auth_refresh_http_error_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_OAUTH_CLIENT_ID", "client-from-environment")
    _write_auth_file(tmp_path, _fake_jwt(time.time() - 10))

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    auth = CodexCloudAuth(
        codex_home=tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(CodexAuthError, match="HTTP 400"):
        auth.credentials()


def test_auth_refresh_requires_client_id_environment_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_OAUTH_CLIENT_ID", raising=False)
    _write_auth_file(tmp_path, _fake_jwt(time.time() - 10))

    auth = CodexCloudAuth(codex_home=tmp_path)

    with pytest.raises(CodexAuthError, match="CODEX_OAUTH_CLIENT_ID"):
        auth.credentials()


def test_auth_rejects_api_key_only_login(tmp_path):
    (tmp_path / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-test", "tokens": None}),
        encoding="utf-8",
    )
    auth = CodexCloudAuth(codex_home=tmp_path)
    with pytest.raises(CodexAuthError, match="tokens de login ChatGPT"):
        auth.credentials()


# ---------------------------------------------------------------------------
# Conversão chat -> Responses
# ---------------------------------------------------------------------------

def test_build_responses_payload_maps_roles_and_tools():
    driver = _make_driver(lambda request: httpx.Response(500))
    driver._remember_reasoning("call-1", {"type": "reasoning", "id": "rs-1", "summary": []})
    messages = [
        {"role": "system", "content": "regra 1"},
        {"role": "system", "content": "regra 2"},
        {"role": "user", "content": "faz X"},
        {
            "role": "assistant",
            "content": "vou ler o arquivo",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"ok": true}'},
    ]
    tools = [{"type": "function", "function": {
        "name": "read_file", "description": "lê", "parameters": {"type": "object"},
    }}]

    body = driver._build_responses_payload(messages, tools)

    assert body["model"] == "gpt-5.5"
    assert body["instructions"] == "regra 1\n\nregra 2\n\n" + PREAMBLE_INSTRUCTIONS
    assert body["store"] is False and body["stream"] is True
    types = [item["type"] for item in body["input"]]
    assert types == ["message", "message", "reasoning", "function_call", "function_call_output"]
    function_call = body["input"][3]
    assert function_call["call_id"] == "call-1"
    assert function_call["name"] == "read_file"
    assert body["tools"] == [{
        "type": "function",
        "name": "read_file",
        "description": "lê",
        "parameters": {"type": "object"},
        "strict": False,
    }]
    driver.close()


def test_build_responses_payload_appends_preamble_without_system():
    driver = _make_driver(lambda request: httpx.Response(500))
    body = driver._build_responses_payload([{"role": "user", "content": "oi"}], [])
    assert body["instructions"] == PREAMBLE_INSTRUCTIONS
    driver.close()


def test_build_responses_payload_merges_extra_body_and_multimodal():
    driver = _make_driver(
        lambda request: httpx.Response(500),
        extra_body={"reasoning": {"effort": "xhigh", "summary": "auto"}},
    )
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "olha essa imagem"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ],
    }]

    body = driver._build_responses_payload(messages, [])

    assert body["reasoning"] == {"effort": "xhigh", "summary": "auto"}
    parts = body["input"][0]["content"]
    assert parts == [
        {"type": "input_text", "text": "olha essa imagem"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
    ]
    driver.close()


def test_chat_tools_to_responses_tools_ignores_malformed_entries():
    assert _chat_tools_to_responses_tools([{"type": "function"}, "junk"]) == []


# ---------------------------------------------------------------------------
# Streaming SSE
# ---------------------------------------------------------------------------

def test_responses_turn_streams_text_and_reasoning():
    events = [
        {"type": "response.created"},
        {"type": "response.reasoning_summary_text.delta", "delta": "pensando"},
        {"type": "response.output_text.delta", "delta": "Olá"},
        {"type": "response.output_text.delta", "delta": ", mundo"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 2}}},
    ]

    def handler(request):
        assert request.headers["authorization"] == "Bearer token-abc"
        assert request.headers["chatgpt-account-id"] == "acc-xyz"
        assert request.url.path.endswith("/responses")
        return httpx.Response(200, text=_sse(events), headers={"content-type": "text/event-stream"})

    driver = _make_driver(handler)
    chunks: list[str] = []

    text, tool_calls = driver._responses_turn(
        [{"role": "user", "content": "oi"}], [], on_text_chunk=chunks.append
    )

    assert text == "Olá, mundo"
    assert tool_calls == []
    assert chunks == ["<think>pensando", "</think>", "Olá", ", mundo"]
    driver.close()


def test_responses_turn_streams_commentary_as_thinking():
    """Modelos gpt-5.6-* narram progresso via message phase=commentary.

    Sequência real capturada do backend Codex com gpt-5.6-sol: o commentary
    chega como response.output_text.delta de um item message cujo added traz
    phase=commentary. Deve virar bloco <think> e ficar fora do texto final.
    """
    events = [
        {"type": "response.created"},
        {"type": "response.output_item.added", "item": {
            "type": "message", "phase": "commentary", "id": "msg-c1",
        }},
        {"type": "response.output_text.delta", "item_id": "msg-c1", "delta": "Vou listar"},
        {"type": "response.output_text.delta", "item_id": "msg-c1", "delta": " os arquivos."},
        {"type": "response.output_item.done", "item": {
            "type": "message", "phase": "commentary", "id": "msg-c1",
        }},
        {"type": "response.output_item.added", "item": {
            "type": "message", "phase": "final_answer", "id": "msg-f1",
        }},
        {"type": "response.output_text.delta", "item_id": "msg-f1", "delta": "São 3 arquivos."},
        {"type": "response.completed", "response": {"usage": {}}},
    ]
    driver = _make_driver(lambda request: httpx.Response(200, text=_sse(events)))
    chunks: list[str] = []

    text, tool_calls = driver._responses_turn(
        [{"role": "user", "content": "oi"}], [], on_text_chunk=chunks.append
    )

    assert text == "São 3 arquivos."
    assert tool_calls == []
    assert chunks == ["<think>Vou listar", " os arquivos.", "</think>", "São 3 arquivos."]
    driver.close()


def test_responses_turn_closes_commentary_before_function_call():
    """Commentary seguido de function_call fecha o <think> na fronteira do item."""
    events = [
        {"type": "response.output_item.added", "item": {
            "type": "message", "phase": "commentary", "id": "msg-c1",
        }},
        {"type": "response.output_text.delta", "item_id": "msg-c1", "delta": "Consultando src."},
        {"type": "response.output_item.done", "item": {
            "type": "message", "phase": "commentary", "id": "msg-c1",
        }},
        {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "call-1",
            "name": "listar_arquivos", "arguments": '{"path": "src"}',
        }},
        {"type": "response.completed", "response": {"usage": {}}},
    ]
    driver = _make_driver(lambda request: httpx.Response(200, text=_sse(events)))
    chunks: list[str] = []

    text, tool_calls = driver._responses_turn(
        [{"role": "user", "content": "oi"}], [], on_text_chunk=chunks.append
    )

    assert text == ""
    assert chunks == ["<think>Consultando src.", "</think>"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call-1"
    driver.close()


def test_responses_turn_collects_function_calls_and_reasoning_items():
    reasoning_item = {"type": "reasoning", "id": "rs-1", "encrypted_content": "enc"}
    events = [
        {"type": "response.output_item.done", "item": reasoning_item},
        {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "call-9",
            "name": "read_file", "arguments": '{"path": "x"}',
        }},
        {"type": "response.completed", "response": {"usage": {}}},
    ]
    driver = _make_driver(lambda request: httpx.Response(200, text=_sse(events)))

    text, tool_calls = driver._responses_turn([{"role": "user", "content": "oi"}], [])

    assert text == ""
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call-9"
    assert tool_calls[0]["arguments"] == {"path": "x"}
    assert tool_calls[0]["argument_error"] is None
    assert driver._reasoning_items["call-9"] == reasoning_item
    driver.close()


def test_responses_turn_reports_invalid_tool_arguments():
    events = [
        {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "call-1",
            "name": "read_file", "arguments": "{invalid",
        }},
    ]
    driver = _make_driver(lambda request: httpx.Response(200, text=_sse(events)))

    _, tool_calls = driver._responses_turn([{"role": "user", "content": "oi"}], [])

    assert tool_calls[0]["argument_error"] is not None
    driver.close()


def test_responses_turn_retries_once_on_401_with_forced_refresh():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, text=_sse([
            {"type": "response.output_text.delta", "delta": "ok"},
        ]))

    auth = _FakeAuth()
    driver = _make_driver(handler, auth=auth)

    text, _ = driver._responses_turn([{"role": "user", "content": "oi"}], [])

    assert text == "ok"
    assert auth.calls == [False, True]
    driver.close()


def test_responses_turn_maps_http_errors():
    driver_429 = _make_driver(lambda request: httpx.Response(
        429, headers={"retry-after": "7"}, text="slow down"
    ))
    with pytest.raises(TransientAPIError) as excinfo:
        driver_429._responses_turn([{"role": "user", "content": "oi"}], [])
    assert excinfo.value.rate_limited is True
    assert excinfo.value.retry_after == 7.0
    driver_429.close()

    driver_500 = _make_driver(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(TransientAPIError):
        driver_500._responses_turn([{"role": "user", "content": "oi"}], [])
    driver_500.close()

    driver_400 = _make_driver(lambda request: httpx.Response(400, text="bad request"))
    with pytest.raises(FatalAPIError):
        driver_400._responses_turn([{"role": "user", "content": "oi"}], [])
    driver_400.close()


def test_responses_turn_response_failed_event_raises():
    events = [{
        "type": "response.failed",
        "response": {"error": {"code": "server_error", "message": "explodiu"}},
    }]
    driver = _make_driver(lambda request: httpx.Response(200, text=_sse(events)))
    with pytest.raises(FatalAPIError, match="explodiu"):
        driver._responses_turn([{"role": "user", "content": "oi"}], [])
    driver.close()


def test_responses_turn_network_error_is_transient():
    def handler(request):
        raise httpx.ConnectError("sem rede", request=request)

    driver = _make_driver(handler)
    with pytest.raises(TransientAPIError):
        driver._responses_turn([{"role": "user", "content": "oi"}], [])
    driver.close()


def test_categorize_passes_through_precategorized_errors():
    transient = TransientAPIError("x", rate_limited=True, retry_after=3.0)
    assert _categorize_api_exception(transient) == ("rate_limit", 3.0)
    assert _categorize_api_exception(TransientAPIError("x")) == ("transient", None)
    assert _categorize_api_exception(FatalAPIError("x")) == ("fatal", None)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def test_codexcloud_profile_registered_with_api_driver():
    profile = profiles.get("codexcloud")
    assert isinstance(profile, CodexCloudProfile)
    assert profile.driver == "codexcloud"
    assert profile.has_builtin_tools is False
    assert profile.supports_tools is True
    connection = profile.effective_connection()
    assert isinstance(connection, OpenAIConnection)
    assert connection.provider == "codexcloud"
    assert connection.base_url == CODEX_CLOUD_BASE_URL
    assert not connection.api_key_env
    assert connection.extra_body["reasoning"]["summary"] == "auto"


def test_codexcloud_profile_configure_with_model_returns_api_connection():
    profile = profiles.get("codexcloud")
    connection = profile.configure_with_model("gpt-5.5")
    assert isinstance(connection, OpenAIConnection)
    assert connection.model == "gpt-5.5"
    assert connection.provider == "codexcloud"
    assert connection.base_url == CODEX_CLOUD_BASE_URL
    assert connection.extra_body["reasoning"]["summary"] == "auto"


def test_codex_config_defaults_fallback_without_config(monkeypatch, tmp_path):
    monkeypatch.setattr("quimera.profiles.codexcloud.Path.home", lambda: tmp_path)
    _codex_config_defaults.cache_clear()
    try:
        model, effort = _codex_config_defaults()
        assert model == "gpt-5.5"
        assert effort == "medium"
    finally:
        _codex_config_defaults.cache_clear()


def test_codex_config_defaults_reads_codex_config(monkeypatch, tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        'model = "gpt-6"\nmodel_reasoning_effort = "xhigh"\n', encoding="utf-8"
    )
    monkeypatch.setattr("quimera.profiles.codexcloud.Path.home", lambda: codex_dir.parent)
    _codex_config_defaults.cache_clear()
    try:
        model, effort = _codex_config_defaults()
        assert model == "gpt-6"
        assert effort == "xhigh"
    finally:
        _codex_config_defaults.cache_clear()

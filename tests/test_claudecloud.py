"""Testes do driver/perfil claudecloud (Anthropic via conta do Claude Code).

Nenhum teste faz chamada de rede real: HTTP é simulado com httpx.MockTransport.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from quimera import profiles
from quimera.profiles.base import OpenAIConnection
from quimera.profiles.claudecloud import ClaudeCloudProfile
from quimera.runtime.claude_auth import ClaudeAuthError, ClaudeCloudAuth
from quimera.runtime.drivers.claudecloud import (
    CLAUDE_CODE_IDENTITY,
    ClaudeCloudDriver,
    _chat_tools_to_anthropic_tools,
)
from quimera.runtime.drivers.openai_compat import FatalAPIError, TransientAPIError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_credentials(tmp_path, access_token="access-1", refresh_token="refresh-1",
                       expires_at=None):
    data = {
        "claudeAiOauth": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": expires_at if expires_at is not None else int(time.time() * 1000) + 3600_000,
            "scopes": ["user:inference"],
        }
    }
    (tmp_path / ".credentials.json").write_text(json.dumps(data), encoding="utf-8")
    return data


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
        return "token-abc"


def _make_driver(handler, auth=None, extra_body=None) -> ClaudeCloudDriver:
    transport = httpx.MockTransport(handler)
    return ClaudeCloudDriver(
        model="claude-sonnet-4-5-20250929",
        extra_body=extra_body,
        auth=auth or _FakeAuth(),
        http_client=httpx.Client(transport=transport),
    )


# ---------------------------------------------------------------------------
# ClaudeCloudAuth
# ---------------------------------------------------------------------------

def test_auth_returns_valid_token_without_refresh(tmp_path):
    _write_credentials(tmp_path, access_token="tok-valid")
    auth = ClaudeCloudAuth(claude_home=tmp_path)

    assert auth.credentials() == "tok-valid"


def test_auth_refreshes_expired_token_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_OAUTH_CLIENT_ID", "client-from-environment")
    fresh_expires_in = 3600
    _write_credentials(
        tmp_path, access_token="tok-old", refresh_token="refresh-old",
        expires_at=int(time.time() * 1000) - 10_000,
    )
    requests_seen = []

    def handler(request):
        requests_seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "access_token": "tok-fresh",
            "refresh_token": "refresh-new",
            "expires_in": fresh_expires_in,
        })

    auth = ClaudeCloudAuth(
        claude_home=tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert auth.credentials() == "tok-fresh"
    assert requests_seen[0]["grant_type"] == "refresh_token"
    assert requests_seen[0]["refresh_token"] == "refresh-old"
    assert requests_seen[0]["client_id"] == "client-from-environment"
    persisted = json.loads((tmp_path / ".credentials.json").read_text(encoding="utf-8"))
    assert persisted["claudeAiOauth"]["accessToken"] == "tok-fresh"
    assert persisted["claudeAiOauth"]["refreshToken"] == "refresh-new"
    assert persisted["last_refresh"].endswith("Z")


def test_auth_refresh_uses_injected_runtime_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_OAUTH_CLIENT_ID", raising=False)
    _write_credentials(
        tmp_path, access_token="tok-old", refresh_token="refresh-old",
        expires_at=int(time.time() * 1000) - 10_000,
    )
    requests_seen = []

    class _Secrets:
        @staticmethod
        def get(key, default=None):
            if key == "CLAUDE_OAUTH_CLIENT_ID":
                return "client-from-runtime-secrets"
            return default

    def handler(request):
        requests_seen.append(json.loads(request.content))
        return httpx.Response(200, json={"access_token": "tok-fresh"})

    auth = ClaudeCloudAuth(
        claude_home=tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        runtime_secrets=_Secrets(),
    )

    assert auth.credentials() == "tok-fresh"
    assert requests_seen[0]["client_id"] == "client-from-runtime-secrets"


def test_auth_refresh_requires_client_id_environment_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_OAUTH_CLIENT_ID", raising=False)
    _write_credentials(
        tmp_path, refresh_token="refresh-old",
        expires_at=int(time.time() * 1000) - 10_000,
    )

    auth = ClaudeCloudAuth(claude_home=tmp_path)

    with pytest.raises(ClaudeAuthError, match="CLAUDE_OAUTH_CLIENT_ID"):
        auth.credentials()


def test_auth_missing_file_raises(tmp_path):
    auth = ClaudeCloudAuth(claude_home=tmp_path)
    with pytest.raises(ClaudeAuthError, match="claude login"):
        auth.credentials()


def test_auth_missing_refresh_token_raises(tmp_path):
    _write_credentials(tmp_path, refresh_token="",
                       expires_at=int(time.time() * 1000) - 10_000)
    auth = ClaudeCloudAuth(claude_home=tmp_path)
    with pytest.raises(ClaudeAuthError, match="refreshToken"):
        auth.credentials()


def test_auth_rejects_non_oauth_file(tmp_path):
    (tmp_path / ".credentials.json").write_text(json.dumps({"foo": 1}), encoding="utf-8")
    auth = ClaudeCloudAuth(claude_home=tmp_path)
    with pytest.raises(ClaudeAuthError, match="claudeAiOauth"):
        auth.credentials()


# ---------------------------------------------------------------------------
# Conversão chat -> Anthropic
# ---------------------------------------------------------------------------

def test_build_anthropic_payload_maps_roles_and_tools():
    driver = _make_driver(lambda request: httpx.Response(500))
    messages = [
        {"role": "system", "content": "regra 1"},
        {"role": "user", "content": "faz X"},
        {
            "role": "assistant",
            "content": "vou ler",
            "tool_calls": [{
                "id": "toolu-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "toolu-1", "content": '{"ok": true}'},
    ]
    tools = [{"type": "function", "function": {
        "name": "read_file", "description": "lê", "parameters": {"type": "object"},
    }}]

    body = driver._build_anthropic_payload(messages, tools)

    assert body["model"] == "claude-sonnet-4-5-20250929"
    # Token OAuth de subscription exige a identidade do Claude Code no
    # primeiro bloco de system; o system do Quimera vem depois.
    assert body["system"][0] == {"type": "text", "text": CLAUDE_CODE_IDENTITY}
    assert body["system"][1]["text"] == "regra 1"
    assert body["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert body["stream"] is True
    assert body["max_tokens"] == 32000
    # Família com budget_tokens: sem default de thinking adaptive.
    assert "thinking" not in body
    assert body["tools"] == [{
        "name": "read_file", "description": "lê", "input_schema": {"type": "object"},
    }]
    assert body["messages"][0] == {"role": "user", "content": "faz X"}
    assistant = body["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {"type": "text", "text": "vou ler"}
    assert assistant["content"][1]["type"] == "tool_use"
    assert assistant["content"][1]["id"] == "toolu-1"
    assert assistant["content"][1]["input"] == {"path": "a.txt"}
    tool_result = body["messages"][2]
    assert tool_result["role"] == "user"
    assert tool_result["content"][0]["type"] == "tool_result"
    assert tool_result["content"][0]["tool_use_id"] == "toolu-1"
    # Breakpoint móvel de cache no último bloco da conversa.
    assert tool_result["content"][-1]["cache_control"] == {"type": "ephemeral"}
    driver.close()


def test_build_anthropic_payload_defaults_adaptive_thinking():
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    driver = ClaudeCloudDriver(
        model="claude-sonnet-5", auth=_FakeAuth(),
        http_client=httpx.Client(transport=transport),
    )
    body = driver._build_anthropic_payload([{"role": "user", "content": "oi"}], [])
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    # Última (e única) mensagem vira bloco com o breakpoint móvel de cache.
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    driver.close()

    override = ClaudeCloudDriver(
        model="claude-sonnet-5", auth=_FakeAuth(),
        http_client=httpx.Client(transport=transport),
        extra_body={"thinking": {"type": "disabled"}},
    )
    body = override._build_anthropic_payload([{"role": "user", "content": "oi"}], [])
    assert body["thinking"] == {"type": "disabled"}
    override.close()


def test_chat_tools_to_anthropic_tools_ignores_malformed():
    assert _chat_tools_to_anthropic_tools([{"type": "function"}, "junk"]) == []


# ---------------------------------------------------------------------------
# Streaming SSE
# ---------------------------------------------------------------------------

def test_messages_turn_streams_text():
    events = [
        {"type": "message_start", "message": {"id": "msg-1"}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Olá"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": ", mundo"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]

    def handler(request):
        assert request.headers["authorization"] == "Bearer token-abc"
        assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
        assert request.url.path.endswith("/v1/messages")
        return httpx.Response(200, text=_sse(events),
                              headers={"content-type": "text/event-stream"})

    driver = _make_driver(handler)
    chunks: list[str] = []

    text, tool_calls = driver._messages_turn(
        [{"role": "user", "content": "oi"}], [], on_text_chunk=chunks.append
    )

    assert text == "Olá, mundo"
    assert tool_calls == []
    assert chunks == ["Olá", ", mundo"]
    driver.close()


def test_chat_forwards_tools_keyword_to_messages_turn():
    """Regressão: a base chama _chat_streaming(messages, tools=...) por keyword."""
    events = [
        {"type": "message_start", "message": {"id": "msg-1"}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]

    def handler(request):
        body = json.loads(request.content)
        assert [tool["name"] for tool in body["tools"]] == ["ping"]
        return httpx.Response(200, text=_sse(events),
                              headers={"content-type": "text/event-stream"})

    driver = _make_driver(handler)
    tools = [{"type": "function", "function": {"name": "ping", "parameters": {}}}]

    text, tool_calls = driver._chat([{"role": "user", "content": "oi"}], tools)

    assert text == "ok"
    assert tool_calls == []
    driver.close()


def test_messages_turn_collects_tool_use():
    events = [
        {"type": "message_start", "message": {"id": "msg-1"}},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu-9", "name": "read_file"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"path": '}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '"x"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    driver = _make_driver(lambda request: httpx.Response(200, text=_sse(events)))

    text, tool_calls = driver._messages_turn([{"role": "user", "content": "oi"}], [])

    assert text == ""
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "toolu-9"
    assert tool_calls[0]["arguments"] == {"path": "x"}
    assert tool_calls[0]["argument_error"] is None
    driver.close()


def test_messages_turn_retries_once_on_401_with_forced_refresh():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, text=_sse([
            {"type": "message_start", "message": {"id": "m"}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ]))

    auth = _FakeAuth()
    driver = _make_driver(handler, auth=auth)

    text, _ = driver._messages_turn([{"role": "user", "content": "oi"}], [])

    assert text == "ok"
    assert auth.calls == [False, True]
    driver.close()


def test_messages_turn_maps_429_as_rate_limited():
    driver = _make_driver(lambda request: httpx.Response(
        429, headers={"retry-after": "5"}, text="slow down"
    ))
    with pytest.raises(TransientAPIError) as excinfo:
        driver._messages_turn([{"role": "user", "content": "oi"}], [])
    assert excinfo.value.rate_limited is True
    assert excinfo.value.retry_after == 5.0
    driver.close()


def test_messages_turn_maps_401_after_refresh_as_fatal():
    driver = _make_driver(lambda request: httpx.Response(401, text="nope"))
    with pytest.raises(FatalAPIError, match="401"):
        driver._messages_turn([{"role": "user", "content": "oi"}], [])
    driver.close()


def test_messages_turn_resolves_short_model_alias():
    posted: dict = {}

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [
                {"id": "claude-fable-5-1"},
                {"id": "claude-sonnet-5"},
                {"id": "claude-sonnet-4-5-20250929"},
            ]})
        posted["body"] = json.loads(request.content)
        return httpx.Response(200, text=_sse([
            {"type": "message_start", "message": {"id": "m"}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ]))

    driver = ClaudeCloudDriver(
        model="sonnet", auth=_FakeAuth(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    text, _ = driver._messages_turn([{"role": "user", "content": "oi"}], [])

    assert text == "ok"
    # Primeiro id da família na ordem da API (mais novo primeiro).
    assert posted["body"]["model"] == "claude-sonnet-5"
    assert posted["body"]["system"][0]["text"] == CLAUDE_CODE_IDENTITY
    driver.close()


def test_messages_turn_unknown_alias_is_fatal():
    def handler(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-5"}]})

    driver = ClaudeCloudDriver(
        model="galactica", auth=_FakeAuth(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(FatalAPIError, match="galactica"):
        driver._messages_turn([{"role": "user", "content": "oi"}], [])
    driver.close()


def test_thinking_blocks_survive_tool_hops():
    """Thinking (com signature) volta na posição original ao reenviar o turno."""
    events = [
        {"type": "message_start", "message": {"id": "m"}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "preciso ler o arquivo"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "sig-abc"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "vou ler"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "toolu-7", "name": "read_file"}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "input_json_delta", "partial_json": '{"path": "x"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    driver = _make_driver(lambda request: httpx.Response(200, text=_sse(events)))
    chunks: list[str] = []

    text, tool_calls = driver._messages_turn(
        [{"role": "user", "content": "oi"}], [], on_text_chunk=chunks.append
    )

    assert text == "vou ler"
    assert tool_calls[0]["id"] == "toolu-7"
    # A signature nunca vaza para o feed de raciocínio.
    assert "sig-abc" not in "".join(chunks)

    history = [
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": "vou ler", "tool_calls": [{
            "id": "toolu-7", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "x"}'},
        }]},
        {"role": "tool", "tool_call_id": "toolu-7", "content": '{"ok": true}'},
    ]
    body = driver._build_anthropic_payload(history, [])
    assistant = body["messages"][1]
    assert assistant["content"][0] == {
        "type": "thinking", "thinking": "preciso ler o arquivo", "signature": "sig-abc",
    }
    assert assistant["content"][1] == {"type": "text", "text": "vou ler"}
    assert assistant["content"][2]["type"] == "tool_use"
    assert assistant["content"][2]["id"] == "toolu-7"
    assert assistant["content"][2]["input"] == {"path": "x"}

    # Mutação no payload (ex.: cache_control) não pode contaminar a side-table.
    assistant["content"][0]["cache_control"] = {"type": "ephemeral"}
    body2 = driver._build_anthropic_payload(history, [])
    assert "cache_control" not in body2["messages"][1]["content"][0]
    driver.close()


def test_thinking_replay_falls_back_when_history_edited():
    """Se os ids de tool_use divergirem do retido, reconstrói sem thinking."""
    driver = _make_driver(lambda request: httpx.Response(500))
    driver._turn_blocks["toolu-1"] = (
        {"type": "thinking", "thinking": "t", "signature": "s"},
        {"type": "tool_use", "id": "toolu-1", "name": "a", "input": {}},
        {"type": "tool_use", "id": "toolu-2", "name": "b", "input": {}},
    )
    history = [
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": "vou ler", "tool_calls": [{
            "id": "toolu-1", "type": "function",
            "function": {"name": "a", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "toolu-1", "content": '{"ok": true}'},
    ]

    body = driver._build_anthropic_payload(history, [])

    kinds = [block["type"] for block in body["messages"][1]["content"]]
    assert "thinking" not in kinds
    assert kinds == ["text", "tool_use"]
    driver.close()


def test_max_tokens_stop_reason_appends_truncation_notice():
    events = [
        {"type": "message_start", "message": {"id": "m"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "resposta parcial"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
        {"type": "message_stop"},
    ]
    driver = _make_driver(lambda request: httpx.Response(200, text=_sse(events)))
    chunks: list[str] = []

    text, tool_calls = driver._messages_turn(
        [{"role": "user", "content": "oi"}], [], on_text_chunk=chunks.append
    )

    assert text.startswith("resposta parcial")
    assert "truncada" in text
    assert tool_calls == []
    # O aviso também vai para o feed streaming.
    assert "truncada" in "".join(chunks)
    driver.close()


def test_refusal_stop_reason_raises_fatal():
    events = [
        {"type": "message_start", "message": {"id": "m"}},
        {"type": "message_delta", "delta": {
            "stop_reason": "refusal",
            "stop_details": {"type": "refusal", "category": "cyber",
                             "explanation": "conteúdo não permitido"},
        }},
        {"type": "message_stop"},
    ]
    driver = _make_driver(lambda request: httpx.Response(200, text=_sse(events)))

    with pytest.raises(FatalAPIError, match="refusal") as excinfo:
        driver._messages_turn([{"role": "user", "content": "oi"}], [])

    assert "recusou" in (excinfo.value.user_message or "")
    driver.close()


def test_claudecloud_profile_registered_with_api_driver():
    profile = profiles.get("claudecloud")

    assert isinstance(profile, ClaudeCloudProfile)
    # Regressão: o pacote quimera.profiles precisa importar o módulo para o
    # registro acontecer no app real (não só quando o teste importa direto).
    assert getattr(profiles, "_claudecloud", None) is not None
    assert profile.driver == "claudecloud"
    # Sem modelo padrão: perfil é só template, não executa sozinho.
    # effective_connection() retorna None até um /connect definir o modelo.
    assert profile.model is None
    assert profile.effective_connection() is None


def test_claudecloud_profile_configure_with_model_returns_api_connection():
    profile = profiles.get("claudecloud")
    assert profile is not None

    connection = profile.configure_with_model("claude-opus-4-1-20250805")

    assert isinstance(connection, OpenAIConnection)
    assert connection.model == "claude-opus-4-1-20250805"
    assert connection.provider == "claudecloud"


def test_claudecloud_connection_profile_without_override_has_printable_label():
    # Regressão: `--connect <nome> --profile claudecloud` cria um perfil dinâmico
    # herdando ClaudeCloudProfile; sem conexão persistida, effective_connection()
    # é None e o CLI imprime o label via format_connection_label — não pode explodir.
    from quimera.profiles.base import (
        _resolve_registry,
        format_connection_label,
        register_connection_profile,
    )

    dynamic = register_connection_profile(
        "claudecloud-sonnet-test", metadata={"profile": "claudecloud"}
    )
    try:
        assert isinstance(dynamic, ClaudeCloudProfile)
        assert dynamic.effective_connection() is None
        label = format_connection_label(dynamic.effective_connection())
        assert "sem conexão" in label
    finally:
        _resolve_registry()._profiles.pop("claudecloud-sonnet-test", None)

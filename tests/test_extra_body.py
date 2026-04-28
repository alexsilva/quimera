"""
Testes para extra_body em OpenAIConnection, CLI, e driver.

Cobre:
- Serialização/desserialização de extra_body em connection_to_dict / _connection_from_dict
- format_connection_label com extra_body
- _parse_extra_body_arg
- OpenAICompatDriver com extra_body (repassa para API)
- _build_connection_from_args com --extra-body
"""
from __future__ import annotations

import json
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from quimera.plugins.base import (
    OpenAIConnection,
    _connection_from_dict,
    connection_to_dict,
    format_connection_label,
)
from quimera.cli import _parse_extra_body_arg, _build_connection_from_args
from quimera.runtime.drivers.openai_compat import OpenAICompatDriver


# ---------------------------------------------------------------------------
# Testes de serialização de extra_body
# ---------------------------------------------------------------------------

def test_connection_to_dict_includes_extra_body():
    conn = OpenAIConnection(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        provider="openai_compat",
        extra_body={"thinking": {"type": "disabled"}},
    )
    data = connection_to_dict(conn)
    assert data["extra_body"] == {"thinking": {"type": "disabled"}}
    assert data["type"] == "openai"


def test_connection_to_dict_extra_body_none_omitted():
    conn = OpenAIConnection(model="gpt-4o")
    data = connection_to_dict(conn)
    assert "extra_body" in data
    assert data["extra_body"] is None


def test_connection_from_dict_loads_extra_body():
    data = {
        "type": "openai",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "provider": "openai_compat",
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    conn = _connection_from_dict(data)
    assert isinstance(conn, OpenAIConnection)
    assert conn.extra_body == {"thinking": {"type": "disabled"}}


def test_connection_from_dict_loads_extra_body_none_when_missing():
    data = {
        "type": "openai",
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
    }
    conn = _connection_from_dict(data)
    assert isinstance(conn, OpenAIConnection)
    assert conn.extra_body is None


def test_roundtrip_extra_body():
    conn = OpenAIConnection(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        extra_body={"thinking": {"type": "disabled"}},
    )
    data = connection_to_dict(conn)
    conn2 = _connection_from_dict(data)
    assert conn2.extra_body == {"thinking": {"type": "disabled"}}


def test_roundtrip_extra_body_none():
    conn = OpenAIConnection(model="gpt-4o", extra_body=None)
    data = connection_to_dict(conn)
    conn2 = _connection_from_dict(data)
    assert conn2.extra_body is None


# ---------------------------------------------------------------------------
# Testes de format_connection_label
# ---------------------------------------------------------------------------

def test_format_connection_label_includes_extra_body():
    conn = OpenAIConnection(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        provider="openai_compat",
        extra_body={"thinking": {"type": "disabled"}},
    )
    label = format_connection_label(conn)
    assert "deepseek-chat" in label
    assert "extra_body=" in label
    assert '"thinking"' in label
    assert '"disabled"' in label


def test_format_connection_label_no_extra_body():
    conn = OpenAIConnection(model="gpt-4o")
    label = format_connection_label(conn)
    assert "extra_body" not in label


# ---------------------------------------------------------------------------
# Testes de _parse_extra_body_arg
# ---------------------------------------------------------------------------

def test_parse_extra_body_arg_valid_json():
    result = _parse_extra_body_arg('{"thinking": {"type": "disabled"}}')
    assert result == {"thinking": {"type": "disabled"}}


def test_parse_extra_body_arg_none():
    assert _parse_extra_body_arg(None) is None


def test_parse_extra_body_arg_empty_string():
    assert _parse_extra_body_arg("  ") is None


def test_parse_extra_body_arg_invalid_json_exits():
    with pytest.raises(SystemExit, match="extra-body"):
        _parse_extra_body_arg("not json")


# ---------------------------------------------------------------------------
# Testes de OpenAICompatDriver com extra_body
# ---------------------------------------------------------------------------

def test_driver_init_with_extra_body():
    with patch("quimera.runtime.drivers.openai_compat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        driver = OpenAICompatDriver(
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            extra_body={"thinking": {"type": "disabled"}},
        )
    # extra_body é armazenado como self.extra_body (None ou dict)
    assert driver.extra_body == {"thinking": {"type": "disabled"}}


def test_driver_init_without_extra_body():
    with patch("quimera.runtime.drivers.openai_compat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        driver = OpenAICompatDriver(
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
        )
    assert driver.extra_body is None


def test_driver_init_extra_body_none_explicit():
    with patch("quimera.runtime.drivers.openai_compat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        driver = OpenAICompatDriver(
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            extra_body=None,
        )
    assert driver.extra_body is None


def _make_driver_extra(model="gpt-4o", base_url="https://api.openai.com/v1", extra_body=None):
    """Cria driver com cliente mockado, suporte a extra_body."""
    with patch("quimera.runtime.drivers.openai_compat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        driver = OpenAICompatDriver(model=model, base_url=base_url, extra_body=extra_body)
    driver._client = mock_client
    return driver, mock_client


def test_chat_with_tools_passes_extra_body():
    """Verifica que extra_body é passado na chamada com tools."""
    extra = {"thinking": {"type": "disabled"}}
    driver, mock_client = _make_driver_extra(extra_body=extra)

    # Resposta sem tool calls
    msg = SimpleNamespace(content="ok", tool_calls=None)
    choice = SimpleNamespace(message=msg)
    mock_client.chat.completions.create.return_value = SimpleNamespace(choices=[choice])

    driver._chat(
        [{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
    )

    call_kwargs = mock_client.chat.completions.create.call_args[1]
    assert "extra_body" in call_kwargs
    assert call_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_chat_with_tools_no_extra_body_when_none():
    """Sem extra_body, a chave extra_body não deve aparecer na chamada."""
    driver, mock_client = _make_driver_extra(extra_body=None)

    msg = SimpleNamespace(content="ok", tool_calls=None)
    choice = SimpleNamespace(message=msg)
    mock_client.chat.completions.create.return_value = SimpleNamespace(choices=[choice])

    driver._chat(
        [{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
    )

    call_kwargs = mock_client.chat.completions.create.call_args[1]
    assert "extra_body" not in call_kwargs


# ---------------------------------------------------------------------------
# Testes de _build_connection_from_args com --extra-body
# ---------------------------------------------------------------------------

def _fake_plugin(name="test-agent", driver="openai_compat", model="gpt-4o",
                 base_url="https://api.openai.com/v1",
                 api_key_env="OPENAI_API_KEY", supports_tools=True):
    """Cria um plugin falso para testes de _build_connection_from_args."""
    from quimera.plugins.base import AgentPlugin
    return AgentPlugin(
        name=name,
        prefix=f"/{name}",
        style=("cyan", name.title()),
        driver=driver,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        supports_tools=supports_tools,
    )


def test_build_connection_with_model_and_extra_body():
    """--model + --extra-body juntos montam OpenAIConnection com extra_body."""
    plugin = _fake_plugin()
    args = Namespace(
        base=None,
        driver="openai",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        extra_body='{"thinking": {"type": "disabled"}}',
        cmd=None,
    )
    conn = _build_connection_from_args(plugin, args)
    assert isinstance(conn, OpenAIConnection)
    assert conn.model == "deepseek-chat"
    assert conn.extra_body == {"thinking": {"type": "disabled"}}


def test_build_connection_with_model_no_extra_body():
    """--model sem --extra-body: extra_body deve ser None."""
    plugin = _fake_plugin()
    args = Namespace(
        base=None,
        driver="openai",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        extra_body=None,
        cmd=None,
    )
    conn = _build_connection_from_args(plugin, args)
    assert isinstance(conn, OpenAIConnection)
    assert conn.extra_body is None


def test_build_connection_base_with_model_ignores_extra_body():
    """--base + --model: caminho configure_with_model ignora extra_body (é CliConnection)."""
    # Isso testa que o ramo base+model não quebra com extra_body presente
    from quimera.plugins.base import AgentPlugin
    base_plugin = AgentPlugin(
        name="base-agent",
        prefix="/base-agent",
        style=("green", "Base"),
        driver="cli",
        cmd=["my-cli", "--model=PLACEHOLDER"],
    )

    # Precisamos de um registry para o base_plugin
    import quimera.plugins.base as base_mod
    old_registry = dict(base_mod._registry)
    try:
        base_mod._registry["base-agent"] = base_plugin
        args = Namespace(
            base="base-agent",
            driver=None,
            model="gpt-4o",
            base_url=None,
            api_key_env=None,
            extra_body='{"thinking": {"type": "disabled"}}',
            cmd=None,
        )
        conn = _build_connection_from_args(base_plugin, args)
        # Deve retornar CliConnection (base+model sempre é CLI)
        from quimera.plugins.base import CliConnection
        assert isinstance(conn, CliConnection)
    finally:
        base_mod._registry.clear()
        base_mod._registry.update(old_registry)


def test_build_connection_without_model_falls_to_interactive():
    """--driver=openai sem --model deve cair em modo interativo (raise SystemExit)."""
    plugin = _fake_plugin()
    args = Namespace(
        base=None,
        driver="openai",
        model=None,  # sem modelo!
        base_url=None,
        api_key_env=None,
        extra_body=None,
        cmd=None,
    )
    from unittest.mock import patch as mock_patch
    with mock_patch("quimera.cli._configure_connection_interactively") as mock_interactive:
        mock_interactive.return_value = OpenAIConnection(model="fallback")
        _build_connection_from_args(plugin, args)
        mock_interactive.assert_called_once()

# ---------------------------------------------------------------------------
# Testes de persistência de extra_body (set_connection_override / load)
# ---------------------------------------------------------------------------


class TestExtraBodyPersistence:
    """Testa o ciclo completo: salvar no JSON e recarregar."""

    def test_set_connection_override_persists_extra_body(self, tmp_path, monkeypatch):
        """set_connection_override deve salvar extra_body no connections.json."""
        from quimera.plugins import base as base_mod
        from quimera.plugins.base import (
            OpenAIConnection,
            set_connection_override,
            AgentPlugin,
        )

        # Redireciona o arquivo de conexões para tmp_path
        conn_file = tmp_path / "connections.json"
        monkeypatch.setattr(base_mod, "_get_connections_file", lambda: conn_file)
        base_mod._registry.clear()
        try:
            plugin = AgentPlugin(
                name="deepseek",
                prefix="/deepseek",
                style=("blue", "DeepSeek"),
                driver="openai_compat",
            )
            base_mod._registry["deepseek"] = plugin

            conn = OpenAIConnection(
                model="deepseek-chat",
                base_url="https://api.deepseek.com/v1",
                api_key_env="DEEPSEEK_API_KEY",
                provider="openai_compat",
                extra_body={"thinking": {"type": "disabled"}},
            )
            set_connection_override("deepseek", conn, persist=True)

            # Verifica que foi salvo no arquivo
            assert conn_file.exists()
            saved = json.loads(conn_file.read_text(encoding="utf-8"))
            assert "deepseek" in saved
            assert saved["deepseek"]["extra_body"] == {"thinking": {"type": "disabled"}}
            assert saved["deepseek"]["model"] == "deepseek-chat"
        finally:
            base_mod._registry.clear()

    def test_set_connection_override_extra_body_none(self, tmp_path, monkeypatch):
        """extra_body=None deve ser persistido como None."""
        from quimera.plugins import base as base_mod
        from quimera.plugins.base import (
            OpenAIConnection,
            set_connection_override,
            AgentPlugin,
        )

        conn_file = tmp_path / "connections.json"
        monkeypatch.setattr(base_mod, "_get_connections_file", lambda: conn_file)
        base_mod._registry.clear()
        try:
            plugin = AgentPlugin(
                name="gpt",
                prefix="/gpt",
                style=("green", "GPT"),
                driver="openai_compat",
            )
            base_mod._registry["gpt"] = plugin

            conn = OpenAIConnection(model="gpt-4o", extra_body=None)
            set_connection_override("gpt", conn, persist=True)

            saved = json.loads(conn_file.read_text(encoding="utf-8"))
            assert saved["gpt"]["extra_body"] is None
        finally:
            base_mod._registry.clear()

    def test_load_connections_roundtrip_extra_body(self, tmp_path, monkeypatch):
        """Salva e recarrega conexão com extra_body via JSON."""
        from quimera.plugins import base as base_mod
        from quimera.plugins.base import (
            _connection_from_dict,
            save_connections,
            load_connections,
        )

        conn_file = tmp_path / "connections.json"
        monkeypatch.setattr(base_mod, "_get_connections_file", lambda: conn_file)

        payload = {
            "deepseek": {
                "type": "openai",
                "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com/v1",
                "api_key_env": "DEEPSEEK_API_KEY",
                "provider": "openai_compat",
                "extra_body": {"thinking": {"type": "disabled"}},
            }
        }
        save_connections(payload)
        loaded = load_connections()
        assert loaded == payload
        conn = _connection_from_dict(loaded["deepseek"])
        assert conn.extra_body == {"thinking": {"type": "disabled"}}


# ---------------------------------------------------------------------------
# Testes de effective_connection() propagando extra_body
# ---------------------------------------------------------------------------


class TestEffectiveConnectionExtraBody:
    """Garante que effective_connection() propaga extra_body do override."""

    def test_effective_connection_returns_extra_body_from_override(self):
        """Quando há override com extra_body, effective_connection deve retorná-lo."""
        from quimera.plugins.base import AgentPlugin, OpenAIConnection

        plugin = AgentPlugin(
            name="deepseek",
            prefix="/deepseek",
            style=("blue", "DeepSeek"),
            driver="openai_compat",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
        )

        # Sem override, não deve ter extra_body
        conn = plugin.effective_connection()
        assert isinstance(conn, OpenAIConnection)
        assert conn.extra_body is None

        # Com override
        override = OpenAIConnection(
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            extra_body={"thinking": {"type": "disabled"}},
        )
        object.__setattr__(plugin, "_connection_override", override)
        conn = plugin.effective_connection()
        assert conn.extra_body == {"thinking": {"type": "disabled"}}


# ---------------------------------------------------------------------------
# Testes extras de serialização
# ---------------------------------------------------------------------------


def test_cli_connection_to_dict_has_no_extra_body():
    """CliConnection não deve ter campo extra_body no dict."""
    from quimera.plugins.base import CliConnection, connection_to_dict

    conn = CliConnection(cmd=["my-cli"], prompt_as_arg=True)
    data = connection_to_dict(conn)
    assert "extra_body" not in data
    assert data["type"] == "cli"

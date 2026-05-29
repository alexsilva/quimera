"""Tests for ConnectionConfigurator — preservação de configuração existente."""
import json

import pytest

from quimera.connection_configurator import ConnectionConfigurator
from quimera.plugins.base import AgentPlugin, CliConnection, OpenAIConnection


def _make_plugin(name="agent", cmd=None, model="gpt-4o", base_url="https://api.openai.com/v1",
                 api_key_env="OPENAI_API_KEY", driver="openai_compat"):
    return AgentPlugin(
        name=name,
        prefix=f"/{name}",
        style=("cyan", name.upper()),
        cmd=cmd or ["ollama", "run", "llama3"],
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        driver=driver,
    )


def _make_configurator(inputs: list, bools: list | None = None, warn=None):
    """Cria ConnectionConfigurator com entradas pré-definidas.

    Cada chamada a prompt_text consome o próximo item de `inputs`;
    string vazia significa "pressionar Enter" (aceitar default).
    """
    inputs_iter = iter(inputs)
    bools_iter = iter(bools or [])

    def prompt_text(label, default=None):
        try:
            val = next(inputs_iter)
        except StopIteration:
            val = ""
        return val if val else (default or "")

    def prompt_bool(label, default=False):
        try:
            return next(bools_iter)
        except StopIteration:
            return default

    return ConnectionConfigurator(
        prompt_text=prompt_text,
        prompt_bool=prompt_bool,
        warn=warn or (lambda msg: None),
    )


# ---------------------------------------------------------------------------
# OpenAI — sem alterações preserva a conexão original
# ---------------------------------------------------------------------------

class TestOpenAIUnchanged:
    def _make_existing_conn(self, provider="openai_compat"):
        return OpenAIConnection(
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            provider=provider,
            supports_native_tools=True,
            extra_body=None,
            max_connections=4,
        )

    def _run_configure_unchanged(self, provider="openai_compat"):
        """Simula usuário pressionando Enter em todos os campos."""
        conn = self._make_existing_conn(provider=provider)
        plugin = _make_plugin()
        object.__setattr__(plugin, "_connection_override", conn)

        # inputs: driver, provider, model, base_url, api_key_env, extra_body, max_connections
        cfg = _make_configurator(
            inputs=["openai", "", "", "", "", "", ""],
            bools=[True],  # supports_native_tools mantém True
        )
        result = cfg.configure(plugin)
        return conn, result

    def test_unchanged_returns_same_object_openai_compat(self):
        conn, result = self._run_configure_unchanged(provider="openai_compat")
        assert result is conn, "Deve retornar o mesmo objeto quando nada mudou"

    def test_unchanged_returns_same_object_provider_openai(self):
        """Regressão: provider='openai' (default do dataclass) não deve ser normalizado."""
        conn, result = self._run_configure_unchanged(provider="openai")
        assert result is conn, "provider='openai' deve ser preservado sem normalização"

    def test_changed_model_returns_new_conn(self):
        conn = self._make_existing_conn()
        plugin = _make_plugin()
        object.__setattr__(plugin, "_connection_override", conn)

        cfg = _make_configurator(
            inputs=["openai", "", "gpt-4o-mini", "", "", "", ""],
            bools=[True],
        )
        result = cfg.configure(plugin)
        assert result is not conn
        assert result.model == "gpt-4o-mini"

    def test_extra_body_preserved_when_unchanged(self):
        body = {"thinking": {"type": "enabled"}}
        conn = OpenAIConnection(
            model="claude-3-5",
            base_url="https://api.anthropic.com/v1",
            api_key_env="ANTHROPIC_API_KEY",
            provider="anthropic",
            extra_body=body,
            max_connections=2,
        )
        plugin = _make_plugin(model="claude-3-5", base_url="https://api.anthropic.com/v1",
                              api_key_env="ANTHROPIC_API_KEY", driver="anthropic")
        object.__setattr__(plugin, "_connection_override", conn)

        extra_str = json.dumps(body, ensure_ascii=False)
        cfg = _make_configurator(
            inputs=["openai", "", "", "", "", extra_str, "2"],
            bools=[True],  # supports_native_tools mantém True (valor do conn)
        )
        result = cfg.configure(plugin)
        assert result is conn


# ---------------------------------------------------------------------------
# CLI — sem alterações preserva a conexão original
# ---------------------------------------------------------------------------

class TestCLIUnchanged:
    def _make_existing_conn(self):
        return CliConnection(
            cmd=["ollama", "run", "llama3"],
            prompt_as_arg=False,
            output_format=None,
        )

    def test_unchanged_returns_same_object(self):
        conn = self._make_existing_conn()
        plugin = _make_plugin(driver="cli")
        object.__setattr__(plugin, "_connection_override", conn)

        # inputs: driver, output_format (vazio=None mantido), cmd (aceita default)
        cfg = _make_configurator(
            inputs=["cli", "", "ollama run llama3"],
            bools=[False],
        )
        result = cfg.configure(plugin)
        assert result is conn

    def test_changed_cmd_returns_new_conn(self):
        conn = self._make_existing_conn()
        plugin = _make_plugin(driver="cli")
        object.__setattr__(plugin, "_connection_override", conn)

        cfg = _make_configurator(
            inputs=["cli", "", "ollama run mistral"],
            bools=[False],
        )
        result = cfg.configure(plugin)
        assert result is not conn
        assert result.cmd == ["ollama", "run", "mistral"]

    def test_empty_cmd_returns_existing_cli(self):
        """Enter vazio no campo cmd retorna a conexão CLI existente."""
        conn = self._make_existing_conn()
        plugin = _make_plugin(driver="cli")
        object.__setattr__(plugin, "_connection_override", conn)

        # cmd vazio → _configure_cli retorna cli_defaults
        cfg = _make_configurator(
            inputs=["cli", "", ""],
            bools=[],
        )
        result = cfg.configure(plugin)
        assert result is conn

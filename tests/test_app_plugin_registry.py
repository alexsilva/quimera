"""Tests for PluginRegistry injection in QuimeraApp."""
from pathlib import Path

import pytest

from quimera.app.core import QuimeraApp
from quimera.plugins.base import AgentPlugin, PluginRegistry


@pytest.fixture
def sample_plugin():
    return AgentPlugin(
        name="test-agent",
        prefix="/test",
        style=("blue", "Test Agent"),
    )


@pytest.fixture
def empty_registry():
    return PluginRegistry()


@pytest.fixture
def populated_registry(sample_plugin):
    registry = PluginRegistry()
    registry.register(sample_plugin)
    return registry


class TestPluginRegistryInjection:
    """Testes para injeção de PluginRegistry no QuimeraApp."""

    def test_default_no_registry_uses_plugins_module(self, tmp_path):
        """Sem registry injetado, usa plugins.get / plugins.all_plugins (fallback)."""
        app = QuimeraApp(tmp_path)
        # O módulo plugins já tem agentes registrados (claude, codex, etc.)
        available = app.get_available_plugins()
        assert len(available) > 0
        # Verifica que busca um agente conhecido do módulo global
        plugin = app.get_agent_plugin("codex")
        assert plugin is not None
        assert plugin.name == "codex"

    def test_injected_empty_registry_returns_no_plugins(self, tmp_path, empty_registry):
        """Verifica que registry vazio injetado retorna lista vazia e None para consultas."""
        app = QuimeraApp(tmp_path, plugin_registry=empty_registry)
        available = app.get_available_plugins()
        assert available == []
        plugin = app.get_agent_plugin("codex")
        assert plugin is None

    def test_injected_registry_with_plugin_returns_it(self, tmp_path, populated_registry, sample_plugin):
        """Verifica que registry populado retorna os plugins corretos."""
        app = QuimeraApp(tmp_path, plugin_registry=populated_registry)
        available = app.get_available_plugins()
        assert available == [sample_plugin]
        plugin = app.get_agent_plugin("test-agent")
        assert plugin is sample_plugin

    def test_injected_registry_unknown_agent_returns_none(self, tmp_path, populated_registry):
        """Verifica que agente inexistente no registry retorna None."""
        app = QuimeraApp(tmp_path, plugin_registry=populated_registry)
        plugin = app.get_agent_plugin("unknown-agent")
        assert plugin is None

    def test_default_registry_isolation_from_injected(self, tmp_path, populated_registry):
        """Verifica que dois apps com registries diferentes não compartilham plugins."""
        app1 = QuimeraApp(tmp_path, plugin_registry=populated_registry)
        app2 = QuimeraApp(tmp_path)  # Sem registry -> usa modulo plugins global
        assert app1.get_available_plugins() == [populated_registry.all_plugins()[0]]
        assert len(app2.get_available_plugins()) > 0  # Modulo global tem plugins

    def test_get_agent_plugin_normalizes_name_with_registry(self, tmp_path, populated_registry, sample_plugin):
        """Verifica que a normalização de nome funciona com registry injetado."""
        app = QuimeraApp(tmp_path, plugin_registry=populated_registry)

        class FakeAgent:
            name = "test-agent"

        plugin = app.get_agent_plugin(FakeAgent())
        assert plugin is sample_plugin

    def test_get_agent_plugin_empty_name_returns_none(self, tmp_path, empty_registry):
        """Verifica que nome vazio ou None retorna None mesmo com registry."""
        app = QuimeraApp(tmp_path, plugin_registry=empty_registry)
        assert app.get_agent_plugin("") is None
        assert app.get_agent_plugin(None) is None

    def test_available_commands_uses_only_selected_agents_prefixes(self, tmp_path):
        """Verifica que o autocomplete só inclui prefixos dos agentes selecionados."""
        registry = PluginRegistry()
        codex_plugin = AgentPlugin(name="codex", prefix="/codex", style=("blue", "Codex"))
        opencode_plugin = AgentPlugin(name="opencode", prefix="/opencode", style=("blue", "OpenCode"))
        registry.register(codex_plugin)
        registry.register(opencode_plugin)

        app = QuimeraApp(tmp_path, plugin_registry=registry, agents=["codex"])
        commands = app._available_commands()

        assert "/codex" in commands
        assert "/opencode" not in commands

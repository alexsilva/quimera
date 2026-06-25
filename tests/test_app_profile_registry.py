"""Tests for ProfileRegistry injection in QuimeraApp."""
from pathlib import Path

import pytest

from quimera.app.core import QuimeraApp
from quimera.profiles.base import ExecutionProfile, ProfileRegistry


@pytest.fixture
def sample_profile():
    return ExecutionProfile(
        name="test-agent",
        prefix="/test",
        style=("blue", "Test Agent"),
    )


@pytest.fixture
def empty_registry():
    return ProfileRegistry()


@pytest.fixture
def populated_registry(sample_profile):
    registry = ProfileRegistry()
    registry.register(sample_profile)
    return registry


class TestProfileRegistryInjection:
    """Testes para injeção de ProfileRegistry no QuimeraApp."""

    def test_default_no_registry_uses_profiles_module(self, tmp_path):
        """Sem registry injetado, usa profiles.get / profiles.all_profiles (fallback)."""
        app = QuimeraApp(tmp_path)
        # O módulo profiles já tem agentes registrados (claude, codex, etc.)
        available = app.get_available_profiles()
        assert len(available) > 0
        # Verifica que busca um agente conhecido do módulo global
        profile = app.get_agent_profile("codex")
        assert profile is not None
        assert profile.name == "codex"

    def test_injected_empty_registry_returns_no_profiles(self, tmp_path, empty_registry):
        """Verifica que registry vazio injetado retorna lista vazia e None para consultas."""
        app = QuimeraApp(tmp_path, profile_registry=empty_registry)
        available = app.get_available_profiles()
        assert available == []
        profile = app.get_agent_profile("codex")
        assert profile is None

    def test_injected_registry_with_profile_returns_it(self, tmp_path, populated_registry, sample_profile):
        """Verifica que registry populado retorna os profiles corretos."""
        app = QuimeraApp(tmp_path, profile_registry=populated_registry)
        available = app.get_available_profiles()
        assert available == [sample_profile]
        profile = app.get_agent_profile("test-agent")
        assert profile is sample_profile

    def test_injected_registry_unknown_agent_returns_none(self, tmp_path, populated_registry):
        """Verifica que agente inexistente no registry retorna None."""
        app = QuimeraApp(tmp_path, profile_registry=populated_registry)
        profile = app.get_agent_profile("unknown-agent")
        assert profile is None

    def test_default_registry_isolation_from_injected(self, tmp_path, populated_registry):
        """Verifica que dois apps com registries diferentes não compartilham profiles."""
        app1 = QuimeraApp(tmp_path, profile_registry=populated_registry)
        app2 = QuimeraApp(tmp_path)  # Sem registry -> usa modulo profiles global
        assert app1.get_available_profiles() == [populated_registry.all_profiles()[0]]
        assert len(app2.get_available_profiles()) > 0  # Modulo global tem profiles

    def test_get_agent_profile_normalizes_name_with_registry(self, tmp_path, populated_registry, sample_profile):
        """Verifica que a normalização de nome funciona com registry injetado."""
        app = QuimeraApp(tmp_path, profile_registry=populated_registry)

        class FakeAgent:
            name = "test-agent"

        profile = app.get_agent_profile(FakeAgent())
        assert profile is sample_profile

    def test_get_agent_profile_empty_name_returns_none(self, tmp_path, empty_registry):
        """Verifica que nome vazio ou None retorna None mesmo com registry."""
        app = QuimeraApp(tmp_path, profile_registry=empty_registry)
        assert app.get_agent_profile("") is None
        assert app.get_agent_profile(None) is None

    def test_available_commands_uses_only_selected_agents_prefixes(self, tmp_path):
        """Verifica que o autocomplete só inclui prefixos dos agentes selecionados."""
        registry = ProfileRegistry()
        codex_profile = ExecutionProfile(name="codex", prefix="/codex", style=("blue", "Codex"))
        opencode_profile = ExecutionProfile(name="opencode", prefix="/opencode", style=("blue", "OpenCode"))
        registry.register(codex_profile)
        registry.register(opencode_profile)

        app = QuimeraApp(tmp_path, profile_registry=registry, agents=["codex"])
        commands = app._available_commands()

        assert "/codex" in commands
        assert "/opencode" not in commands

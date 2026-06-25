from dataclasses import dataclass, field

from quimera.tasks.router import TaskRouter


@dataclass
class ProfileStub:
    name: str
    prefix: str = ""
    aliases: list[str] = field(default_factory=list)
    supports_task_execution: bool = True
    avoid_task_types: list[str] = field(default_factory=list)
    driver: str = "cli"


class RepositorySpy:
    def __init__(self, counts_by_status=None):
        self.counts_by_status = counts_by_status or {}
        self.calls = []

    def list_tasks(self, filt=None):
        filt = filt or {}
        self.calls.append(filt)
        status = filt.get("status")
        count = self.counts_by_status.get(status, 0)
        return [{"id": index + 1} for index in range(count)]


def test_choose_agent_with_single_profile(monkeypatch):
    """Verifica que choose_agent_with_load_balance retorna o único profile disponível."""
    profile = ProfileStub("codex")
    repository = RepositorySpy()
    router = TaskRouter(
        active_agents=["codex"],
        get_agent_profile=lambda name: profile if name == "codex" else None,
        get_available_profiles=lambda: [profile],
        repository=repository,
    )

    monkeypatch.setattr("quimera.tasks.router.score_profile_for_task", lambda _profile, _task_type: 3)

    assert router.choose_agent_with_load_balance("code_edit") == "codex"


def test_choose_agent_with_load_balance_prefers_less_busy_agent(monkeypatch):
    """Verifica que o roteador prefere o agente menos ocupado."""

    claude = ProfileStub("claude")
    codex = ProfileStub("codex")
    calls = []

    class RepositoryByAgent:
        def list_tasks(self, filt=None):
            filt = filt or {}
            calls.append(filt)
            assigned_to = filt.get("assigned_to")
            status = filt.get("status")
            if assigned_to == "claude" and status == "pending":
                return [{"id": 1}, {"id": 2}, {"id": 3}]
            return []

    router = TaskRouter(
        active_agents=["claude", "codex"],
        get_agent_profile=lambda name: {"claude": claude, "codex": codex}.get(name),
        get_available_profiles=lambda: [claude, codex],
        repository=RepositoryByAgent(),
    )
    monkeypatch.setattr("quimera.tasks.router.score_profile_for_task", lambda _profile, _task_type: 5)

    assert router.choose_agent_with_load_balance("general") == "codex"
    assert any(call.get("assigned_to") == "claude" for call in calls)
    assert any(call.get("assigned_to") == "codex" for call in calls)


def test_choose_agent_uses_fallback_when_effective_score_is_too_low(monkeypatch):
    """Verifica que o roteador usa fallback quando o score efetivo é muito baixo."""
    profile = ProfileStub("claude")
    router = TaskRouter(
        active_agents=["claude"],
        get_agent_profile=lambda _name: profile,
        get_available_profiles=lambda: [profile],
        repository=RepositorySpy(),
    )

    monkeypatch.setattr("quimera.tasks.router.score_profile_for_task", lambda _profile, _task_type: -5)
    monkeypatch.setattr("quimera.tasks.router.choose_best_agent", lambda _task_type, _profiles: "fallback-agent")

    assert router.choose_agent_with_load_balance("architecture") == "fallback-agent"


def test_get_task_routing_profiles_respects_active_agents_and_wildcard():
    """Verifica que get_task_routing_profiles respeita agentes ativos e curinga."""

    claude = ProfileStub("claude")
    codex = ProfileStub("codex")
    disabled = ProfileStub("disabled", supports_task_execution=False)

    explicit_router = TaskRouter(
        active_agents=["claude", "disabled"],
        get_agent_profile=lambda name: {"claude": claude, "disabled": disabled}.get(name),
        get_available_profiles=lambda: [claude, codex, disabled],
        repository=RepositorySpy(),
    )
    wildcard_router = TaskRouter(
        active_agents=["*"],
        get_agent_profile=lambda name: {"claude": claude, "codex": codex, "disabled": disabled}.get(name),
        get_available_profiles=lambda: [claude, codex, disabled],
        repository=RepositorySpy(),
    )

    assert [profile.name for profile in explicit_router.get_task_routing_profiles()] == ["claude"]
    assert [profile.name for profile in wildcard_router.get_task_routing_profiles()] == ["claude", "codex"]


def test_count_agent_open_tasks_uses_repository_list_tasks():
    """Verifica que count_agent_open_tasks consulta o repositório corretamente."""
    repository = RepositorySpy({"pending": 2, "in_progress": 1})
    profile = ProfileStub("codex")
    router = TaskRouter(
        active_agents=["codex"],
        get_agent_profile=lambda _name: profile,
        get_available_profiles=lambda: [profile],
        repository=repository,
    )

    count = router.count_agent_open_tasks("codex")

    assert count == 3
    assert repository.calls == [
        {"assigned_to": "codex", "status": "pending"},
        {"assigned_to": "codex", "status": "in_progress"},
    ]


def test_get_task_routing_profiles_resolves_name_case_and_prefix_without_direct_lookup():
    """Verifica que get_task_routing_profiles resolve nomes com case diferente e prefixos."""

    codex = ProfileStub("codex", prefix="/codex", aliases=["/code"])
    opencode = ProfileStub("opencode", prefix="/opencode")
    router = TaskRouter(
        active_agents=["CODEX", "/opencode", "/ghost"],
        get_agent_profile=lambda _name: None,
        get_available_profiles=lambda: [codex, opencode],
        repository=RepositorySpy(),
    )

    selected = [profile.name for profile in router.get_task_routing_profiles()]

    assert selected == ["codex", "opencode"]


def test_get_task_routing_profiles_deduplicates_same_profile_when_name_and_alias_are_active():
    """Verifica que get_task_routing_profiles deduplica quando nome e alias estão ativos."""

    codex = ProfileStub("codex", prefix="/codex", aliases=["/code"])
    router = TaskRouter(
        active_agents=["codex", "/code"],
        get_agent_profile=lambda _name: None,
        get_available_profiles=lambda: [codex],
        repository=RepositorySpy(),
    )

    selected = [profile.name for profile in router.get_task_routing_profiles()]

    assert selected == ["codex"]

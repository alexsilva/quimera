from dataclasses import dataclass, field

from quimera.app.task_router import TaskRouter


@dataclass
class PluginStub:
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


def test_choose_agent_with_single_plugin(monkeypatch):
    plugin = PluginStub("codex")
    repository = RepositorySpy()
    router = TaskRouter(
        active_agents=["codex"],
        get_agent_plugin=lambda name: plugin if name == "codex" else None,
        get_available_plugins=lambda: [plugin],
        repository=repository,
    )

    monkeypatch.setattr("quimera.app.task_router.score_plugin_for_task", lambda _plugin, _task_type: 3)

    assert router.choose_agent_with_load_balance("code_edit") == "codex"


def test_choose_agent_with_load_balance_prefers_less_busy_agent(monkeypatch):
    claude = PluginStub("claude")
    codex = PluginStub("codex")
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
        get_agent_plugin=lambda name: {"claude": claude, "codex": codex}.get(name),
        get_available_plugins=lambda: [claude, codex],
        repository=RepositoryByAgent(),
    )
    monkeypatch.setattr("quimera.app.task_router.score_plugin_for_task", lambda _plugin, _task_type: 5)

    assert router.choose_agent_with_load_balance("general") == "codex"
    assert any(call.get("assigned_to") == "claude" for call in calls)
    assert any(call.get("assigned_to") == "codex" for call in calls)


def test_choose_agent_uses_fallback_when_effective_score_is_too_low(monkeypatch):
    plugin = PluginStub("claude")
    router = TaskRouter(
        active_agents=["claude"],
        get_agent_plugin=lambda _name: plugin,
        get_available_plugins=lambda: [plugin],
        repository=RepositorySpy(),
    )

    monkeypatch.setattr("quimera.app.task_router.score_plugin_for_task", lambda _plugin, _task_type: -5)
    monkeypatch.setattr("quimera.app.task_router.choose_best_agent", lambda _task_type, _plugins: "fallback-agent")

    assert router.choose_agent_with_load_balance("architecture") == "fallback-agent"


def test_get_task_routing_plugins_respects_active_agents_and_wildcard():
    claude = PluginStub("claude")
    codex = PluginStub("codex")
    disabled = PluginStub("disabled", supports_task_execution=False)

    explicit_router = TaskRouter(
        active_agents=["claude", "disabled"],
        get_agent_plugin=lambda name: {"claude": claude, "disabled": disabled}.get(name),
        get_available_plugins=lambda: [claude, codex, disabled],
        repository=RepositorySpy(),
    )
    wildcard_router = TaskRouter(
        active_agents=["*"],
        get_agent_plugin=lambda name: {"claude": claude, "codex": codex, "disabled": disabled}.get(name),
        get_available_plugins=lambda: [claude, codex, disabled],
        repository=RepositorySpy(),
    )

    assert [plugin.name for plugin in explicit_router.get_task_routing_plugins()] == ["claude"]
    assert [plugin.name for plugin in wildcard_router.get_task_routing_plugins()] == ["claude", "codex"]


def test_count_agent_open_tasks_uses_repository_list_tasks():
    repository = RepositorySpy({"pending": 2, "in_progress": 1})
    plugin = PluginStub("codex")
    router = TaskRouter(
        active_agents=["codex"],
        get_agent_plugin=lambda _name: plugin,
        get_available_plugins=lambda: [plugin],
        repository=repository,
    )

    count = router.count_agent_open_tasks("codex")

    assert count == 3
    assert repository.calls == [
        {"assigned_to": "codex", "status": "pending"},
        {"assigned_to": "codex", "status": "in_progress"},
    ]


def test_get_task_routing_plugins_resolves_name_case_and_prefix_without_direct_lookup():
    codex = PluginStub("codex", prefix="/codex", aliases=["/code"])
    opencode = PluginStub("opencode", prefix="/opencode")
    router = TaskRouter(
        active_agents=["CODEX", "/opencode", "/ghost"],
        get_agent_plugin=lambda _name: None,
        get_available_plugins=lambda: [codex, opencode],
        repository=RepositorySpy(),
    )

    selected = [plugin.name for plugin in router.get_task_routing_plugins()]

    assert selected == ["codex", "opencode"]


def test_get_task_routing_plugins_deduplicates_same_plugin_when_name_and_alias_are_active():
    codex = PluginStub("codex", prefix="/codex", aliases=["/code"])
    router = TaskRouter(
        active_agents=["codex", "/code"],
        get_agent_plugin=lambda _name: None,
        get_available_plugins=lambda: [codex],
        repository=RepositorySpy(),
    )

    selected = [plugin.name for plugin in router.get_task_routing_plugins()]

    assert selected == ["codex"]

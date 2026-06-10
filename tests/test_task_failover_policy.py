from dataclasses import dataclass

from quimera.app.task_failover_policy import TaskFailoverPolicy


@dataclass
class PluginStub:
    name: str = "agent"
    prefix: str = ""
    aliases: list[str] | None = None
    supports_task_execution: bool = True


class RepositorySpy:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def can_reassign_task(self, task_id, candidate_agents):
        self.calls.append((task_id, list(candidate_agents)))
        return self.result


def test_is_operational_review_agent_returns_false_for_inactive_agent():
    """Verifica que is_operational_review_agent retorna False para agente inativo."""
    repository = RepositorySpy()
    policy = TaskFailoverPolicy(
        active_agents=["codex"],
        get_agent_plugin=lambda name: PluginStub(name="codex") if name == "codex" else None,
        repository=repository,
    )

    assert policy.is_operational_review_agent("pickle") is False


def test_is_operational_review_agent_returns_false_without_task_execution_support():
    """Verifica que is_operational_review_agent retorna False se o agente não suporta execução de tarefas."""
    repository = RepositorySpy()
    policy = TaskFailoverPolicy(
        active_agents=["codex"],
        get_agent_plugin=lambda name: (
            PluginStub(name="codex", supports_task_execution=False)
            if name == "codex"
            else None
        ),
        repository=repository,
    )

    assert policy.is_operational_review_agent("codex") is False


def test_review_agents_for_excludes_executor_and_explicit_excluded_agents():
    """Verifica que review_agents_for exclui o executor e agentes explicitamente excluídos."""
    repository = RepositorySpy()
    plugins = {
        "codex": PluginStub(name="codex"),
        "pickle": PluginStub(name="pickle"),
        "deepseek-pro-v4": PluginStub(name="deepseek-pro-v4"),
    }
    policy = TaskFailoverPolicy(
        active_agents=["codex", "pickle", "deepseek-pro-v4"],
        get_agent_plugin=lambda name: plugins.get(name),
        repository=repository,
    )

    candidates = policy.review_agents_for(
        executor_agent="codex",
        exclude_agents={"deepseek-pro-v4"},
    )

    assert candidates == ["pickle"]


def test_can_failover_delegates_to_repository():
    """Verifica que can_failover delega a decisão para o repositório."""

    repository = RepositorySpy(result=True)
    policy = TaskFailoverPolicy(
        active_agents=["codex", "pickle", "deepseek-pro-v4"],
        get_agent_plugin=lambda name: PluginStub(name=name),
        repository=repository,
    )

    assert policy.can_failover(task_id=7, failed_agent="codex") is True
    assert repository.calls == [(7, ["pickle", "deepseek-pro-v4"])]


def test_has_review_failover_returns_boolean_based_on_remaining_candidates():
    """Verifica que has_review_failover retorna booleano baseado em candidatos restantes."""
    repository = RepositorySpy()
    policy_without_fallback = TaskFailoverPolicy(
        active_agents=["codex", "pickle"],
        get_agent_plugin=lambda name: PluginStub(name=name),
        repository=repository,
    )
    policy_with_fallback = TaskFailoverPolicy(
        active_agents=["codex", "pickle", "deepseek-pro-v4"],
        get_agent_plugin=lambda name: PluginStub(name=name),
        repository=repository,
    )

    assert policy_without_fallback.has_review_failover("codex", "pickle") is False
    assert policy_with_fallback.has_review_failover("codex", "pickle") is True


def test_review_agents_for_treats_alias_as_same_agent_identity():
    """Verifica que review_agents_for trata alias como mesma identidade do agente."""
    repository = RepositorySpy()
    plugins = {
        "codex": PluginStub(name="codex", prefix="/codex", aliases=["/code"]),
        "opencode": PluginStub(name="opencode", prefix="/opencode"),
    }
    policy = TaskFailoverPolicy(
        active_agents=["codex", "opencode"],
        get_agent_plugin=lambda name: plugins.get(name),
        repository=repository,
    )

    candidates = policy.review_agents_for(executor_agent="/code")

    assert candidates == ["opencode"]

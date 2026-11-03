from dataclasses import dataclass

from quimera.tasks.failover import TaskFailoverPolicy


@dataclass
class ProfileStub:
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

    def can_review_task(self, task_id, candidate_agents):
        self.calls.append((task_id, list(candidate_agents)))
        return bool(candidate_agents) and self.result


def test_is_operational_review_agent_returns_false_for_inactive_agent():
    """Verifica que is_operational_review_agent retorna False para agente inativo."""
    repository = RepositorySpy()
    policy = TaskFailoverPolicy(
        active_agents=["codex"],
        get_agent_profile=lambda name: ProfileStub(name="codex") if name == "codex" else None,
        repository=repository,
    )

    assert policy.is_operational_review_agent("pickle") is False


def test_is_operational_review_agent_returns_false_without_task_execution_support():
    """Verifica que is_operational_review_agent retorna False se o agente não suporta execução de tarefas."""
    repository = RepositorySpy()
    policy = TaskFailoverPolicy(
        active_agents=["codex"],
        get_agent_profile=lambda name: (
            ProfileStub(name="codex", supports_task_execution=False)
            if name == "codex"
            else None
        ),
        repository=repository,
    )

    assert policy.is_operational_review_agent("codex") is False


def test_review_agents_for_excludes_executor_and_explicit_excluded_agents():
    """Verifica que review_agents_for exclui o executor e agentes explicitamente excluídos."""
    repository = RepositorySpy()
    profiles = {
        "codex": ProfileStub(name="codex"),
        "pickle": ProfileStub(name="pickle"),
        "deepseek-pro-v4": ProfileStub(name="deepseek-pro-v4"),
    }
    policy = TaskFailoverPolicy(
        active_agents=["codex", "pickle", "deepseek-pro-v4"],
        get_agent_profile=lambda name: profiles.get(name),
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
        get_agent_profile=lambda name: ProfileStub(name=name),
        repository=repository,
    )

    assert policy.can_failover(task_id=7, failed_agent="codex") is True
    assert repository.calls == [(7, ["pickle", "deepseek-pro-v4"])]


def test_can_review_counts_pool_members_except_executor_and_excluded():
    """Revisores restantes: membros do pool, fora o executor e quem acabou de falhar."""
    repository = RepositorySpy()
    policy_without_fallback = TaskFailoverPolicy(
        active_agents=["codex", "pickle"],
        get_agent_profile=lambda name: ProfileStub(name=name),
        repository=repository,
    )
    policy_with_fallback = TaskFailoverPolicy(
        active_agents=["codex", "pickle", "deepseek-pro-v4"],
        get_agent_profile=lambda name: ProfileStub(name=name),
        repository=repository,
    )

    assert policy_without_fallback.can_review(7, "codex", exclude_agents={"pickle"}) is False
    assert policy_with_fallback.can_review(7, "codex", exclude_agents={"pickle"}) is True
    assert repository.calls[-1] == (7, ["deepseek-pro-v4"])


def test_suspended_members_still_count_for_failover_but_are_not_available():
    """Um 429 de segundos não faz a task falhar: o suspenso volta e conta como candidato."""
    repository = RepositorySpy()
    policy = TaskFailoverPolicy(
        active_agents=["codex", "pickle"],
        get_agent_profile=lambda name: ProfileStub(name=name),
        repository=repository,
        available_agents=lambda: ["codex"],
    )

    assert policy.can_failover(task_id=7, failed_agent="codex") is True
    assert repository.calls == [(7, ["pickle"])]
    assert policy.is_available("codex") is True
    assert policy.is_available("pickle") is False
    assert policy.review_agents_for("codex") == ["pickle"]


def test_review_agents_for_treats_alias_as_same_agent_identity():
    """Verifica que review_agents_for trata alias como mesma identidade do agente."""
    repository = RepositorySpy()
    profiles = {
        "codex": ProfileStub(name="codex", prefix="/codex", aliases=["/code"]),
        "opencode": ProfileStub(name="opencode", prefix="/opencode"),
    }
    policy = TaskFailoverPolicy(
        active_agents=["codex", "opencode"],
        get_agent_profile=lambda name: profiles.get(name),
        repository=repository,
    )

    candidates = policy.review_agents_for(executor_agent="/code")

    assert candidates == ["opencode"]


def test_is_active_agent_checks_pool_membership_with_prefix_and_alias():
    """Verifica que is_active_agent segue o pool atual, aceitando prefixo e alias."""
    pool = ["codex", "opencode"]
    profiles = {
        "codex": ProfileStub(name="codex", prefix="/codex"),
        "opencode": ProfileStub(name="opencode", prefix="/code", aliases=["pickle"]),
    }
    policy = TaskFailoverPolicy(
        active_agents=lambda: pool,
        get_agent_profile=lambda name: profiles.get(name),
        repository=RepositorySpy(),
    )

    assert policy.is_active_agent("codex") is True
    assert policy.is_active_agent("/code") is True
    assert policy.is_active_agent("pickle") is True
    assert policy.is_active_agent("gemini") is False

    pool.remove("codex")
    assert policy.is_active_agent("codex") is False

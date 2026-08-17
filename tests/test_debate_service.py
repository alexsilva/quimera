import json
import time

import pytest

from quimera.debate.commands import DebateCommand
from quimera.debate.models import (
    DebateEvidence,
    DebateMode,
    DebateProtocolError,
    DebateStatus,
)
from quimera.debate.repository import DebateRepository
from quimera.debate.service import DebateService
from quimera.tasks.repository import TaskRepository


class _Renderer:
    def __init__(self):
        self.messages = []

    def show_message(self, agent, content):
        self.messages.append((agent, content))

    def flush_quick(self):
        return True


class _Client:
    def __init__(self, cancel_event):
        self.cancel_event = cancel_event
        self.cancel_sources = []

    def cancel_active_work(self, source=None):
        self.cancel_sources.append(source)
        self.cancel_event.set()


class _Dispatch:
    def __init__(
        self,
        cancel_event,
        calls,
        *,
        blocking=False,
        delay=0.0,
        round_two_vote="support",
        critical_agent=None,
        invalid_once=False,
        broken_agents=(),
        ignore_cancel=False,
        invalid_evidence_once=False,
    ):
        self.client = _Client(cancel_event)
        self.calls = calls
        self.blocking = blocking
        self.delay = delay
        self.round_two_vote = round_two_vote
        self.critical_agent = critical_agent
        self.invalid_once = invalid_once
        self.broken_agents = set(broken_agents)
        self.ignore_cancel = ignore_cancel
        self.invalid_evidence_once = invalid_evidence_once

    def _get_agent_client(self):
        return self.client

    def close(self):
        return None

    def delegate(self, agent, **options):
        prompt = options["request_override"]
        self.calls.append((agent, options))
        if self.blocking:
            while not self.client.cancel_event.wait(0.01):
                pass
            return None
        if self.delay:
            if self.ignore_cancel:
                time.sleep(self.delay)
                return None
            deadline = time.monotonic() + self.delay
            while time.monotonic() < deadline:
                if self.client.cancel_event.wait(0.005):
                    return None
        delegation_id = options["delegation"]["delegation_id"]
        if agent in self.broken_agents and "synthesis" not in delegation_id:
            return "{}"
        if self.invalid_once and ":participant:" in delegation_id:
            return json.dumps(
                {**_contribution(agent, vote="propose"), "confidence": "0.9"}
            )
        if self.invalid_evidence_once and ":participant:" in delegation_id:
            payload = _contribution(agent, vote="propose")
            payload["evidence"][0]["excerpt"] = "trecho inventado"
            return json.dumps(payload)
        if "TIPO_DE_CHAMADA: debate_synthesis" in prompt:
            workflow = '"mode":"workflow"' in prompt
            return json.dumps(
                {
                    "summary": "sintese comum",
                    "verdict": "seguir o plano" if workflow else "usar a opcao A",
                    "agreements": ["criterio comum"],
                    "disagreements": [],
                    "critical_objections": [],
                    "confidence": 0.8,
                    "consensus_reached": False,
                    "evidence_ids": ["E1"],
                    "evidence": _evidence(),
                    "work_items": (
                        [
                            {
                                "id": "T1",
                                "title": "Implementar",
                                "description": "implementar base",
                                "task_type": "code_edit",
                                "assigned_to": "alpha",
                                "dependencies": [],
                                "acceptance_criteria": ["testes passam"],
                                "priority": "high",
                                "evidence_ids": ["E1"],
                            },
                            {
                                "id": "T2",
                                "title": "Revisar",
                                "description": "revisar implementacao",
                                "task_type": "code_review",
                                "assigned_to": "beta",
                                "dependencies": ["T1"],
                                "acceptance_criteria": ["sem findings"],
                                "priority": "medium",
                                "evidence_ids": ["E1"],
                            },
                        ]
                        if workflow
                        else []
                    ),
                }
            )
        round_two = ":r2:" in delegation_id
        return json.dumps(
            _contribution(
                agent,
                vote=self.round_two_vote if round_two else "propose",
                critical=round_two and agent == self.critical_agent,
            )
        )


def _contribution(agent, *, vote, critical=False):
    return {
        "position": f"posicao de {agent}",
        "arguments": ["argumento"],
        "objections": ["risco critico"] if critical else [],
        "proposal": "proposta",
        "confidence": 0.9,
        "vote": vote,
        "critical_objection": critical,
        "evidence_ids": ["E1"],
        "evidence": _evidence(),
        "work_items": [],
    }


def _evidence():
    return [
        {
            "id": "E1",
            "source": "evidence.py",
            "line_start": 1,
            "line_end": 2,
            "excerpt": "def verified_behavior():",
            "claim": "o comportamento citado existe no workspace",
        }
    ]


def _make_service(tmp_path, *, blocking=False, history_provider=None, **dispatch_options):
    (tmp_path / "evidence.py").write_text(
        "def verified_behavior():\n    return True\n", encoding="utf-8"
    )
    db_path = str(tmp_path / "debate.db")
    task_repository = TaskRepository(db_path)
    job_id = task_repository.add_job("test")
    debate_repository = DebateRepository(db_path)
    renderer = _Renderer()
    calls = []
    persisted = []
    notices = []
    service = DebateService(
        repository=debate_repository,
        task_repository=task_repository,
        dispatch_factory=lambda event: _Dispatch(
            event,
            calls,
            blocking=blocking,
            **dispatch_options,
        ),
        active_agents=lambda: ["alpha", "beta", "gamma"],
        renderer=renderer,
        session_id="session-test",
        current_job_id=job_id,
        staging_root=tmp_path / "staging",
        workspace_root=tmp_path,
        persist_message=lambda agent, content: persisted.append((agent, content)),
        notify_tasks_changed=lambda: notices.append("tasks"),
        history_provider=history_provider,
    )
    return (
        service,
        debate_repository,
        task_repository,
        renderer,
        calls,
        persisted,
        notices,
    )


def test_service_recovers_expired_sessions_on_startup(tmp_path, monkeypatch):
    recovered = []
    monkeypatch.setattr(
        DebateRepository,
        "recover_expired",
        lambda repository: recovered.append(repository.db_path) or 0,
    )

    _make_service(tmp_path)

    assert recovered == [str(tmp_path / "debate.db")]


def test_context_flag_snapshots_recent_chat_history(tmp_path):
    # roles reais da sessao: "human" para o usuario, nome do agente na resposta
    history = [
        {"role": "human", "content": "qual bug estamos investigando?"},
        {"role": "claude-fable", "content": "o scroll do feed nao vai ao fim"},
        {"role": "tool", "content": "saida de ferramenta ignorada"},
        {"role": "human", "content": "   "},
    ]
    service, repository, *_ = _make_service(tmp_path, history_provider=lambda: history)

    session = service.start(
        DebateCommand(
            action="start",
            topic="decidir correcao",
            include_context=True,
            timeout_seconds=2,
        )
    )
    service.wait(timeout=10)

    expected = (
        "[user] qual bug estamos investigando?\n\n"
        "[claude-fable] o scroll do feed nao vai ao fim"
    )
    assert session.context == expected
    assert repository.get_session(session.id).context == expected


def test_context_flag_fails_when_history_is_empty(tmp_path):
    service, *_ = _make_service(tmp_path, history_provider=lambda: [])
    with pytest.raises(ValueError, match="historico do chat vazio"):
        service.start(
            DebateCommand(
                action="start",
                topic="tema",
                include_context=True,
                timeout_seconds=2,
            )
        )


def test_context_flag_without_flag_keeps_context_empty(tmp_path):
    service, repository, *_ = _make_service(
        tmp_path, history_provider=lambda: [{"role": "user", "content": "oi"}]
    )
    session = service.start(
        DebateCommand(action="start", topic="tema", timeout_seconds=2)
    )
    service.wait(timeout=10)
    assert session.context == ""
    assert repository.get_session(session.id).context == ""


def test_context_flag_truncates_history_keeping_recent_messages(tmp_path):
    history = [{"role": "user", "content": f"mensagem {i} " + "x" * 500} for i in range(40)]
    service, *_ = _make_service(tmp_path, history_provider=lambda: history)

    context = service._build_chat_context()

    assert len(context) <= 8_000
    assert "mensagem 39" in context
    assert "mensagem 0 " not in context


def test_context_flag_fails_without_history_provider(tmp_path):
    service, *_ = _make_service(tmp_path)
    with pytest.raises(ValueError, match="historico do chat nao acessivel"):
        service.start(
            DebateCommand(
                action="start",
                topic="tema",
                include_context=True,
                timeout_seconds=2,
            )
        )


def test_debate_runs_two_rounds_and_converges(tmp_path):
    service, repository, _, renderer, calls, persisted, _ = _make_service(tmp_path)
    session = service.start(
        DebateCommand(action="start", topic="escolher A ou B", timeout_seconds=2)
    )

    assert service.wait(5)
    loaded = repository.get_session(session.id)
    assert loaded is not None
    assert loaded.status == DebateStatus.CONVERGED
    assert loaded.current_round == 2
    assert len(repository.list_contributions(session.id)) == 6
    assert len(calls) == 7
    assert all(call[1]["show_delegation"] is False for call in calls)
    assert all(call[1]["emit_run_deltas"] is False for call in calls)
    assert all(
        "whatever tools your environment provides" in call[1]["request_override"]
        and '"evidence"' in call[1]["request_override"]
        for call in calls
    )
    assert any("Resultado: **consenso**" in content for _, content in renderer.messages)
    assert len(persisted) == 1


def test_round_runs_sequentially_and_shares_same_round_contributions(tmp_path):
    service, _, _, _, calls, _, _ = _make_service(tmp_path)
    session = service.start(
        DebateCommand(action="start", topic="ordem sequencial", timeout_seconds=30)
    )

    assert service.wait(10)
    assert session is not None
    round_one = [
        (agent, options)
        for agent, options in calls
        if ":r1:participant:" in options["delegation"]["delegation_id"]
    ]
    assert [agent for agent, _ in round_one] == ["alpha", "beta", "gamma"]

    def _current_round_agents(options):
        snapshot = json.loads(
            options["request_override"].split("SNAPSHOT_JSON:\n", 1)[1]
        )
        return [item["agent"] for item in snapshot["current_round_contributions"]]

    assert _current_round_agents(round_one[0][1]) == []
    assert _current_round_agents(round_one[1][1]) == ["alpha"]
    assert _current_round_agents(round_one[2][1]) == ["alpha", "beta"]


def test_workflow_apply_is_idempotent_and_enforces_dependencies(tmp_path):
    service, repository, task_repository, _, _, _, notices = _make_service(tmp_path)
    session = service.start(
        DebateCommand(
            action="start",
            topic="dividir entrega",
            mode=DebateMode.WORKFLOW,
            agents=("alpha", "beta"),
            timeout_seconds=2,
        )
    )
    assert service.wait(5)
    assert repository.get_session(session.id).status == DebateStatus.CONVERGED

    first = service.apply(session.id)
    second = service.apply(session.id)

    assert first == second
    assert len(first) == 2
    assert notices == ["tasks"]
    assert task_repository.list_task_dependencies(first[1]) == [first[0]]
    assert task_repository.claim_task("beta") is None
    assert task_repository.claim_task("alpha") == first[0]
    assert task_repository.complete_task(first[0], result="ok") is True
    assert task_repository.claim_task("beta") == first[1]


def test_cancel_stops_active_participants(tmp_path):
    service, repository, _, _, _, _, _ = _make_service(tmp_path, blocking=True)
    session = service.start(
        DebateCommand(action="start", topic="bloquear", timeout_seconds=30)
    )
    deadline = time.monotonic() + 2
    while service.active_id != session.id and time.monotonic() < deadline:
        time.sleep(0.01)

    assert service.cancel(session.id) is True
    assert service.wait(5)
    loaded = repository.get_session(session.id)
    assert loaded is not None
    assert loaded.status == DebateStatus.CANCELLED


def test_cancel_fn_declares_system_source_on_timeout_and_user_on_cancel():
    import threading

    from quimera.domain.execution import ExecutionControlSource

    root_cancel = threading.Event()
    client = _Client(threading.Event())
    cancel_fn = DebateService._make_cancel_fn(client, root_cancel)
    assert cancel_fn is not None

    cancel_fn()
    assert client.cancel_sources == [ExecutionControlSource.SYSTEM]

    root_cancel.set()
    cancel_fn()
    assert client.cancel_sources[-1] == ExecutionControlSource.USER


def test_cancel_fn_handles_missing_client_and_legacy_signature():
    import threading

    assert DebateService._make_cancel_fn(None, threading.Event()) is None

    class _Legacy:
        def __init__(self):
            self.calls = 0

        def cancel_active_work(self):
            self.calls += 1

    legacy = _Legacy()
    cancel_fn = DebateService._make_cancel_fn(legacy, threading.Event())
    assert cancel_fn is not None
    cancel_fn()
    assert legacy.calls == 1


def test_service_allows_only_one_active_debate_and_closes_ingress(tmp_path):
    service, repository, _, _, _, _, _ = _make_service(tmp_path, blocking=True)
    first = service.start(
        DebateCommand(action="start", topic="primeiro", timeout_seconds=30)
    )

    with pytest.raises(ValueError, match="ja existe um debate ativo"):
        service.start(
            DebateCommand(action="start", topic="segundo", timeout_seconds=30)
        )

    service.shutdown(timeout=5)
    assert repository.get_session(first.id).status == DebateStatus.CANCELLED
    with pytest.raises(RuntimeError, match="encerrado"):
        service.start(
            DebateCommand(action="start", topic="terceiro", timeout_seconds=30)
        )


def test_debate_exhausts_without_consensus_and_preserves_dissent(tmp_path):
    service, repository, _, renderer, _, persisted, _ = _make_service(
        tmp_path,
        round_two_vote="oppose",
    )
    session = service.start(
        DebateCommand(action="start", topic="tema controverso", timeout_seconds=2)
    )

    assert service.wait(5)
    loaded = repository.get_session(session.id)
    assert loaded is not None
    assert loaded.status == DebateStatus.EXHAUSTED
    assert loaded.result is not None
    assert loaded.result.consensus_reached is False
    assert any(
        "Resultado: **sem consenso**" in content for _, content in renderer.messages
    )
    assert len(persisted) == 1


def test_critical_objection_prevents_false_consensus(tmp_path):
    service, repository, _, _, _, _, _ = _make_service(
        tmp_path,
        critical_agent="gamma",
    )
    session = service.start(
        DebateCommand(action="start", topic="avaliar risco", timeout_seconds=2)
    )

    assert service.wait(5)
    loaded = repository.get_session(session.id)
    assert loaded is not None
    assert loaded.status == DebateStatus.EXHAUSTED
    assert loaded.result is not None
    assert loaded.result.consensus_reached is False
    assert "risco critico" in loaded.result.critical_objections


def test_invalid_protocol_gets_one_repair_attempt(tmp_path):
    service, repository, _, _, calls, _, _ = _make_service(tmp_path, invalid_once=True)
    session = service.start(
        DebateCommand(action="start", topic="reparar protocolo", timeout_seconds=2)
    )

    assert service.wait(5)
    loaded = repository.get_session(session.id)
    assert loaded is not None
    assert loaded.status == DebateStatus.CONVERGED
    repair_calls = [
        options["delegation"]["delegation_id"]
        for _, options in calls
        if "participant-repair" in options["delegation"]["delegation_id"]
    ]
    assert len(repair_calls) == 6
    assert len(repair_calls) == len(set(repair_calls))


def test_unverifiable_evidence_gets_one_repair_attempt(tmp_path):
    service, repository, _, _, calls, _, _ = _make_service(
        tmp_path, invalid_evidence_once=True
    )
    session = service.start(
        DebateCommand(action="start", topic="provar afirmacoes", timeout_seconds=2)
    )

    assert service.wait(5)
    assert repository.get_session(session.id).status == DebateStatus.CONVERGED
    repair_calls = [
        options["delegation"]["delegation_id"]
        for _, options in calls
        if "participant-repair" in options["delegation"]["delegation_id"]
    ]
    assert len(repair_calls) == 6


def test_evidence_verifier_rejects_symlink_escape_and_sensitive_files(tmp_path):
    service, *_ = _make_service(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service._workspace_root = workspace.resolve()
    outside = tmp_path / "outside.py"
    outside.write_text("outside_value = True\n", encoding="utf-8")
    (workspace / "escape.py").symlink_to(outside)
    (workspace / ".env").write_text("SECRET=value\n", encoding="utf-8")

    with pytest.raises(DebateProtocolError, match="fora do workspace"):
        service._verify_evidence(
            (
                DebateEvidence(
                    "E1", "escape.py", 1, 1, "outside_value = True", "escape"
                ),
            )
        )
    with pytest.raises(DebateProtocolError, match="arquivo sensivel"):
        service._verify_evidence(
            (DebateEvidence("E1", ".env", 1, 1, "SECRET=value", "secret"),)
        )


def test_web_evidence_verified_against_fetched_page_with_cache(tmp_path):
    service, *_ = _make_service(tmp_path)
    fetches = []

    def fetcher(url):
        fetches.append(url)
        return "A documentacao oficial recomenda pipeline em vez de barreiras.\nOutra frase."

    service._web_fetcher = fetcher
    evidence = (
        DebateEvidence(
            "E1",
            "https://example.com/docs",
            0,
            0,
            "documentacao oficial recomenda pipeline",
            "doc recomenda pipeline",
            kind="web",
        ),
        DebateEvidence(
            "E2",
            "https://example.com/docs",
            0,
            0,
            "pipeline em vez de barreiras",
            "doc desaconselha barreiras",
            kind="web",
        ),
    )

    service._verify_evidence(evidence)

    assert fetches == ["https://example.com/docs"]


def test_web_evidence_rejects_mismatch_short_excerpt_and_fetch_failure(tmp_path):
    service, *_ = _make_service(tmp_path)

    def make(excerpt):
        return (
            DebateEvidence(
                "E1",
                "https://example.com/docs",
                0,
                0,
                excerpt,
                "afirmacao",
                kind="web",
            ),
        )

    service._web_fetcher = lambda url: "conteudo real da pagina publicada hoje"
    with pytest.raises(DebateProtocolError, match="nao corresponde ao conteudo"):
        service._verify_evidence(make("trecho que nao existe na pagina citada"))

    with pytest.raises(DebateProtocolError, match="curto demais"):
        service._verify_evidence(make("curto"))

    def broken(url):
        raise TimeoutError("timeout de rede")

    service._web_fetcher = broken
    with pytest.raises(DebateProtocolError, match="falha ao buscar"):
        service._verify_evidence(make("trecho longo o suficiente para verificar"))


def test_debate_fails_when_repair_cannot_restore_quorum(tmp_path):
    service, repository, _, _, _, _, _ = _make_service(
        tmp_path,
        broken_agents=("beta", "gamma"),
    )
    session = service.start(
        DebateCommand(action="start", topic="quorum", timeout_seconds=2)
    )

    assert service.wait(5)
    loaded = repository.get_session(session.id)
    assert loaded is not None
    assert loaded.status == DebateStatus.FAILED
    assert "quorum nao atingido" in loaded.error


def test_timeout_is_global_across_round_and_synthesis(tmp_path):
    service, repository, _, _, _, _, _ = _make_service(tmp_path, delay=0.03)
    session = service.start(
        DebateCommand(
            action="start",
            topic="timeout global",
            timeout_seconds=0.04,
        )
    )

    assert service.wait(2)
    loaded = repository.get_session(session.id)
    assert loaded is not None
    assert loaded.status == DebateStatus.FAILED
    assert "timeout total" in loaded.error


def test_timeout_does_not_wait_for_uncooperative_backend(tmp_path):
    service, repository, _, _, _, _, _ = _make_service(
        tmp_path,
        delay=2.0,
        ignore_cancel=True,
    )
    started = time.monotonic()
    session = service.start(
        DebateCommand(
            action="start",
            topic="backend nao cooperativo",
            timeout_seconds=0.04,
        )
    )

    assert service.wait(1.0)
    assert time.monotonic() - started < 1.0
    loaded = repository.get_session(session.id)
    assert loaded is not None
    assert loaded.status == DebateStatus.FAILED
    assert "timeout total" in loaded.error

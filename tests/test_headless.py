"""Execução única sem humano: ``quimera --agents A B --prompt "..."``.

Cobre as peças (sessão marcada, broker sem humano, ask_user sem resposta,
runner com fallback e timeout, validação da CLI) e o caminho de ponta a ponta
com os agentes fake, num subprocesso com ``HOME`` isolado (preset ``strict``).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from quimera import cli
from quimera.app.headless import (
    EXIT_ALL_FAILED,
    EXIT_OK,
    EXIT_TIMEOUT,
    HeadlessAttempt,
    HeadlessResult,
    run_headless_prompt,
)
from quimera.app.unattended_input_gate import UnattendedInputGate
from quimera.runtime.input_broker import AskUserUnanswered, InputBroker
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.interaction import InteractionTools
from quimera.storage import HEADLESS_SESSION, INTERACTIVE_SESSION, SessionStorage

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── Sessão marcada ─────────────────────────────────────────────────────────


def _save(logs_dir: Path, mode: str, messages: list[dict]) -> SessionStorage:
    storage = SessionStorage(logs_dir, mode=mode)
    storage.save_history(messages)
    return storage


def test_headless_session_is_saved_with_its_mode(tmp_path: Path) -> None:
    storage = _save(tmp_path, HEADLESS_SESSION, [{"role": "human", "content": "oi"}])
    payload = json.loads(storage.history_file.read_text(encoding="utf-8"))
    assert payload["mode"] == HEADLESS_SESSION
    assert payload["messages"] == [{"role": "human", "content": "oi"}]


def test_interactive_session_does_not_restore_a_headless_run(tmp_path: Path) -> None:
    _save(tmp_path, INTERACTIVE_SESSION, [{"role": "human", "content": "conversa"}])
    time.sleep(1.1)  # session_id tem resolução de segundo
    _save(tmp_path, HEADLESS_SESSION, [{"role": "human", "content": "script"}])

    restored = SessionStorage(tmp_path).load_last_session()
    assert restored["messages"] == [{"role": "human", "content": "conversa"}]


def test_headless_session_starts_clean(tmp_path: Path) -> None:
    _save(tmp_path, INTERACTIVE_SESSION, [{"role": "human", "content": "conversa"}])
    restored = SessionStorage(tmp_path, mode=HEADLESS_SESSION).load_last_session()
    assert restored == {"messages": [], "shared_state": {}}


def test_headless_runs_at_the_same_instant_preserve_every_snapshot(tmp_path: Path, monkeypatch) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 2, 9, 20, 0, tzinfo=tz)

    monkeypatch.setattr("quimera.storage.datetime", FrozenDatetime)
    chat = _save(tmp_path, INTERACTIVE_SESSION, [{"role": "human", "content": "conversa"}])
    first = _save(tmp_path, HEADLESS_SESSION, [{"role": "human", "content": "primeiro"}])
    second = _save(tmp_path, HEADLESS_SESSION, [{"role": "human", "content": "segundo"}])

    assert len({storage.history_file for storage in (chat, first, second)}) == 3
    for storage, content in ((chat, "conversa"), (first, "primeiro"), (second, "segundo")):
        payload = json.loads(storage.history_file.read_text(encoding="utf-8"))
        assert payload["mode"] == storage.mode
        assert payload["messages"] == [{"role": "human", "content": content}]
    assert SessionStorage(tmp_path).load_last_session()["messages"] == [
        {"role": "human", "content": "conversa"}
    ]


def test_unknown_session_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SessionStorage(tmp_path, mode="batch")


# ── Broker sem humano ──────────────────────────────────────────────────────


def _unattended_broker(approval: str):
    out = io.StringIO()
    from quimera.ui import PlainTextRenderer

    renderer = PlainTextRenderer(stream=out)
    gate = UnattendedInputGate(renderer=renderer, approval=approval)  # type: ignore[arg-type]
    return InputBroker(renderer=renderer, input_gate=gate), out


def test_unattended_deny_refuses_approval_at_once() -> None:
    broker, out = _unattended_broker("deny")
    started = time.monotonic()
    assert broker.request_approval("write_file", "x.txt", timeout=30) is False
    assert time.monotonic() - started < 5
    assert "execução não interativa — negado automaticamente" in out.getvalue()


def test_unattended_auto_grants_approval() -> None:
    broker, out = _unattended_broker("auto")
    assert broker.request_approval("write_file", "x.txt", timeout=30) is True
    assert "execução não interativa — aprovado automaticamente" in out.getvalue()


@pytest.mark.parametrize("approval", ["deny", "auto"])
def test_unattended_ask_user_fails_at_once_without_choosing(approval: str) -> None:
    broker, out = _unattended_broker(approval)
    started = time.monotonic()
    with pytest.raises(AskUserUnanswered, match="execução não interativa"):
        broker.request_ask_user("Qual?", ["Python", "Rust"], timeout=30)
    assert time.monotonic() - started < 5
    assert "pergunta sem resposta" in out.getvalue()


def test_unattended_gate_rejects_unknown_policy() -> None:
    with pytest.raises(ValueError):
        UnattendedInputGate(approval="ask")  # type: ignore[arg-type]


def test_ask_user_timeout_raises_instead_of_picking_the_first_option() -> None:
    from quimera.app.simple_input_gate import SimpleInputGate

    gate = SimpleInputGate(stdin=io.StringIO(""))
    broker = InputBroker(input_gate=gate)
    with pytest.raises(AskUserUnanswered, match="não respondeu"):
        broker.request_ask_user("Qual?", ["Python", "Rust"], timeout=0.3)
    with pytest.raises(AskUserUnanswered):
        broker.request_ask_user("Texto livre?", [], timeout=0.3)


def test_ask_user_tool_reports_unanswered_question_as_error() -> None:
    tools = InteractionTools(MagicMock(allow_ask_user=True))

    def _no_answer(question, options):
        raise AskUserUnanswered("o usuário não respondeu em 180s")

    tools.set_ask_user_fn(_no_answer)
    result = tools.ask_user(
        ToolCall(name="ask_user", arguments={"question": "Qual?", "options": ["a", "b"]})
    )
    assert result.ok is False
    assert "o usuário não respondeu em 180s" in str(result.error)
    assert "não presuma" in str(result.error)
    assert result.data == {"unanswered": True}


# ── Runner ─────────────────────────────────────────────────────────────────


class _FakeApp:
    """O mínimo do QuimeraApp que o runner usa."""

    def __init__(self, behaviours: dict) -> None:
        self.behaviours = behaviours
        self.calls: list[tuple[str, dict]] = []
        self.persisted: list[tuple[str, str]] = []
        self.closed: list[dict] = []
        self.cancelled = threading.Event()
        self.receipts: dict[str, dict] = {}
        self.storage = SimpleNamespace(session_id="sessao-teste")
        self.renderer = MagicMock()
        self.session_state_mgr = MagicMock()
        self.agent_client = SimpleNamespace(cancel_active_work=self.cancelled.set)
        self.process_supervisor = MagicMock()
        self.session_services = SimpleNamespace(persist_message=self._persist)
        self.dispatch_services = SimpleNamespace(
            delegate=self._delegate,
            consume_last_run_failure=self._consume,
        )
        self._last_agent = ""

    def _persist(self, role, content, *, return_history_snapshot=False):
        self.persisted.append((role, content))
        return [{"role": role, "content": content}] if return_history_snapshot else None

    def _delegate(self, agent, **options):
        self.calls.append((agent, options))
        self._last_agent = agent
        behaviour = self.behaviours[agent]
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour(self) if callable(behaviour) else behaviour

    def _consume(self):
        return self.receipts.pop(self._last_agent, None)

    @staticmethod
    def parse_response(response):
        return response, None, None, None

    def close(self, **kwargs):
        self.closed.append(kwargs)


def test_runner_falls_back_in_order_and_reports_each_attempt() -> None:
    app = _FakeApp({"a": None, "b": RuntimeError("sem créditos"), "c": "resposta de c", "d": "nunca"})
    app.receipts["a"] = {"error_kind": "process_died", "message": "código 3", "exit_code": 3}

    result = run_headless_prompt(app, "revise X", ["a", "b", "c", "d"])

    assert result.ok is True
    assert result.exit_code == EXIT_OK
    assert (result.agent, result.response) == ("c", "resposta de c")
    assert [call[0] for call in app.calls] == ["a", "b", "c"]
    assert [(a.agent, a.ok) for a in result.attempts] == [("a", False), ("b", False), ("c", True)]
    assert "process_died" in str(result.attempts[0].error) and "exit=3" in str(result.attempts[0].error)
    assert result.attempts[1].error == "RuntimeError: sem créditos"
    assert result.attempts[2].error is None
    # Mesmo turno do chat: prompt de primeiro falante; só o primeiro é o primário.
    assert all(call[1]["is_first_speaker"] for call in app.calls)
    assert [call[1]["primary"] for call in app.calls] == [True, False, False]
    assert app.calls[0][1]["request_override"] == "revise X"
    assert app.persisted == [("human", "revise X"), ("c", "resposta de c")]
    assert app.closed == [{"interrupted": False, "summarize_session": False}]
    assert result.session_id == "sessao-teste"


def test_runner_treats_blank_response_as_failure() -> None:
    app = _FakeApp({"a": "   \n", "b": "ok"})
    result = run_headless_prompt(app, "oi", ["a", "b"])
    assert result.agent == "b"
    assert "empty response" in str(result.attempts[0].error)


def test_runner_reports_when_every_candidate_fails() -> None:
    app = _FakeApp({"a": None, "b": None})
    result = run_headless_prompt(app, "oi", ["a", "b"])
    assert result.ok is False
    assert result.exit_code == EXIT_ALL_FAILED
    assert result.error == "nenhum agente candidato respondeu"
    assert [a.error for a in result.attempts] == [
        "Agent 'a' returned no response",
        "Agent 'b' returned no response",
    ]
    assert app.persisted == [("human", "oi")]


def test_runner_timeout_cancels_the_running_agent() -> None:
    def _hang(app):
        app.cancelled.wait(10)
        return "tarde demais"

    app = _FakeApp({"a": _hang, "b": "não chega"})
    started = time.monotonic()
    result = run_headless_prompt(app, "oi", ["a", "b"], timeout=0.3)

    assert time.monotonic() - started < 5
    assert result.exit_code == EXIT_TIMEOUT
    assert result.ok is False
    assert "tempo limite" in str(result.error)
    assert [(a.agent, a.ok) for a in result.attempts] == [("a", False)]
    assert app.cancelled.is_set()
    app.process_supervisor.terminate_all.assert_called_once()
    # A resposta que chega depois do cancelamento não vale nem é gravada.
    assert [call[0] for call in app.calls] == ["a"]
    assert app.persisted == [("human", "oi")]
    assert app.closed == [{"interrupted": False, "summarize_session": False}]


def test_runner_requires_candidates() -> None:
    with pytest.raises(ValueError):
        run_headless_prompt(_FakeApp({}), "oi", [])


# ── CLI ────────────────────────────────────────────────────────────────────


def _parse(monkeypatch, *argv: str):
    monkeypatch.setattr(sys, "argv", ["quimera", *argv])
    return cli._parse_args(cli._build_parser())


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--output", "json"], "--output exige --prompt"),
        (["--timeout", "5"], "--timeout exige --prompt"),
        (["--approve", "auto"], "--approve exige --prompt"),
        (["--driver-repl", "x", "--prompt", "oi", "--output", "json"], "não vale com --driver-repl"),
        (["--interactive-test", "--prompt", "oi"], "não combina com --interactive-test"),
        (["--prompt", "oi", "--name", "Alex"], "não combina com --name"),
        (["--prompt", "oi", "--list-connections"], "não combina com --list-connections"),
        (["--prompt", "oi", "--timeout", "0"], "--timeout deve ser um número de segundos maior que zero"),
    ],
)
def test_cli_rejects_combinations_that_would_be_ignored(monkeypatch, capsys, argv, message) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(monkeypatch, *argv)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_accepts_headless_flags(monkeypatch) -> None:
    args = _parse(
        monkeypatch, "--agents", "a", "b", "--prompt", "oi", "--output", "json",
        "--timeout", "30", "--approve", "auto",
    )
    assert (args.repl_prompt, args.output, args.timeout, args.approve) == ("oi", "json", 30.0, "auto")


def test_prompt_without_agents_or_saved_selection_is_an_error(monkeypatch, capsys) -> None:
    args = _parse(monkeypatch, "--prompt", "oi")
    config = SimpleNamespace(selected_agents=[])
    with pytest.raises(SystemExit):
        cli._select_agents(cli._build_parser(), args, config, ["a", "b"])  # type: ignore[arg-type]
    assert "--prompt exige --agents" in capsys.readouterr().err


def test_prompt_uses_the_saved_selection_in_order(monkeypatch) -> None:
    args = _parse(monkeypatch, "--prompt", "oi")
    config = SimpleNamespace(selected_agents=["b", "sumiu", "a"])
    assert cli._select_agents(cli._build_parser(), args, config, ["a", "b"]) == ["b", "a"]  # type: ignore[arg-type]


def test_interactive_session_still_falls_back_to_every_agent(monkeypatch) -> None:
    args = _parse(monkeypatch)
    config = SimpleNamespace(selected_agents=[])
    assert cli._select_agents(cli._build_parser(), args, config, ["a", "b"]) == ["a", "b"]  # type: ignore[arg-type]


def test_prompt_dash_reads_stdin(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("  linha 1\nlinha 2\n"))
    assert cli._read_headless_prompt(cli._build_parser(), "-") == "linha 1\nlinha 2"


def test_blank_prompt_is_an_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n \n"))
    with pytest.raises(SystemExit):
        cli._read_headless_prompt(cli._build_parser(), "-")
    assert "--prompt vazio" in capsys.readouterr().err


def _result(ok: bool) -> HeadlessResult:
    attempts = [HeadlessAttempt("a", False, "Agent 'a' returned no response", 10)]
    if ok:
        attempts.append(HeadlessAttempt("b", True, None, 20))
        return HeadlessResult(True, "b", "resposta", attempts, "s", None, EXIT_OK)
    return HeadlessResult(False, None, None, attempts, "s", "nenhum agente candidato respondeu", EXIT_ALL_FAILED)


def test_text_output_is_only_the_response() -> None:
    out, err = io.StringIO(), io.StringIO()
    cli._write_headless_result(_result(True), "text", out, err)
    assert out.getvalue() == "resposta\n"
    assert err.getvalue() == ""


def test_text_failure_goes_to_stderr_with_each_attempt() -> None:
    out, err = io.StringIO(), io.StringIO()
    cli._write_headless_result(_result(False), "text", out, err)
    assert out.getvalue() == ""
    assert err.getvalue() == (
        "quimera: nenhum agente candidato respondeu\n  a: Agent 'a' returned no response\n"
    )


def test_json_output_carries_every_attempt() -> None:
    out, err = io.StringIO(), io.StringIO()
    cli._write_headless_result(_result(True), "json", out, err)
    payload = json.loads(out.getvalue())
    assert payload == {
        "ok": True,
        "agent": "b",
        "response": "resposta",
        "attempts": [
            {"agent": "a", "ok": False, "error": "Agent 'a' returned no response", "duration_ms": 10},
            {"agent": "b", "ok": True, "error": None, "duration_ms": 20},
        ],
        "session_id": "s",
        "error": None,
        "exit_code": 0,
    }


# ── Ponta a ponta com os agentes fake ──────────────────────────────────────


def _run_quimera(tmp_path: Path, *argv: str, env_extra: dict | None = None, stdin: str | None = None):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    home.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            # HOME isolado: config padrão (preset strict) e sessões só do teste.
            "HOME": str(home),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "QUIMERA_FAKE_API_KEY": "test",
        }
    )
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-m", "quimera.cli", "--test", *argv],
        cwd=workspace,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    return proc, workspace, home


def test_e2e_prints_only_the_final_answer(tmp_path: Path) -> None:
    proc, _workspace, home = _run_quimera(tmp_path, "--agents", "fake-cli", "--prompt", "diga oi")

    assert proc.returncode == 0, proc.stderr
    # Só a resposta do agente: nem banner, nem eco do prompt, nem progresso.
    # Nem no stderr: num terminal ele aparece misturado à resposta.
    assert proc.stderr == ""
    lines = proc.stdout.splitlines()
    assert len(lines) == 3, proc.stdout
    assert lines[0] == "[quimera-fake-cli] role=tester model=fake-cli"
    assert lines[1].startswith("Prompt recebido (")
    assert lines[2] == (
        "Teste fake: cenário executado, resultado esperado observado e evidência textual emitida."
    )
    sessions = list(home.rglob("sessao-*.json"))
    assert len(sessions) == 1
    payload = json.loads(sessions[0].read_text(encoding="utf-8"))
    assert payload["mode"] == HEADLESS_SESSION
    assert [message["role"] for message in payload["messages"]] == ["human", "fake-cli"]


def test_e2e_falls_back_to_the_next_candidate(tmp_path: Path) -> None:
    proc, _workspace, _home = _run_quimera(
        tmp_path,
        "--agents", "fake-cli", "fake-openai", "--output", "json", "--prompt", "diga oi",
        env_extra={"QUIMERA_FAKE_CLI_FAIL": "1"},
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["agent"] == "fake-openai"
    assert payload["response"].startswith("Fake OpenAI respondeu")
    assert [(a["agent"], a["ok"]) for a in payload["attempts"]] == [
        ("fake-cli", False),
        ("fake-openai", True),
    ]
    assert "exit=3" in payload["attempts"][0]["error"]


def test_e2e_fallback_in_text_mode_prints_only_the_answer(tmp_path: Path) -> None:
    proc, _workspace, _home = _run_quimera(
        tmp_path,
        "--agents", "fake-cli", "fake-openai", "--prompt", "diga oi",
        env_extra={"QUIMERA_FAKE_CLI_FAIL": "1"},
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("Fake OpenAI respondeu")
    # Houve resposta: a falha do fake-cli fica em ``attempts`` (--output json).
    assert proc.stderr == ""


def test_e2e_all_candidates_failing_exits_1(tmp_path: Path) -> None:
    proc, _workspace, _home = _run_quimera(
        tmp_path, "--agents", "fake-cli", "--prompt", "diga oi",
        env_extra={"QUIMERA_FAKE_CLI_FAIL": "1"},
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    # Só o resumo final: o progresso e os avisos do renderer não vazam.
    lines = proc.stderr.splitlines()
    assert lines[0] == "quimera: nenhum agente candidato respondeu"
    assert len(lines) == 2, proc.stderr
    assert lines[1].startswith("  fake-cli: Agent 'fake-cli' failed (process_died)")


@pytest.mark.parametrize(("approval", "created"), [("deny", False), ("auto", True)])
def test_e2e_approval_policy_without_a_human(tmp_path: Path, approval: str, created: bool) -> None:
    proc, workspace, _home = _run_quimera(
        tmp_path,
        "--agents", "fake-openai", "--approve", approval, "--prompt", "escreva arquivo probe",
    )

    assert proc.returncode == 0, proc.stderr
    assert (workspace / "quimera_fake_probe.txt").exists() is created
    # A decisão da política aparece na resposta do agente, não como artefato.
    assert proc.stderr == ""
    assert ("[OK]" if created else "Execução não aprovada") in proc.stdout


def test_e2e_ask_user_fails_without_waiting(tmp_path: Path) -> None:
    started = time.monotonic()
    proc, _workspace, _home = _run_quimera(
        tmp_path, "--agents", "fake-openai", "--prompt", "faça uma enquete",
    )

    assert proc.returncode == 0, proc.stderr
    assert "Pergunta sem resposta: execução não interativa" in proc.stdout
    # Bem abaixo dos 180 s que um humano ausente levaria para expirar.
    assert time.monotonic() - started < 120


def test_e2e_timeout_exits_124(tmp_path: Path) -> None:
    proc, _workspace, _home = _run_quimera(
        tmp_path,
        "--agents", "fake-cli", "--timeout", "3", "--output", "json", "--prompt", "oi",
        env_extra={"QUIMERA_FAKE_CLI_SLEEP": "60"},
    )

    assert proc.returncode == EXIT_TIMEOUT, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == EXIT_TIMEOUT
    assert payload["error"] == "tempo limite de 3s esgotado"
    assert [(a["agent"], a["ok"]) for a in payload["attempts"]] == [("fake-cli", False)]


def test_e2e_prompt_from_stdin(tmp_path: Path) -> None:
    proc, _workspace, _home = _run_quimera(
        tmp_path, "--agents", "fake-cli", "--prompt", "-", stdin="texto vindo do stdin\n",
    )

    assert proc.returncode == 0, proc.stderr
    assert "Teste fake: cenário executado" in proc.stdout


# ── Casos de borda da revisão ──────────────────────────────────────────────


@pytest.mark.parametrize("approval", ["deny", "auto"])
def test_approval_without_broker_follows_the_unattended_policy(tmp_path: Path, approval: str) -> None:
    """Executores de background montam o ApprovalManager sem broker
    (``TaskExecutorPool.build_tool_executor``): a política vale igual."""
    from quimera.runtime.approval import ApprovalManager
    from quimera.runtime.config import ToolRuntimeConfig
    from quimera.workspace import Workspace

    config = ToolRuntimeConfig(workspace=Workspace(tmp_path))
    out = io.StringIO()
    from quimera.ui import PlainTextRenderer

    renderer = PlainTextRenderer(stream=out)
    manager = ApprovalManager(
        config,
        renderer=renderer,
        input_gate=UnattendedInputGate(renderer=renderer, approval=approval),  # type: ignore[arg-type]
    )
    result = {}
    worker = threading.Thread(target=lambda: result.update(ok=manager.approve(tool_name="write_file", summary="x")))
    worker.start()
    worker.join(timeout=10)

    assert result["ok"] is (approval == "auto")
    outcome = "aprovado" if approval == "auto" else "negado"
    assert f"execução não interativa — {outcome} automaticamente" in out.getvalue()


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e300", "-1"])
def test_cli_rejects_non_finite_or_huge_timeout(monkeypatch, capsys, value: str) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(monkeypatch, "--agents", "a", "--prompt", "oi", f"--timeout={value}")
    assert exc.value.code == 2
    assert "--timeout deve ser um número de segundos maior que zero" in capsys.readouterr().err


def test_legacy_snapshot_without_mode_is_still_restored(tmp_path: Path) -> None:
    day = tmp_path / "2026-11-01"
    day.mkdir()
    (day / "sessao-2026-11-01-120000.json").write_text(
        json.dumps(
            {
                "session_id": "sessao-2026-11-01-120000",
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "messages": [{"role": "human", "content": "antiga"}],
                "shared_state": {},
            }
        ),
        encoding="utf-8",
    )
    restored = SessionStorage(tmp_path).load_last_session()
    assert restored["messages"] == [{"role": "human", "content": "antiga"}]


def test_runner_reports_a_crash_of_the_turn() -> None:
    app = _FakeApp({"a": "ok"})

    def _broken_persist(role, content, *, return_history_snapshot=False):
        raise OSError("disco cheio")

    app.session_services = SimpleNamespace(persist_message=_broken_persist)
    result = run_headless_prompt(app, "oi", ["a"])

    assert result.ok is False
    assert result.exit_code == EXIT_ALL_FAILED
    assert result.error == "OSError: disco cheio"
    assert app.closed == [{"interrupted": False, "summarize_session": False}]


def test_timeout_between_candidates_does_not_start_the_next_one() -> None:
    def _ignores_cancel(app):
        time.sleep(0.6)  # não obedece ao cancelamento e falha depois do corte
        return None

    app = _FakeApp({"a": _ignores_cancel, "b": "não pode começar"})
    result = run_headless_prompt(app, "oi", ["a", "b"], timeout=0.2)

    assert result.exit_code == EXIT_TIMEOUT
    assert [call[0] for call in app.calls] == ["a"]
    assert [(a.agent, a.ok) for a in result.attempts] == [("a", False)]


def test_answer_accepted_before_the_timeout_wins() -> None:
    app = _FakeApp({"a": "resposta"})
    persisted = []

    def _slow_persist(role, content, *, return_history_snapshot=False):
        if role == "a":
            time.sleep(0.6)  # a gravação passa do prazo; a resposta já foi aceita
        persisted.append((role, content))
        return [{"role": role, "content": content}] if return_history_snapshot else None

    app.session_services = SimpleNamespace(persist_message=_slow_persist)
    result = run_headless_prompt(app, "oi", ["a"], timeout=0.3)

    assert result.ok is True
    assert result.exit_code == EXIT_OK
    assert result.response == "resposta"
    assert persisted == [("human", "oi"), ("a", "resposta")]
    assert not app.cancelled.is_set()


@pytest.mark.parametrize(
    ("signum", "exit_code", "reason"),
    [
        (__import__("signal").SIGINT, 130, "interrompido (Ctrl+C)"),
        (__import__("signal").SIGTERM, 143, "encerrado (SIGTERM)"),
    ],
)
def test_signal_cancels_the_agent_and_closes_the_app(signum, exit_code: int, reason: str) -> None:
    import signal as signal_module

    def _hang(app):
        app.cancelled.wait(10)
        return None

    app = _FakeApp({"a": _hang, "b": "não chega"})
    previous_sigterm = signal_module.getsignal(signal_module.SIGTERM)
    timer = threading.Timer(0.3, os.kill, args=(os.getpid(), signum))
    timer.start()
    try:
        result = run_headless_prompt(app, "oi", ["a", "b"])
    finally:
        timer.cancel()

    assert result.exit_code == exit_code
    assert result.error == reason
    assert [(a.agent, a.ok, a.error) for a in result.attempts] == [("a", False, reason)]
    assert app.cancelled.is_set()
    assert app.closed == [{"interrupted": True, "summarize_session": False}]
    # O handler de SIGTERM do processo é restaurado.
    assert signal_module.getsignal(signal_module.SIGTERM) == previous_sigterm

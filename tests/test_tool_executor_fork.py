"""Isolamento do ToolExecutor em forks concorrentes de AgentClient/dispatch."""
import threading
from pathlib import Path

import pytest

from quimera.agents.client import AgentClient
from quimera.runtime.approval import (
    ApprovalManager,
    AutoApprovalHandler,
    ConsoleApprovalHandler,
)
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor, ToolExecutorWiring
from quimera.runtime.models import ToolCall


class _RecordingBroker:
    """InputBroker mínimo que registra os callbacks de spinner recebidos."""

    def __init__(self):
        """Inicializa uma instância de _RecordingBroker."""
        self.spinner_calls = []

    def set_spinner_callbacks(self, suspend_fn, resume_fn):
        self.spinner_calls.append((suspend_fn, resume_fn))


@pytest.fixture
def workspace(tmp_path):
    """Raiz de workspace isolada para os executores do teste."""
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _build_executor(workspace_root, *, handler=None) -> ToolExecutor:
    """Cria um ToolExecutor auto-aprovado apontando para o workspace do teste."""
    config = ToolRuntimeConfig(
        workspace_root=Path(workspace_root),
        require_approval_for_mutations=False,
    )
    return ToolExecutor(config, handler if handler is not None else AutoApprovalHandler())


# ── Isolamento estrutural ──────────────────────────────────────────────


def test_fork_creates_independent_runtime_state(workspace):
    """Fork não compartilha registry, policy, tools nem manager de aprovação."""
    primary = _build_executor(workspace)

    forked = primary.fork_for_concurrent_run()

    assert forked is not primary
    assert forked.config is primary.config
    assert forked.registry is not primary.registry
    assert forked.policy is not primary.policy
    assert forked.approval_manager is not primary.approval_manager
    assert forked._delegate_tools is not primary._delegate_tools
    assert forked._interaction_tools is not primary._interaction_tools
    assert forked._state_tools is not primary._state_tools
    assert forked._task_tools is not primary._task_tools


def test_fork_preserves_injected_wiring(workspace):
    """Todas as injeções nomeadas são reaplicadas no executor isolado."""
    primary = _build_executor(workspace)
    delegate_fn = lambda agent, **options: f"resposta:{agent}"  # noqa: E731
    ask_user_fn = lambda question, options: (0, options[0])  # noqa: E731
    update_state_fn = lambda payload: True  # noqa: E731
    cancel_checker = lambda: False  # noqa: E731
    primary.set_delegate_fn(delegate_fn)
    primary.set_background_delegate_fn(delegate_fn)
    primary.set_ask_user_fn(ask_user_fn)
    primary.set_update_state_fn(update_state_fn)
    primary.set_cancel_checker(cancel_checker)
    primary.set_active_agents_provider(lambda: ["codex"])
    primary.set_orchestrator_provider(lambda: "claude")

    forked = primary.fork_for_concurrent_run()

    assert forked.is_delegate_available() is True
    assert forked.is_ask_user_available() is True
    assert forked.is_update_state_available() is True
    assert forked._wiring.delegate_fn is delegate_fn
    assert forked._wiring.cancel_checker is cancel_checker
    assert forked._wiring.ask_user_fn is ask_user_fn


def test_fork_preserves_blocked_tools_from_execution_mode(workspace):
    """O modo de execução ativo (blocked_tools) acompanha o fork."""
    primary = _build_executor(workspace)
    primary.policy.blocked_tools = ["write_file", "apply_patch"]

    forked = primary.fork_for_concurrent_run()

    assert forked.policy.blocked_tools == ["write_file", "apply_patch"]
    forked.policy.blocked_tools.append("run_shell")
    assert primary.policy.blocked_tools == ["write_file", "apply_patch"]


def test_fork_preserves_allowed_tools_from_execution_mode(workspace):
    primary = _build_executor(workspace)
    primary.policy.allowed_tools = ["read_file", "grep_search"]

    forked = primary.fork_for_concurrent_run()

    assert forked.policy.allowed_tools == ["read_file", "grep_search"]
    forked.policy.allowed_tools.append("list_files")
    assert primary.policy.allowed_tools == ["read_file", "grep_search"]


def test_wiring_apply_to_skips_unset_injections(workspace):
    """Injeções não configuradas não são reaplicadas no destino."""
    target = _build_executor(workspace)

    ToolExecutorWiring(delegate_fn=lambda agent, **options: "ok").apply_to(target)

    assert target.is_delegate_available() is True
    assert target.is_ask_user_available() is False
    assert target.is_update_state_available() is False


# ── Isolamento de aprovação / cancelamento ─────────────────────────────


def test_fork_does_not_clear_primary_approval_cancel_event(workspace):
    """Fim de um fork não desliga o cancelamento de aprovação do chat."""
    console = ConsoleApprovalHandler()
    primary = _build_executor(workspace, handler=ApprovalManager(
        ToolRuntimeConfig(workspace_root=Path(workspace)),
        base_handler=console,
    ))
    primary_cancel = threading.Event()
    primary.set_approval_cancel_event(primary_cancel)

    forked = primary.fork_for_concurrent_run()
    forked.set_approval_cancel_event(threading.Event())
    # Reproduz o finally de AgentClient._run_api_agent ao encerrar o fork.
    forked.set_approval_cancel_event(None)
    forked.set_spinner_callbacks(None, None)

    assert console._cancel_event is primary_cancel


def test_fork_keeps_shared_input_broker_and_interactive_lock(workspace):
    """Serialização do terminal continua compartilhada entre os forks."""
    broker = _RecordingBroker()
    console = ConsoleApprovalHandler()
    console.set_input_broker(broker)

    forked_console = console.fork_for_concurrent_run()

    assert forked_console is not console
    assert forked_console.input_broker is broker
    assert forked_console._interactive_lock is console._interactive_lock
    # Um handler sem spinner próprio não sobrescreve o do broker.
    assert broker.spinner_calls == []


def test_fork_approve_all_does_not_leak_to_primary(workspace):
    """Approve-all consumido em um fork não afeta o ciclo do chat."""
    primary = _build_executor(workspace)

    forked = primary.fork_for_concurrent_run()
    forked.approval_manager.set_approve_all(True)

    assert forked.approval_manager._pre_handler._approve_all is True
    assert primary.approval_manager._pre_handler._approve_all is False


# ── Concorrência real ──────────────────────────────────────────────────


def test_concurrent_forks_keep_progress_callbacks_isolated(workspace):
    """Execuções paralelas não trocam o callback de progresso entre si."""
    primary = _build_executor(workspace)
    workers = 8
    ready = threading.Barrier(workers)
    observed: dict[int, object] = {}
    errors: list[BaseException] = []

    def _run(index: int) -> None:
        try:
            forked = primary.fork_for_concurrent_run()
            callback = lambda message, _index=index: None  # noqa: E731
            forked.set_tool_progress_callback(callback)
            ready.wait(timeout=10)
            result = forked.execute(
                ToolCall(name="run_shell", arguments={"command": f"echo agente-{index}"}),
                progress_callback=callback,
            )
            assert result.ok, result.error
            assert f"agente-{index}" in result.content
            observed[index] = forked._delegate_tools._progress_callback
        except BaseException as exc:  # noqa: BLE001 - propagado após o join
            errors.append(exc)

    threads = [threading.Thread(target=_run, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert len(observed) == workers
    assert len({id(callback) for callback in observed.values()}) == workers
    assert primary._tool_progress_callback is None
    assert primary._delegate_tools._progress_callback is None


def test_concurrent_agent_client_forks_get_distinct_executors(workspace, tmp_path):
    """Cada fork de AgentClient recebe seu próprio ToolExecutor."""

    class _NullRenderer:
        supports_agent_feed = False

        def show_error(self, message, **kwargs):
            pass

        def show_system_neutral(self, message):
            pass

    client = AgentClient(_NullRenderer(), working_dir=str(workspace))
    client.tool_executor = _build_executor(workspace)
    forks: list[AgentClient] = []
    lock = threading.Lock()
    ready = threading.Barrier(6)

    def _fork() -> None:
        ready.wait(timeout=10)
        forked = client.fork_for_concurrent_run()
        with lock:
            forks.append(forked)

    threads = [threading.Thread(target=_fork) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(forks) == 6
    executors = [forked.tool_executor for forked in forks]
    assert all(executor is not None for executor in executors)
    assert len({id(executor) for executor in executors}) == 6
    assert client.tool_executor not in executors
    # Cancelamento continua compartilhado com o client do chat.
    assert all(forked.cancel_event is client.cancel_event for forked in forks)


def test_client_refuses_fork_when_executor_cannot_isolate(workspace):
    """Sem executor isolável o fork é recusado em vez de compartilhar estado."""

    class _NullRenderer:
        supports_agent_feed = False

    class _OpaqueExecutor:
        """Integração customizada sem suporte a isolamento por fork."""

    client = AgentClient(_NullRenderer(), working_dir=str(workspace))
    client.tool_executor = _OpaqueExecutor()

    assert client.fork_for_concurrent_run() is None


def test_client_without_executor_still_forks(workspace):
    """Client sem ToolExecutor injetado continua forkável."""

    class _NullRenderer:
        supports_agent_feed = False

    client = AgentClient(_NullRenderer(), working_dir=str(workspace))

    forked = client.fork_for_concurrent_run()

    assert forked is not None
    assert forked.tool_executor is None

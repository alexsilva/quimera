import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quimera.app import QuimeraApp
from quimera.app.staging import merge_staging_to_workspace
from quimera.app.protocol import AppProtocol
from quimera.app.session import AppSessionServices, compute_history_hard_limit
from quimera.domain.session_state import SessionRuntimeState
from quimera.shared_state import clear_agent_state_for_session_start
from quimera.ui.base import RendererBase


class _Storage:
    def __init__(self):
        self.saved_history = None
        self.saved_shared_state = None
        self.logs = []

    def append_log(self, role, content):
        self.logs.append((role, content))

    def save_history(self, history, shared_state=None):
        self.saved_history = list(history)
        self.saved_shared_state = dict(shared_state or {})


class _Renderer(RendererBase):
    def __init__(self):
        self.system_messages = []
        self.notifications = []

    def show_system(self, message):
        self.system_messages.append(message)

    def show_notification(self, message, *, severity="information", timeout=None):
        self.notifications.append((message, severity, timeout))


class _ContextManager:
    def __init__(self):
        self.saved_summary = None

    def load_session_summary(self):
        return None

    def update_with_summary(self, summary):
        self.saved_summary = summary


def test_parse_response_preserves_shared_state_lock():
    """Verifica que parse response preserves shared state lock."""
    app = QuimeraApp.__new__(QuimeraApp)
    app.shared_state = {}
    app._lock = threading.Lock()
    app._shared_state_lock = threading.Lock()
    app._turn_stamps = {}
    app.protocol = AppProtocol(
        lock=app._shared_state_lock,
        shared_state=app.shared_state,
        turn_stamps=app._turn_stamps,
    )

    app.protocol.apply_state_update({"goal": "corrigir lock"})

    assert app.protocol._lock is app._shared_state_lock
    assert app.protocol._lock is not app._lock
    assert app.shared_state["goal"] == "corrigir lock"


def test_delegation_json_is_plain_content_not_protocol_envelope():
    """Verifica que delegation json is plain content not protocol envelope."""
    proto = AppProtocol(lock=threading.Lock(), shared_state={})

    response, target, delegation, extend, ack_id = proto.parse_response(
        '{"type":"delegation","route":"codex","content":"delegar textual"}'
    )

    assert response == '{"type":"delegation","route":"codex","content":"delegar textual"}'
    assert target is None
    assert delegation is None
    assert extend is False
    assert ack_id is None


def _make_merge_app(workspace_root: Path, state_dir: Path | None = None):
    app = QuimeraApp.__new__(QuimeraApp)
    app.workspace = SimpleNamespace(cwd=workspace_root, state_dir=state_dir or workspace_root / ".state")
    return app


def test_merge_staging_to_workspace_normal_path_writes_manifest(tmp_path):
    """Verifica que merge staging to workspace normal path writes manifest."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    staging = tmp_path / "staging"
    src = staging / "0" / "dir" / "file.txt"
    src.parent.mkdir(parents=True)
    src.write_text("novo", encoding="utf-8")
    app = _make_merge_app(workspace)

    manifest = merge_staging_to_workspace(staging, app.workspace)

    assert (workspace / "dir" / "file.txt").read_text(encoding="utf-8") == "novo"
    assert manifest[0]["relative_path"] == "dir/file.txt"
    assert manifest[0]["overwritten"] is False
    manifest_log = app.workspace.state_dir / "staging_merge_manifest.jsonl"
    assert '"relative_path": "dir/file.txt"' in manifest_log.read_text(encoding="utf-8")


def test_merge_staging_to_workspace_blocks_destination_outside_workspace(tmp_path):
    """Verifica que merge staging to workspace blocks destination outside workspace."""
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    staging = tmp_path / "staging"
    src = staging / "0" / "linked" / "escape.txt"
    src.parent.mkdir(parents=True)
    src.write_text("escape", encoding="utf-8")
    app = _make_merge_app(workspace)

    with pytest.raises(ValueError, match="outside workspace"):
        merge_staging_to_workspace(staging, app.workspace)
    assert not (outside / "escape.txt").exists()


def test_merge_staging_to_workspace_blocks_source_symlink(tmp_path):
    """Verifica que merge staging to workspace blocks source symlink."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    staging = tmp_path / "staging"
    index = staging / "0"
    index.mkdir(parents=True)
    real = tmp_path / "real.txt"
    real.write_text("real", encoding="utf-8")
    (index / "link.txt").symlink_to(real)
    app = _make_merge_app(workspace)

    with pytest.raises(ValueError, match="symlink source"):
        merge_staging_to_workspace(staging, app.workspace)


def test_merge_staging_to_workspace_records_overwrite(tmp_path):
    """Verifica que merge staging to workspace records overwrite."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("antigo", encoding="utf-8")
    staging = tmp_path / "staging"
    src = staging / "0" / "file.txt"
    src.parent.mkdir(parents=True)
    src.write_text("novo", encoding="utf-8")
    app = _make_merge_app(workspace)

    manifest = merge_staging_to_workspace(staging, app.workspace)

    assert (workspace / "file.txt").read_text(encoding="utf-8") == "novo"
    assert manifest[0]["overwritten"] is True



def test_session_state_history_transactions_preserve_reference_and_return_snapshots():
    """Verifica que session state history transactions preserve reference and return snapshots."""
    history = [{"role": "human", "content": f"m{i}"} for i in range(4)]
    original_ref = history
    state = SessionRuntimeState.from_legacy(history=history, shared_state={})

    dropped, trimmed_snapshot = state.trim_history(2)

    assert dropped == 2
    assert history is original_ref
    assert trimmed_snapshot == [
        {"role": "human", "content": "m2"},
        {"role": "human", "content": "m3"},
    ]
    assert history == trimmed_snapshot
    assert trimmed_snapshot is not history

    dropped, append_snapshot = state.append_history_trimmed_and_snapshot(
        {"role": "assistant", "content": "m4"},
        2,
    )

    assert dropped == 1
    assert history is original_ref
    assert append_snapshot == [
        {"role": "human", "content": "m3"},
        {"role": "assistant", "content": "m4"},
    ]
    assert history == append_snapshot
    assert append_snapshot is not history

    matched, final_snapshot = state.replace_history_if_prefix_matches(
        append_snapshot,
        len(append_snapshot),
        [{"role": "system", "content": "summary"}],
    )

    assert matched is True
    assert history is original_ref
    assert final_snapshot == [{"role": "system", "content": "summary"}]
    assert history == final_snapshot
    assert final_snapshot is not history


def test_persist_message_returned_snapshot_is_atomic_with_append_and_trim():
    """Verifica que persist message returned snapshot is atomic with append and trim."""
    history = []
    state = SessionRuntimeState.from_legacy(history=history, shared_state={"goal": "atomic"})

    class InterleavingStorage(_Storage):
        def append_log(self, role, content):
            super().append_log(role, content)
            state.append_history({"role": "assistant", "content": "concurrent message"})

    storage = InterleavingStorage()
    service = AppSessionServices(
        session_state=state,
        storage=storage,
        renderer=_Renderer(),
        agent_pool=SimpleNamespace(primary="codex"),
        context_manager=_ContextManager(),
        session_summarizer=Mock(),
        task_services=Mock(stop_task_executors=Mock()),
        prompt_builder=SimpleNamespace(history_window=2),
    )

    snapshot = service.persist_message(
        "human",
        "message for this round",
        return_history_snapshot=True,
    )

    assert snapshot == [{"role": "human", "content": "message for this round"}]
    assert storage.saved_history == snapshot
    assert storage.saved_shared_state == {"goal": "atomic"}
    assert history == [
        {"role": "human", "content": "message for this round"},
        {"role": "assistant", "content": "concurrent message"},
    ]
    assert snapshot is not history


def test_compute_history_hard_limit_never_undercuts_summarize_threshold():
    """O teto nunca pode truncar o histórico antes do resumo automático disparar."""
    assert compute_history_hard_limit(6, 30) == 60
    assert compute_history_hard_limit(12, 4) == 24
    assert compute_history_hard_limit(6, None) == 12


def test_persist_message_trims_history_to_history_window_times_two():
    """Verifica que, sem resumo automático, o histórico não cresce além de 2x a janela."""
    history = [{"role": "human", "content": f"m{i}"} for i in range(12)]
    state = SessionRuntimeState.from_legacy(history=history, shared_state={})
    storage = _Storage()
    service = AppSessionServices(
        session_state=state,
        storage=storage,
        renderer=_Renderer(),
        agent_pool=SimpleNamespace(primary="codex"),
        context_manager=_ContextManager(),
        session_summarizer=Mock(),
        task_services=Mock(stop_task_executors=Mock()),
        prompt_builder=SimpleNamespace(history_window=6),
        auto_summarize_threshold=0,
    )

    snapshot = service.persist_message("assistant", "m12", return_history_snapshot=True)

    assert len(history) == 12
    assert history == [{"role": "human", "content": f"m{i}"} for i in range(1, 12)] + [
        {"role": "assistant", "content": "m12"}
    ]
    assert snapshot == history


def test_session_summarize_preserves_concurrent_persisted_message():
    """Verifica que session summarize preserves concurrent persisted message."""
    history = [{"role": "human", "content": f"m{i}"} for i in range(12)]
    storage = _Storage()
    renderer = _Renderer()
    context = _ContextManager()
    lock = threading.Lock()

    class Summarizer:
        def __init__(self):
            self.service = None

        def summarize(self, messages, existing_summary=None, preferred_agent=None, fallback=True):
            self.service.persist_message("assistant", "append during summary")
            return "resumo"

    summarizer = Summarizer()
    service = AppSessionServices(
        session_state=SessionRuntimeState.from_legacy(history=history, shared_state={}, shared_state_lock=lock),
        storage=storage,
        renderer=renderer,
        agent_pool=SimpleNamespace(primary="codex"),
        context_manager=context,
        session_summarizer=summarizer,
        task_services=Mock(stop_task_executors=Mock()),
        prompt_builder=SimpleNamespace(history_window=6),
        auto_summarize_threshold=8,
    )
    summarizer.service = service

    service.maybe_auto_summarize()
    service.join_summarization(timeout=3)

    assert history == [
        {"role": "human", "content": "m6"},
        {"role": "human", "content": "m7"},
        {"role": "human", "content": "m8"},
        {"role": "human", "content": "m9"},
        {"role": "human", "content": "m10"},
        {"role": "human", "content": "m11"},
        {"role": "assistant", "content": "append during summary"},
    ]
    assert len(history) == 7
    assert storage.saved_history == history



def test_session_summarize_does_not_save_summary_when_snapshot_changes():
    """Verifica que session summarize does not save summary when snapshot changes."""
    history = [{"role": "human", "content": f"m{i}"} for i in range(12)]
    original_length = len(history)
    storage = _Storage()
    renderer = _Renderer()
    context = _ContextManager()
    lock = threading.Lock()

    class Summarizer:
        def __init__(self):
            self.service = None

        def summarize(self, messages, existing_summary=None, preferred_agent=None, fallback=True):
            with self.service._lock:
                self.service._history[0] = {"role": "human", "content": "mutado durante resumo"}
            return "resumo inconsistente"

    summarizer = Summarizer()
    service = AppSessionServices(
        session_state=SessionRuntimeState.from_legacy(history=history, shared_state={}, shared_state_lock=lock),
        storage=storage,
        renderer=renderer,
        agent_pool=SimpleNamespace(primary="codex"),
        context_manager=context,
        session_summarizer=summarizer,
        task_services=Mock(stop_task_executors=Mock()),
        prompt_builder=SimpleNamespace(history_window=2),
        auto_summarize_threshold=4,
    )
    summarizer.service = service

    service.maybe_auto_summarize()
    service.join_summarization(timeout=3)

    assert context.saved_summary is None
    assert storage.saved_history is None
    assert len(history) == original_length
    assert history[0] == {"role": "human", "content": "mutado durante resumo"}
    assert service._pending_summary_completion == (
        "Resumo adiado; histórico mudou durante a geração."
    )


def test_session_shutdown_summarizes_stable_history_snapshot_after_pending_save():
    """Verifica que session shutdown summarizes stable history snapshot after pending save."""
    history = [{"role": "human", "content": "before shutdown"}]
    shared_state = {"goal": "persist me"}
    storage = _Storage()
    renderer = _Renderer()
    context = _ContextManager()

    class Summarizer:
        def __init__(self):
            self.service = None
            self.messages_seen = None
            self.fallback_seen = None

        def summarize(self, messages, existing_summary=None, preferred_agent=None, fallback=True):
            self.messages_seen = messages
            self.fallback_seen = fallback
            self.service.persist_message("assistant", "mutated during shutdown summary")
            return "shutdown summary"

    summarizer = Summarizer()
    service = AppSessionServices(
        session_state=SessionRuntimeState.from_legacy(history=history, shared_state=shared_state),
        storage=storage,
        renderer=renderer,
        agent_pool=SimpleNamespace(primary="codex"),
        context_manager=context,
        session_summarizer=summarizer,
        task_services=Mock(stop_task_executors=Mock()),
        prompt_builder=SimpleNamespace(history_window=2),
    )
    summarizer.service = service
    service._unsaved_messages = 1
    service._last_save_time = time.monotonic()

    service.shutdown()

    assert storage.saved_history == [{"role": "human", "content": "before shutdown"}]
    assert storage.saved_shared_state == {"goal": "persist me"}
    assert summarizer.messages_seen == [{"role": "human", "content": "before shutdown"}]
    assert summarizer.messages_seen is not history
    assert summarizer.fallback_seen is False
    assert history == [
        {"role": "human", "content": "before shutdown"},
        {"role": "assistant", "content": "mutated during shutdown summary"},
    ]
    assert context.saved_summary == "shutdown summary"

def test_restored_session_clears_volatile_agent_goal_state_only():
    """Verifica que restored session clears volatile agent goal state only."""
    shared_state = {
        "goal": "objetivo antigo",
        "goal_canonical": "objetivo-antigo",
        "current_step": "passo velho",
        "next_step": "proximo velho",
        "decisions": ["decisão útil"],
        "evidence": ["evidência útil"],
        "_current_turn": 7,
    }

    removed = clear_agent_state_for_session_start(shared_state, history_restored=True)

    assert set(removed) == {"goal", "goal_canonical", "current_step", "next_step"}
    assert "goal" not in shared_state
    assert "current_step" not in shared_state
    assert shared_state["decisions"] == ["decisão útil"]
    assert shared_state["evidence"] == ["evidência útil"]
    assert shared_state["_current_turn"] == 7


def test_current_job_env_restore_removes_session_value(monkeypatch):
    """Verifica que current job env restore removes session value."""
    monkeypatch.delenv("QUIMERA_CURRENT_JOB_ID", raising=False)
    app = QuimeraApp.__new__(QuimeraApp)
    app._previous_current_job_id_env = None
    os.environ["QUIMERA_CURRENT_JOB_ID"] = "123"

    app._restore_current_job_env()

    assert "QUIMERA_CURRENT_JOB_ID" not in os.environ


def test_current_job_env_restore_restores_previous_value(monkeypatch):
    """Verifica que current job env restore restores previous value."""
    monkeypatch.setenv("QUIMERA_CURRENT_JOB_ID", "old")
    app = QuimeraApp.__new__(QuimeraApp)
    app._previous_current_job_id_env = "old"
    os.environ["QUIMERA_CURRENT_JOB_ID"] = "new"

    app._restore_current_job_env()

    assert os.environ["QUIMERA_CURRENT_JOB_ID"] == "old"


def test_auto_summarize_calls_load_session_summary_inside_background_thread():
    """load_session_summary deve ser chamado na thread de background, não na thread principal."""
    history = [{"role": "human", "content": f"m{i}"} for i in range(12)]
    storage = _Storage()
    renderer = _Renderer()
    lock = threading.Lock()
    call_thread: list = []

    class TrackingContextManager:
        def load_session_summary(self):
            call_thread.append(threading.current_thread())
            return "existente"

        def update_with_summary(self, summary):
            pass

    class Summarizer:
        def summarize(self, messages, existing_summary=None, preferred_agent=None, fallback=True):
            return "resumo"

    service = AppSessionServices(
        session_state=SessionRuntimeState.from_legacy(history=history, shared_state={}, shared_state_lock=lock),
        storage=storage,
        renderer=renderer,
        agent_pool=SimpleNamespace(primary="codex"),
        context_manager=TrackingContextManager(),
        session_summarizer=Summarizer(),
        task_services=Mock(stop_task_executors=Mock()),
        prompt_builder=SimpleNamespace(history_window=2),
        auto_summarize_threshold=4,
    )

    service.maybe_auto_summarize()
    service.join_summarization(timeout=3)

    assert len(call_thread) == 1
    assert call_thread[0] is not threading.main_thread()


def test_shutdown_waits_for_running_summarization_thread_when_not_interrupted():
    """shutdown() normal deve aguardar a thread de sumarização automática."""
    class FakeThread:
        def __init__(self):
            self.join_calls = []

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.join_calls.append(timeout)

    storage = _Storage()
    renderer = _Renderer()
    lock = threading.Lock()

    service = AppSessionServices(
        session_state=SessionRuntimeState.from_legacy(history=[], shared_state={}, shared_state_lock=lock),
        storage=storage,
        renderer=renderer,
        agent_pool=SimpleNamespace(primary="codex"),
        context_manager=_ContextManager(),
        session_summarizer=Mock(),
        task_services=Mock(stop_task_executors=Mock()),
        prompt_builder=SimpleNamespace(history_window=2),
    )
    fake_thread = FakeThread()
    service._summarization_thread = fake_thread

    service.shutdown(interrupted=False)

    assert fake_thread.join_calls == [30]


def test_shutdown_interrupted_does_not_wait_for_running_summarization_thread():
    """shutdown(interrupted=True) não deve bloquear aguardando a sumarização automática."""
    class FakeThread:
        def __init__(self):
            self.join_calls = []

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.join_calls.append(timeout)

    storage = _Storage()
    renderer = _Renderer()
    lock = threading.Lock()

    service = AppSessionServices(
        session_state=SessionRuntimeState.from_legacy(history=[], shared_state={}, shared_state_lock=lock),
        storage=storage,
        renderer=renderer,
        agent_pool=SimpleNamespace(primary="codex"),
        context_manager=_ContextManager(),
        session_summarizer=Mock(),
        task_services=Mock(stop_task_executors=Mock()),
        prompt_builder=SimpleNamespace(history_window=2),
    )
    fake_thread = FakeThread()
    service._summarization_thread = fake_thread

    service.shutdown(interrupted=True)

    assert fake_thread.join_calls == []


def test_shutdown_flushes_pending_auto_summary_completion():
    """shutdown() deve exibir a conclusão pendente do resumo automático antes de encerrar."""
    history = [{"role": "human", "content": f"m{i}"} for i in range(12)]
    storage = _Storage()
    renderer = _Renderer()
    lock = threading.Lock()
    context = _ContextManager()

    class Summarizer:
        def summarize(self, messages, existing_summary=None, preferred_agent=None, fallback=True):
            return "resumo pronto"

    service = AppSessionServices(
        session_state=SessionRuntimeState.from_legacy(history=history, shared_state={}, shared_state_lock=lock),
        storage=storage,
        renderer=renderer,
        agent_pool=SimpleNamespace(primary="codex"),
        context_manager=context,
        session_summarizer=Summarizer(),
        task_services=Mock(stop_task_executors=Mock()),
        prompt_builder=SimpleNamespace(history_window=2),
        auto_summarize_threshold=4,
    )

    service.maybe_auto_summarize()
    service.join_summarization(timeout=3)
    renderer.system_messages.clear()
    renderer.notifications.clear()

    service.shutdown(interrupted=True)

    assert renderer.system_messages == []
    assert renderer.notifications == [
        ("Resumo concluído; histórico mantido em 2 mensagens recentes.", "information", None),
    ]
    assert service._pending_summary_completion is None


def test_auto_summarize_records_failure_when_worker_raises():
    """Exceção no worker de auto-sumarização deve virar falha controlada."""
    history = [{"role": "human", "content": f"m{i}"} for i in range(12)]
    storage = _Storage()
    renderer = _Renderer()
    lock = threading.Lock()

    class ExplodingSummarizer:
        def summarize(self, messages, existing_summary=None, preferred_agent=None, fallback=True):
            raise RuntimeError("boom")

    service = AppSessionServices(
        session_state=SessionRuntimeState.from_legacy(history=history, shared_state={}, shared_state_lock=lock),
        storage=storage,
        renderer=renderer,
        agent_pool=SimpleNamespace(primary="codex"),
        context_manager=_ContextManager(),
        session_summarizer=ExplodingSummarizer(),
        task_services=Mock(stop_task_executors=Mock()),
        prompt_builder=SimpleNamespace(history_window=2),
        auto_summarize_threshold=4,
    )

    service.maybe_auto_summarize()
    service.join_summarization(timeout=3)

    assert service._pending_summary_completion == (
        "Resumo automático falhou; histórico mantido."
    )


def test_join_summarization_is_noop_when_no_thread_started():
    """join_summarization não deve lançar exceção quando nenhuma thread foi iniciada."""
    storage = _Storage()
    renderer = _Renderer()
    lock = threading.Lock()

    service = AppSessionServices(
        session_state=SessionRuntimeState.from_legacy(history=[], shared_state={}, shared_state_lock=lock),
        storage=storage,
        renderer=renderer,
        agent_pool=SimpleNamespace(primary="codex"),
        context_manager=_ContextManager(),
        session_summarizer=Mock(),
        task_services=Mock(stop_task_executors=Mock()),
        prompt_builder=SimpleNamespace(history_window=2),
    )

    # _summarization_thread é None — não deve lançar
    service.join_summarization(timeout=1)

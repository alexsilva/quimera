import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quimera.app import QuimeraApp
from quimera.app.protocol import AppProtocol
from quimera.app.session import AppSessionServices
from quimera.domain.session_state import SessionState
from quimera.shared_state import clear_agent_state_for_session_start


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


class _Renderer:
    def __init__(self):
        self.system_messages = []

    def show_system(self, message):
        self.system_messages.append(message)


class _ContextManager:
    def __init__(self):
        self.saved_summary = None

    def load_session_summary(self):
        return None

    def update_with_summary(self, summary):
        self.saved_summary = summary


def test_parse_response_preserves_shared_state_lock():
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

    app.parse_response('[STATE_UPDATE]{"goal":"corrigir lock"}[/STATE_UPDATE]')

    assert app.protocol._lock is app._shared_state_lock
    assert app.protocol._lock is not app._lock
    assert app.shared_state["goal"] == "corrigir lock"


def test_handoff_json_is_plain_content_not_protocol_envelope():
    proto = AppProtocol(lock=threading.Lock(), shared_state={})

    response, target, handoff, extend, needs_input, ack_id = proto.parse_response(
        '{"type":"handoff","route":"codex","content":"delegar textual"}'
    )

    assert response == '{"type":"handoff","route":"codex","content":"delegar textual"}'
    assert target is None
    assert handoff is None
    assert extend is False
    assert needs_input is False
    assert ack_id is None


def _make_merge_app(workspace_root: Path, state_dir: Path | None = None):
    app = QuimeraApp.__new__(QuimeraApp)
    app.workspace = SimpleNamespace(cwd=workspace_root, state_dir=state_dir or workspace_root / ".state")
    return app


def test_merge_staging_to_workspace_normal_path_writes_manifest(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    staging = tmp_path / "staging"
    src = staging / "0" / "dir" / "file.txt"
    src.parent.mkdir(parents=True)
    src.write_text("novo", encoding="utf-8")
    app = _make_merge_app(workspace)

    manifest = app._merge_staging_to_workspace(staging)

    assert (workspace / "dir" / "file.txt").read_text(encoding="utf-8") == "novo"
    assert manifest[0]["relative_path"] == "dir/file.txt"
    assert manifest[0]["overwritten"] is False
    manifest_log = app.workspace.state_dir / "staging_merge_manifest.jsonl"
    assert '"relative_path": "dir/file.txt"' in manifest_log.read_text(encoding="utf-8")


def test_merge_staging_to_workspace_blocks_destination_outside_workspace(tmp_path):
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
        app._merge_staging_to_workspace(staging)
    assert not (outside / "escape.txt").exists()


def test_merge_staging_to_workspace_blocks_source_symlink(tmp_path):
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
        app._merge_staging_to_workspace(staging)


def test_merge_staging_to_workspace_records_overwrite(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("antigo", encoding="utf-8")
    staging = tmp_path / "staging"
    src = staging / "0" / "file.txt"
    src.parent.mkdir(parents=True)
    src.write_text("novo", encoding="utf-8")
    app = _make_merge_app(workspace)

    manifest = app._merge_staging_to_workspace(staging)

    assert (workspace / "file.txt").read_text(encoding="utf-8") == "novo"
    assert manifest[0]["overwritten"] is True


def test_session_summarize_preserves_concurrent_persisted_message():
    history = [{"role": "human", "content": f"m{i}"} for i in range(12)]
    storage = _Storage()
    renderer = _Renderer()
    context = _ContextManager()
    lock = threading.Lock()

    class Summarizer:
        def __init__(self):
            self.service = None

        def summarize(self, messages, existing_summary=None, preferred_agent=None):
            self.service.persist_message("assistant", "append during summary")
            return "resumo"

    summarizer = Summarizer()
    service = AppSessionServices(
        session_state=SessionState(history=history, shared_state={}, shared_state_lock=lock),
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

    assert history == [
        {"role": "human", "content": "m10"},
        {"role": "human", "content": "m11"},
        {"role": "assistant", "content": "append during summary"},
    ]
    assert storage.saved_history == history



def test_session_summarize_does_not_save_summary_when_snapshot_changes():
    history = [{"role": "human", "content": f"m{i}"} for i in range(12)]
    original_length = len(history)
    storage = _Storage()
    renderer = _Renderer()
    context = _ContextManager()
    lock = threading.Lock()

    class Summarizer:
        def __init__(self):
            self.service = None

        def summarize(self, messages, existing_summary=None, preferred_agent=None):
            with self.service._lock:
                self.service._history[0] = {"role": "human", "content": "mutado durante resumo"}
            return "resumo inconsistente"

    summarizer = Summarizer()
    service = AppSessionServices(
        session_state=SessionState(history=history, shared_state={}, shared_state_lock=lock),
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

    assert context.saved_summary is None
    assert storage.saved_history is None
    assert len(history) == original_length
    assert history[0] == {"role": "human", "content": "mutado durante resumo"}
    assert "[memória] histórico mudou durante o resumo — truncamento adiado" in renderer.system_messages


def test_session_shutdown_summarizes_stable_history_snapshot_after_pending_save():
    history = [{"role": "human", "content": "before shutdown"}]
    shared_state = {"goal": "persist me"}
    storage = _Storage()
    renderer = _Renderer()
    context = _ContextManager()

    class Summarizer:
        def __init__(self):
            self.service = None
            self.messages_seen = None

        def summarize(self, messages, existing_summary=None, preferred_agent=None):
            self.messages_seen = messages
            self.service.persist_message("assistant", "mutated during shutdown summary")
            return "shutdown summary"

    summarizer = Summarizer()
    service = AppSessionServices(
        session_state=SessionState(history=history, shared_state=shared_state),
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
    assert history == [
        {"role": "human", "content": "before shutdown"},
        {"role": "assistant", "content": "mutated during shutdown summary"},
    ]
    assert context.saved_summary == "shutdown summary"

def test_restored_session_clears_volatile_agent_goal_state_only():
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
    monkeypatch.delenv("QUIMERA_CURRENT_JOB_ID", raising=False)
    app = QuimeraApp.__new__(QuimeraApp)
    app._previous_current_job_id_env = None
    os.environ["QUIMERA_CURRENT_JOB_ID"] = "123"

    app._restore_current_job_env()

    assert "QUIMERA_CURRENT_JOB_ID" not in os.environ


def test_current_job_env_restore_restores_previous_value(monkeypatch):
    monkeypatch.setenv("QUIMERA_CURRENT_JOB_ID", "old")
    app = QuimeraApp.__new__(QuimeraApp)
    app._previous_current_job_id_env = "old"
    os.environ["QUIMERA_CURRENT_JOB_ID"] = "new"

    app._restore_current_job_env()

    assert os.environ["QUIMERA_CURRENT_JOB_ID"] == "old"

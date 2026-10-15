from types import SimpleNamespace
from unittest.mock import Mock

from quimera.app.lifecycle import AppLifecycle
from quimera.runtime.tools.mcp_clients import set_bridge


def _app(events):
    def record(name):
        return Mock(side_effect=lambda *args, **kwargs: events.append(name))

    return SimpleNamespace(
        debate_service=SimpleNamespace(shutdown=record("debate.shutdown")),
        _stop_task_executors=record("tasks.stop"),
        process_supervisor=SimpleNamespace(
            terminate_all=record("processes.terminate"),
            shutdown=record("processes.shutdown"),
        ),
        session_services=SimpleNamespace(shutdown=record("session.shutdown")),
        current_job_id=None,
        agent_client=SimpleNamespace(close=record("agent.close")),
        renderer=SimpleNamespace(close=record("renderer.close")),
        _run_render_bug_detector=record("render.audit"),
        behavior_metrics=SimpleNamespace(_flush_if_dirty=record("metrics.flush")),
        _restore_current_job_env=record("env.restore"),
        bug_store=SimpleNamespace(close=record("bugs.close")),
    )


def test_lifecycle_cancels_debate_before_background_executors():
    events = []
    lifecycle = AppLifecycle(_app(events))

    lifecycle.close(interrupted=True)

    assert events[:2] == ["debate.shutdown", "tasks.stop"]
    assert events.count("debate.shutdown") == 1
    assert events.index("processes.terminate") < events.index("session.shutdown")
    assert events.index("session.shutdown") < events.index("processes.shutdown")


def test_lifecycle_close_is_idempotent():
    events = []
    lifecycle = AppLifecycle(_app(events))

    lifecycle.close()
    first = list(events)
    lifecycle.close()

    assert events == first


def test_lifecycle_close_encerra_mcp_clients_externos_depois_do_resumo():
    """O shutdown do bridge mata os mcp-remote e fecha os pipes antes do atexit
    unir as threads do pool do chat; corre depois do resumo de sessão, que
    ainda pode usar tools externas."""
    events = []
    bridge = SimpleNamespace(shutdown=Mock(side_effect=lambda: events.append("mcp.shutdown")))
    set_bridge(bridge)
    try:
        AppLifecycle(_app(events)).close()
    finally:
        set_bridge(None)

    assert events.count("mcp.shutdown") == 1
    assert events.index("session.shutdown") < events.index("mcp.shutdown")
    assert events.index("mcp.shutdown") < events.index("agent.close")


def test_lifecycle_close_segue_quando_o_shutdown_do_bridge_falha():
    events = []
    set_bridge(SimpleNamespace(shutdown=Mock(side_effect=RuntimeError("pipe quebrado"))))
    try:
        AppLifecycle(_app(events)).close()
    finally:
        set_bridge(None)

    assert "agent.close" in events
    assert "bugs.close" in events

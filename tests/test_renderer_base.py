"""Contrato da RendererBase (PLAN_RENDERER_PROTOCOL.md, fases 0–1)."""
import unittest

from quimera.ui.base import RendererBase
from quimera.ui.renderer import TerminalRenderer
from quimera.ui.textual.constants import (
    format_failover_message,
    format_retry_message,
)
from quimera.ui.textual.renderer import TextualRenderer


#: Todo método/atributo que a camada consumidora usa do renderer. Internals
#: (_audit_logger, _console, _window_manager) NÃO fazem parte do contrato;
#: os equivalentes públicos são log_debug_event, print_direct e
#: agent_window_controller (fase 4 do plano).
CONTRACT = [
    "show_system",
    "show_warning",
    "show_error",
    "show_banner",
    "show_system_neutral",
    "show_approval",
    "show_feed",
    "show_agent",
    "show_message",
    "show_no_response",
    "show_delegation",
    "show_prompt_preview",
    "notify_agent_retry",
    "notify_agent_failover",
    "show_notification",
    "update_agent_transient",
    "clear_agent_transient",
    "commit_agent_stream",
    "show_plain",
    "show_turn_summary",
    "flush",
    "flush_quick",
    "signal_restore_history",
    "set_summarizing",
    "set_prompt_integration",
    "log_debug_event",
    "close",
    "running_status",
    "print_direct",
    "clear_screen",
    "selection_window",
    "approval_window",
    "agent_window_controller",
    "set_profile_resolver",
    "set_orchestrator",
    "supports_agent_feed",
    "supports_structured_agent_activity",
    "theme_name",
    "cycle_theme",
]


class _RecordingRenderer(RendererBase):
    def __init__(self):
        self.system_messages = []

    def show_system(self, message):
        self.system_messages.append(message)


class TestRendererBaseContract(unittest.TestCase):
    def test_contract_methods_exist(self):
        """Todo nome sniffado em produção existe na base."""
        for name in CONTRACT:
            self.assertTrue(hasattr(RendererBase, name), name)

    def test_show_system_is_required(self):
        with self.assertRaises(NotImplementedError):
            RendererBase().show_system("x")

    def test_optional_displays_fall_back_to_show_system(self):
        renderer = _RecordingRenderer()
        renderer.show_banner("banner")
        renderer.show_system_neutral("neutral")
        renderer.show_approval("approval")
        renderer.show_feed("feed", agent="claude", muted=True)
        renderer.show_agent("claude", "agent")
        renderer.show_warning("warn")
        renderer.show_error("err", agent="claude")
        renderer.show_message("claude", "msg")
        self.assertEqual(
            renderer.system_messages,
            ["banner", "neutral", "approval", "feed", "agent", "warn", "err", "msg"],
        )

    def test_notify_fallbacks_use_canonical_formatters(self):
        renderer = _RecordingRenderer()
        renderer.notify_agent_failover("claude", target="codex")
        renderer.notify_agent_retry(
            "claude", reason="no_response", attempt=1, limit=2, detail="d"
        )
        self.assertEqual(renderer.system_messages[0], format_failover_message("claude", "codex"))
        self.assertEqual(
            renderer.system_messages[1],
            format_retry_message("no_response", 1, 2, "d"),
        )

    def test_infra_methods_are_noop(self):
        renderer = _RecordingRenderer()
        self.assertIsNone(renderer.flush())
        self.assertTrue(renderer.flush_quick())
        self.assertIsNone(renderer.signal_restore_history())
        self.assertIsNone(renderer.set_summarizing(True))
        self.assertIsNone(renderer.set_prompt_integration(None, None))
        self.assertIsNone(renderer.log_debug_event("evt", key="v"))
        self.assertIsNone(renderer.show_notification("mensagem"))
        self.assertIsNone(renderer.show_no_response("claude"))
        self.assertIsNone(renderer.show_delegation("a", "b"))
        self.assertIsNone(renderer.show_prompt_preview("a", "p"))
        self.assertIsNone(renderer.update_agent_transient("a", "m"))
        self.assertIsNone(renderer.clear_agent_transient("a"))
        self.assertFalse(renderer.commit_agent_stream("a"))
        self.assertIsNone(renderer.close())
        with renderer.running_status("status", agent="a") as status:
            self.assertIsNone(status)
        self.assertIsNone(renderer.agent_window_controller("a"))
        self.assertIsNone(renderer.set_profile_resolver(lambda agent: None))
        self.assertIsNone(renderer.set_orchestrator("a"))
        with renderer.selection_window(metadata={}):
            pass
        with renderer.approval_window(title="t"):
            pass
        self.assertEqual(renderer.system_messages, [])

    def test_show_plain_and_turn_summary_fall_back_to_show_system(self):
        renderer = _RecordingRenderer()
        renderer.show_plain("linha", agent="claude", muted=True)
        renderer.show_turn_summary("claude", {"tools": [{"status": "ok", "duration_ms": 1200}]})
        renderer.show_turn_summary("claude", {"tools": []})
        self.assertEqual(
            renderer.system_messages,
            ["linha", "TOOLS: 1 chamadas · 1 ok · 0 erro · 1.2s"],
        )

    def test_capability_defaults(self):
        self.assertFalse(RendererBase.supports_agent_feed)
        self.assertFalse(RendererBase.supports_structured_agent_activity)
        self.assertTrue(TextualRenderer.supports_structured_agent_activity)
        self.assertFalse(TerminalRenderer.supports_structured_agent_activity)
        self.assertEqual(_RecordingRenderer().theme_name, "")
        self.assertIsNone(_RecordingRenderer().cycle_theme())

    def test_production_renderers_inherit_base(self):
        self.assertTrue(issubclass(TerminalRenderer, RendererBase))
        self.assertTrue(issubclass(TextualRenderer, RendererBase))


if __name__ == "__main__":
    unittest.main()

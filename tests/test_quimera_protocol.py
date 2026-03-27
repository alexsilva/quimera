import unittest
from unittest.mock import patch

from quimera.app import QuimeraApp
from quimera.constants import AGENT_CLAUDE, AGENT_CODEX, EXTEND_MARKER
from quimera.prompt import PromptBuilder


class DummyRenderer:
    def __init__(self):
        self.warnings = []
        self.system_messages = []

    def show_warning(self, message):
        self.warnings.append(message)

    def show_system(self, message):
        self.system_messages.append(message)


class DummyContextManager:
    def load(self):
        return ""


class DummyStorage:
    def get_log_file(self):
        return "/tmp/quimera.log"


class ProtocolTests(unittest.TestCase):
    def test_parse_mode_detects_extend_marker_at_end(self):
        app = QuimeraApp.__new__(QuimeraApp)

        response, extend = app.parse_mode(f"Resposta objetiva {EXTEND_MARKER}")

        self.assertEqual(response, "Resposta objetiva")
        self.assertTrue(extend)

    def test_parse_mode_keeps_plain_response(self):
        app = QuimeraApp.__new__(QuimeraApp)

        response, extend = app.parse_mode("Resposta objetiva")

        self.assertEqual(response, "Resposta objetiva")
        self.assertFalse(extend)

    def test_parse_route_extracts_internal_handoff(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN

        response, target, message = app.parse_route(
            "Resposta visivel\n[ROUTE:codex] Revise este argumento."
        )

        self.assertEqual(response, "Resposta visivel")
        self.assertEqual(target, AGENT_CODEX)
        self.assertEqual(message, "Revise este argumento.")

    def test_parse_routing_rejects_double_prefix(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()

        agent, message = app.parse_routing("/claude /codex revisar isso")

        self.assertIsNone(agent)
        self.assertIsNone(message)
        self.assertTrue(app.renderer.warnings)

    def test_prompt_marks_only_first_speaker(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        first_prompt = builder.build(AGENT_CLAUDE, history, is_first_speaker=True)
        second_prompt = builder.build(AGENT_CODEX, history, is_first_speaker=False)

        self.assertIn(EXTEND_MARKER, first_prompt)
        self.assertNotIn(EXTEND_MARKER, second_prompt)

    def test_prompt_includes_handoff_when_present(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CODEX, history, handoff="Revise este ponto.")

        self.assertIn("MENSAGEM DIRETA DO OUTRO AGENTE", prompt)
        self.assertIn("Revise este ponto.", prompt)

    def test_run_uses_two_turns_by_default(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = None
        app.prompt_builder = None
        persisted = []
        printed = []

        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi")
        app.parse_mode = QuimeraApp.parse_mode.__get__(app, QuimeraApp)
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.persist_message = lambda role, content: persisted.append((role, content))
        app.shutdown = lambda: None
        responses = iter(["claude responde", "codex comenta"])
        app.call_agent = lambda agent, is_first_speaker=False, handoff=None: next(responses)

        with patch("builtins.input", side_effect=["mensagem", "/exit"]):
            app.run()

        self.assertEqual(
            printed,
            [(AGENT_CLAUDE, "claude responde"), (AGENT_CODEX, "codex comenta")],
        )
        self.assertEqual(
            persisted,
            [
                ("human", "oi"),
                (AGENT_CLAUDE, "claude responde"),
                (AGENT_CODEX, "codex comenta"),
            ],
        )

    def test_run_uses_four_turns_when_extended(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = None
        app.prompt_builder = None
        persisted = []
        printed = []

        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi")
        app.parse_mode = QuimeraApp.parse_mode.__get__(app, QuimeraApp)
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.persist_message = lambda role, content: persisted.append((role, content))
        app.shutdown = lambda: None
        responses = iter(
            [
                "claude abre [DEBATE]",
                "codex comenta",
                "claude aprofunda",
                "codex fecha",
            ]
        )
        app.call_agent = lambda agent, is_first_speaker=False, handoff=None: next(responses)

        with patch("builtins.input", side_effect=["mensagem", "/exit"]):
            app.run()

        self.assertEqual(
            printed,
            [
                ("claude", "claude abre"),
                ("codex", "codex comenta"),
                ("claude", "claude aprofunda"),
                ("codex", "codex fecha"),
            ],
        )
        self.assertEqual(
            persisted,
            [
                ("human", "oi"),
                (AGENT_CLAUDE, "claude abre"),
                (AGENT_CODEX, "codex comenta"),
                (AGENT_CLAUDE, "claude aprofunda"),
                (AGENT_CODEX, "codex fecha"),
            ],
        )

    def test_run_passes_handoff_to_target_agent(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = None
        app.prompt_builder = None
        persisted = []
        printed = []
        calls = []

        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi")
        app.parse_mode = QuimeraApp.parse_mode.__get__(app, QuimeraApp)
        app.parse_route = QuimeraApp.parse_route.__get__(app, QuimeraApp)
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.persist_message = lambda role, content: persisted.append((role, content))
        app.shutdown = lambda: None
        responses = iter(
            [
                "claude responde\n[ROUTE:codex] Revise este argumento.",
                "codex comenta",
            ]
        )

        def fake_call(agent, is_first_speaker=False, handoff=None):
            calls.append((agent, is_first_speaker, handoff))
            return next(responses)

        app.call_agent = fake_call

        with patch("builtins.input", side_effect=["mensagem", "/exit"]):
            app.run()

        self.assertEqual(
            calls,
            [
                (AGENT_CLAUDE, True, None),
                (AGENT_CODEX, False, "Revise este argumento."),
            ],
        )
        self.assertEqual(
            printed,
            [(AGENT_CLAUDE, "claude responde"), (AGENT_CODEX, "codex comenta")],
        )
        self.assertEqual(
            persisted,
            [
                ("human", "oi"),
                (AGENT_CLAUDE, "claude responde"),
                (AGENT_CODEX, "codex comenta"),
            ],
        )


if __name__ == "__main__":
    unittest.main()

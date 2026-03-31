import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import quimera.cli as cli_module
import quimera.plugins as plugins
from quimera.agents import AgentClient
from quimera.app import QuimeraApp
from quimera.cli import main as cli_main
from quimera.config import DEFAULT_HISTORY_WINDOW
from quimera.constants import CMD_HELP, EXTEND_MARKER, MSG_HELP

AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"
from quimera.plugins import AgentPlugin
from quimera.prompt import PromptBuilder
from quimera.session_summary import SessionSummarizer
from quimera.ui import _agent_style


class DummyRenderer:
    def __init__(self):
        self.warnings = []
        self.system_messages = []
        self.handoffs = []

    def show_warning(self, message):
        self.warnings.append(message)

    def show_system(self, message):
        self.system_messages.append(message)

    def show_handoff(self, from_agent, to_agent, task=None):
        self.handoffs.append((from_agent, to_agent, task))


class DummyContextManager:
    def load(self):
        return ""


class DummyConfigManager:
    def __init__(self):
        self.user_name = "Você"
        self.history_window = DEFAULT_HISTORY_WINDOW
        self.auto_summarize_threshold = 30


class DummyStorage:
    def append_log(self, role, content):
        self.last_log = (role, content)

    def get_log_file(self):
        return "/tmp/quimera.log"

    def get_history_file(self):
        return Path("/tmp/sessao-2026-03-27-123456.json")

    def save_history(self, history, shared_state=None):
        self.saved_history = history
        self.saved_shared_state = shared_state


class ProtocolTests(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(cli_module, "TerminalRenderer") and hasattr(cli_module, "AgentClient"),
        "interactive-test CLI não está disponível nesta versão",
    )
    def test_cli_runs_interactive_test_with_default_prompt(self):
        class FakeRenderer:
            instances = []

            def __init__(self):
                self.system_messages = []
                self.plain_messages = []
                FakeRenderer.instances.append(self)

            def show_system(self, message):
                self.system_messages.append(message)

            def show_plain(self, message):
                self.plain_messages.append(message)

        calls = []

        class FakeAgentClient:
            def __init__(self, renderer, metrics_file=None):
                self.renderer = renderer

            def call(self, agent, prompt):
                calls.append((agent, prompt))
                return "saida limpa"

        with patch("quimera.cli.ConfigManager", DummyConfigManager), patch(
            "quimera.cli.TerminalRenderer", FakeRenderer
        ), patch("quimera.cli.AgentClient", FakeAgentClient), patch(
            "sys.argv", ["quimera", "--interactive-test"]
        ):
            cli_main()

        self.assertEqual(len(FakeRenderer.instances), 1)
        self.assertEqual(calls, [(AGENT_CLAUDE, "Use uma ferramenta de shell para executar o comando `pwd` e me diga o diretório atual. Se a ferramenta pedir aprovação, mostre o prompt normalmente.")])
        self.assertTrue(FakeRenderer.instances[0].system_messages)
        self.assertEqual(FakeRenderer.instances[0].plain_messages, ["\n--- RESULTADO LIMPO ---\n", "saida limpa"])

    @unittest.skipUnless(
        hasattr(cli_module, "TerminalRenderer") and hasattr(cli_module, "AgentClient"),
        "interactive-test CLI não está disponível nesta versão",
    )
    def test_cli_runs_interactive_test_with_custom_prompt(self):
        calls = []

        class FakeRenderer:
            def show_system(self, message):
                pass

            def show_plain(self, message):
                pass

        class FakeAgentClient:
            def __init__(self, renderer, metrics_file=None):
                self.renderer = renderer

            def call(self, agent, prompt):
                calls.append((agent, prompt))
                return None

        with patch("quimera.cli.ConfigManager", DummyConfigManager), patch(
            "quimera.cli.TerminalRenderer", FakeRenderer
        ), patch("quimera.cli.AgentClient", FakeAgentClient), patch(
            "sys.argv", ["quimera", "--interactive-test", "codex", "--test-prompt", "rode", "pwd"]
        ):
            cli_main()

        self.assertEqual(calls, [(AGENT_CODEX, "rode pwd")])

    def test_parse_response_detects_extend_marker_at_end(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
        app.shared_state = {}

        response, _, _, extend = app.parse_response(f"Resposta objetiva {EXTEND_MARKER}")

        self.assertEqual(response, "Resposta objetiva")
        self.assertTrue(extend)

    def test_parse_response_keeps_plain_response(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
        app.shared_state = {}

        response, target, handoff, extend = app.parse_response("Resposta objetiva")

        self.assertEqual(response, "Resposta objetiva")
        self.assertIsNone(target)
        self.assertIsNone(handoff)
        self.assertFalse(extend)

    def test_parse_response_extracts_internal_handoff(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
        app.shared_state = {}

        response, target, message, extend = app.parse_response(
            "Resposta visivel\n"
            "[ROUTE:codex] task: Revise este argumento. | context: "
            "Analisar risco no parser atual. | expected: 2 bullets objetivos"
        )

        self.assertEqual(response, "Resposta visivel")
        self.assertEqual(target, AGENT_CODEX)
        self.assertEqual(
            message,
            {
                "task": "Revise este argumento.",
                "context": "Analisar risco no parser atual.",
                "expected": "2 bullets objetivos",
            },
        )
        self.assertFalse(extend)

    def test_parse_response_ignores_invalid_handoff_payload(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
        app.shared_state = {}

        response, target, message, extend = app.parse_response(
            "Resposta visivel\n[ROUTE:codex] Revise este argumento."
        )

        self.assertEqual(response, "Resposta visivel")
        self.assertIsNone(target)
        self.assertIsNone(message)
        self.assertFalse(extend)

    def test_parse_response_extracts_state_update_before_debate(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
        app.shared_state = {}

        response, _, _, extend = app.parse_response(
            "Resposta visivel\n"
            "[STATE_UPDATE]\n"
            '{"goal":"corrigir parser","decisions":["usar json"]}\n'
            "[/STATE_UPDATE]\n"
            f"{EXTEND_MARKER}"
        )

        self.assertEqual(response, "Resposta visivel")
        self.assertTrue(extend)
        self.assertEqual(
            app.shared_state,
            {"goal": "corrigir parser", "decisions": ["usar json"]},
        )

    def test_parse_response_extracts_state_update_after_debate_marker(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
        app.shared_state = {}

        response, _, _, extend = app.parse_response(
            "Resposta visivel\n"
            f"{EXTEND_MARKER}\n"
            "[STATE_UPDATE]\n"
            '{"next_step":"escrever testes"}\n'
            "[/STATE_UPDATE]"
        )

        self.assertEqual(response, "Resposta visivel")
        self.assertTrue(extend)
        self.assertEqual(app.shared_state, {"next_step": "escrever testes"})

    def test_parse_response_merges_multiple_state_updates(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
        app.shared_state = {"decisions": ["A"]}

        response, _, _, extend = app.parse_response(
            "Resposta\n"
            "[STATE_UPDATE]\n"
            '{"decisions":["B"],"goal":"alinhar protocolo"}\n'
            "[/STATE_UPDATE]\n"
            "[STATE_UPDATE]\n"
            '{"decisions":["C"],"next_step":"persistir estado"}\n'
            "[/STATE_UPDATE]"
        )

        self.assertEqual(response, "Resposta")
        self.assertFalse(extend)
        self.assertEqual(
            app.shared_state,
            {
                "decisions": ["A", "B", "C"],
                "goal": "alinhar protocolo",
                "next_step": "persistir estado",
            },
        )

    def test_parse_routing_rejects_double_prefix(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()

        agent, message, explicit = app.parse_routing("/claude /codex revisar isso")

        self.assertIsNone(agent)
        self.assertIsNone(message)
        self.assertTrue(app.renderer.warnings)

    def test_handle_command_shows_help(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()

        handled = app.handle_command(CMD_HELP)

        self.assertTrue(handled)
        self.assertEqual(app.renderer.system_messages, [MSG_HELP])

    def test_prompt_marks_only_first_speaker(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        first_prompt = builder.build(AGENT_CLAUDE, history, is_first_speaker=True)
        second_prompt = builder.build(AGENT_CODEX, history, is_first_speaker=False)

        self.assertIn(EXTEND_MARKER, first_prompt)
        self.assertIn("segundo agente nesta rodada", second_prompt)
        self.assertNotIn("inclua [DEBATE] ao final da sua resposta", second_prompt)

    def test_prompt_uses_handoff_rule_in_handoff_only_mode(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(
            AGENT_CODEX,
            history,
            handoff={
                "task": "Revisar parser",
                "context": "Há dúvida sobre validação",
                "expected": "1 parágrafo curto",
            },
            handoff_only=True,
        )

        self.assertIn("Você recebeu uma subtarefa delegada", prompt)
        self.assertIn("TASK:\nRevisar parser", prompt)
        self.assertIn("EXPECTED:\n1 parágrafo curto", prompt)
        self.assertNotIn("segundo agente nesta rodada", prompt)
        self.assertNotIn("[ROUTE:claude]", prompt)

    def test_prompt_includes_handoff_when_present(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CODEX, history, handoff="Revise este ponto.")

        self.assertIn("MENSAGEM DIRETA DO OUTRO AGENTE", prompt)
        self.assertIn("Revise este ponto.", prompt)

    def test_prompt_includes_session_state_when_present(self):
        builder = PromptBuilder(
            DummyContextManager(),
            history_window=3,
            session_state={
                "session_id": "sessao-2026-03-27-123456",
                "is_new_session": "não",
                "history_restored": "sim",
                "summary_loaded": "não",
            },
        )
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CLAUDE, history)

        self.assertIn("ESTADO DA SESSÃO", prompt)
        self.assertIn("SESSÃO ATUAL: sessao-2026-03-27-123456", prompt)
        self.assertIn("NOVA SESSÃO: não", prompt)
        self.assertIn("HISTÓRICO RESTAURADO: sim", prompt)
        self.assertIn("RESUMO CARREGADO: não", prompt)

    def test_prompt_includes_shared_state_as_json(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={"goal": "corrigir", "decisions": ["usar json"]},
        )

        self.assertIn("ESTADO COMPARTILHADO", prompt)
        self.assertIn('"goal": "corrigir"', prompt)
        self.assertIn('"decisions": [', prompt)

    def test_prompt_truncates_shared_state_to_last_five_decisions(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]
        big_state = {
            "goal": "objetivo",
            "next_step": "próximo passo",
            "decisions": [f"d{i}" for i in range(10)],
            "open_disagreements": ["x" * 200],
        }

        prompt = builder.build(AGENT_CLAUDE, history, shared_state=big_state)

        # Extrai apenas o bloco ESTADO COMPARTILHADO para verificar o conteúdo truncado
        state_start = prompt.index("ESTADO COMPARTILHADO:")
        state_block = prompt[state_start:]
        self.assertIn('"goal": "objetivo"', state_block)
        self.assertIn('"next_step": "próximo passo"', state_block)
        self.assertIn('"d9"', state_block)
        self.assertNotIn('"d0"', state_block)
        self.assertNotIn('"open_disagreements"', state_block)

    def test_app_builds_explicit_session_state_for_prompt(self):
        class FakeWorkspace:
            def __init__(self, cwd):
                self.root = Path("/tmp/projeto")
                self.cwd = cwd
                self.context_persistent = Path("/tmp/quimera_context.md")
                self.context_session = Path("/tmp/quimera_session_context.md")
                self.logs_dir = Path("/tmp/quimera_logs")

            def migrate_from_legacy(self, cwd):
                return []

        class FakeContextManager:
            SUMMARY_MARKER = "## Resumo da última sessão"

            def __init__(self, *_args):
                pass

            def load_session(self):
                return "## Resumo da última sessão\n\nResumo anterior"

        class FakeSessionStorage:
            def __init__(self, *_args):
                pass

            def load_last_session(self):
                return {
                    "messages": [{"role": "human", "content": "oi"}],
                    "shared_state": {"goal": "continuar"},
                }

            def get_history_file(self):
                return Path("/tmp/sessao-2026-03-27-123456.json")

        with patch("quimera.app.ConfigManager", DummyConfigManager), patch("quimera.app.Workspace", FakeWorkspace), patch(
            "quimera.app.ContextManager", FakeContextManager
        ), patch("quimera.app.SessionStorage", FakeSessionStorage):
            app = QuimeraApp(Path("/tmp/projeto"))

        self.assertEqual(
            app.prompt_builder.session_state,
            {
                "session_id": "sessao-2026-03-27-123456",
                "is_new_session": "não",
                "history_restored": "sim",
                "summary_loaded": "sim",
            },
        )
        self.assertEqual(app.shared_state, {"goal": "continuar"})

    def test_app_uses_default_history_window_from_config(self):
        class FakeWorkspace:
            def __init__(self, cwd):
                self.root = Path("/tmp/projeto")
                self.cwd = cwd
                self.context_persistent = Path("/tmp/quimera_context.md")
                self.context_session = Path("/tmp/quimera_session_context.md")
                self.logs_dir = Path("/tmp/quimera_logs")

            def migrate_from_legacy(self, cwd):
                return []

        class FakeContextManager:
            SUMMARY_MARKER = "## Resumo da última sessão"

            def __init__(self, *_args):
                pass

            def load_session(self):
                return ""

        class FakeSessionStorage:
            def __init__(self, *_args):
                pass

            def load_last_session(self):
                return {"messages": [], "shared_state": {}}

            def get_history_file(self):
                return Path("/tmp/sessao-2026-03-27-123456.json")

        with patch("quimera.app.ConfigManager", DummyConfigManager), patch("quimera.app.Workspace", FakeWorkspace), patch(
            "quimera.app.ContextManager", FakeContextManager
        ), patch("quimera.app.SessionStorage", FakeSessionStorage):
            app = QuimeraApp(Path("/tmp/projeto"))

        self.assertEqual(app.prompt_builder.history_window, DEFAULT_HISTORY_WINDOW)

    def test_app_allows_history_window_override(self):
        class FakeWorkspace:
            def __init__(self, cwd):
                self.root = Path("/tmp/projeto")
                self.cwd = cwd
                self.context_persistent = Path("/tmp/quimera_context.md")
                self.context_session = Path("/tmp/quimera_session_context.md")
                self.logs_dir = Path("/tmp/quimera_logs")

            def migrate_from_legacy(self, cwd):
                return []

        class FakeContextManager:
            SUMMARY_MARKER = "## Resumo da última sessão"

            def __init__(self, *_args):
                pass

            def load_session(self):
                return ""

        class FakeSessionStorage:
            def __init__(self, *_args):
                pass

            def load_last_session(self):
                return {"messages": [], "shared_state": {}}

            def get_history_file(self):
                return Path("/tmp/sessao-2026-03-27-123456.json")

        with patch("quimera.app.ConfigManager", DummyConfigManager), patch("quimera.app.Workspace", FakeWorkspace), patch(
            "quimera.app.ContextManager", FakeContextManager
        ), patch("quimera.app.SessionStorage", FakeSessionStorage):
            app = QuimeraApp(Path("/tmp/projeto"), history_window=5)

        self.assertEqual(app.prompt_builder.history_window, 5)

    def test_run_uses_two_turns_by_default(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.user_name = "Você"
        app.round_index = 0
        app.session_call_index = 0
        app.debug_prompt_metrics = False
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = None
        app.prompt_builder = None
        app.session_state = {
            "session_id": "sessao-2026-03-27-123456",
            "history_count": 0,
            "summary_loaded": False,
        }
        persisted = []
        printed = []

        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.shared_state = {}
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.persist_message = lambda role, content: persisted.append((role, content))
        app.shutdown = lambda: None
        app.read_user_input = Mock(side_effect=["mensagem", "/exit"])
        other_agents = [n for n in plugins.all_names() if n != AGENT_CLAUDE]
        all_responses = ["claude responde"] + [f"{a} comenta" for a in other_agents]
        responses = iter(all_responses)
        app.call_agent = lambda agent, is_first_speaker=False, handoff=None, primary=True, protocol_mode="standard": next(responses)

        app.run()

        expected_printed = [(AGENT_CLAUDE, "claude responde")] + [
            (a, f"{a} comenta") for a in other_agents
        ]
        self.assertEqual(printed, expected_printed)
        self.assertEqual(
            persisted,
            [("human", "oi")] + [(AGENT_CLAUDE, "claude responde")] + [
                (a, f"{a} comenta") for a in other_agents
            ],
        )

    def test_decode_stdin_bytes_falls_back_from_invalid_utf8(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app._input_encoding_candidates = lambda: ["utf-8", "cp1252", "latin-1"]

        decoded = app._decode_stdin_bytes(b"ol\xe1\r\n")

        self.assertEqual(decoded, "olá")

    def test_read_user_input_uses_stdin_buffer_and_decodes_bytes(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.user_name = "Alex"
        app._decode_stdin_bytes = QuimeraApp._decode_stdin_bytes.__get__(app, QuimeraApp)
        app._input_encoding_candidates = lambda: ["utf-8", "cp1252", "latin-1"]

        fake_stdin = type(
            "FakeStdin",
            (),
            {"buffer": Mock(readline=Mock(return_value=b"ma\xe7\xe3\r\n"))},
        )()
        fake_stdout = Mock(write=Mock(), flush=Mock())

        with patch("sys.stdin", fake_stdin), patch("sys.stdout", fake_stdout):
            user = app.read_user_input()

        self.assertEqual(user, "maçã")
        fake_stdout.write.assert_called_once_with("Alex: ")
        fake_stdout.flush.assert_called_once()

    def test_run_uses_four_turns_when_extended(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.user_name = "Você"
        app.round_index = 0
        app.session_call_index = 0
        app.debug_prompt_metrics = False
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = None
        app.prompt_builder = None
        app.session_state = {
            "session_id": "sessao-2026-03-27-123456",
            "history_count": 0,
            "summary_loaded": False,
        }
        persisted = []
        printed = []

        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.shared_state = {}
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.persist_message = lambda role, content: persisted.append((role, content))
        app.shutdown = lambda: None
        app.read_user_input = Mock(side_effect=["mensagem", "/exit"])
        responses = iter(
            [
                "claude abre [DEBATE]",
                "codex comenta",
                "claude aprofunda",
                "codex fecha",
            ]
        )
        app.call_agent = lambda agent, is_first_speaker=False, handoff=None, primary=True, protocol_mode="standard": next(responses)

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
        app.user_name = "Você"
        app.round_index = 0
        app.session_call_index = 0
        app.debug_prompt_metrics = False
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = None
        app.prompt_builder = None
        app.session_state = {
            "session_id": "sessao-2026-03-27-123456",
            "history_count": 0,
            "summary_loaded": False,
        }
        persisted = []
        printed = []
        calls = []

        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.shared_state = {}
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.persist_message = lambda role, content: persisted.append((role, content))
        app.shutdown = lambda: None
        app.read_user_input = Mock(side_effect=["mensagem", "/exit"])
        responses = iter(
            [
                "claude responde\n"
                "[ROUTE:codex] task: Revise este argumento. | context: "
                "Analisar risco no parser atual. | expected: 2 bullets objetivos",
                "codex comenta",
                "claude sintetiza",
            ]
        )

        def fake_call(
            agent,
            is_first_speaker=False,
            handoff=None,
            primary=True,
            protocol_mode="standard",
            handoff_only=False,
        ):
            calls.append((agent, is_first_speaker, handoff, handoff_only))
            return next(responses)

        app.call_agent = fake_call

        app.run()

        self.assertEqual(
            calls,
            [
                (AGENT_CLAUDE, True, None, False),
                (
                    AGENT_CODEX,
                    False,
                    {
                        "task": "Revise este argumento.",
                        "context": "Analisar risco no parser atual.",
                        "expected": "2 bullets objetivos",
                    },
                    True,
                ),
                (
                    AGENT_CLAUDE,
                    False,
                    "Você delegou a seguinte subtarefa ao CODEX:\n\nRevise este argumento.\n\n"
                    "Resposta do CODEX à sua delegação:\n\ncodex comenta\n\n"
                    "Com base na resposta acima, sintetize e conclua sua resposta ao humano.",
                    False,
                ),
            ],
        )
        self.assertEqual(
            printed,
            [
                (AGENT_CLAUDE, "claude responde"),
                (AGENT_CODEX, "codex comenta"),
                (AGENT_CLAUDE, "claude sintetiza"),
            ],
        )
        self.assertEqual(
            persisted,
            [
                ("human", "oi"),
                (AGENT_CLAUDE, "claude responde"),
                (AGENT_CODEX, "codex comenta"),
                (AGENT_CLAUDE, "claude sintetiza"),
            ],
        )
        self.assertEqual(
            app.renderer.handoffs,
            [(AGENT_CLAUDE, AGENT_CODEX, "Revise este argumento.")],
        )

    def test_run_passes_handoff_even_with_explicit_prefix(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.user_name = "Você"
        app.round_index = 0
        app.session_call_index = 0
        app.debug_prompt_metrics = False
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.context_manager = None
        app.agent_client = None
        app.prompt_builder = None
        app.session_state = {
            "session_id": "sessao-2026-03-27-123456",
            "history_count": 0,
            "summary_loaded": False,
        }
        persisted = []
        printed = []
        calls = []

        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", True)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.shared_state = {}
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.persist_message = lambda role, content: persisted.append((role, content))
        app.shutdown = lambda: None
        app.read_user_input = Mock(side_effect=["/claude mensagem", "/exit"])
        responses = iter(
            [
                "claude responde\n"
                "[ROUTE:codex] task: Revise este argumento. | context: "
                "Analisar risco no parser atual. | expected: 2 bullets objetivos",
                "codex comenta",
                "claude sintetiza",
            ]
        )

        def fake_call(
            agent,
            is_first_speaker=False,
            handoff=None,
            primary=True,
            protocol_mode="standard",
            handoff_only=False,
        ):
            calls.append((agent, is_first_speaker, handoff, handoff_only))
            return next(responses)

        app.call_agent = fake_call

        app.run()

        self.assertEqual(
            calls,
            [
                (AGENT_CLAUDE, True, None, False),
                (
                    AGENT_CODEX,
                    False,
                    {
                        "task": "Revise este argumento.",
                        "context": "Analisar risco no parser atual.",
                        "expected": "2 bullets objetivos",
                    },
                    True,
                ),
                (
                    AGENT_CLAUDE,
                    False,
                    "Você delegou a seguinte subtarefa ao CODEX:\n\nRevise este argumento.\n\n"
                    "Resposta do CODEX à sua delegação:\n\ncodex comenta\n\n"
                    "Com base na resposta acima, sintetize e conclua sua resposta ao humano.",
                    False,
                ),
            ],
        )
        self.assertEqual(
            printed,
            [
                (AGENT_CLAUDE, "claude responde"),
                (AGENT_CODEX, "codex comenta"),
                (AGENT_CLAUDE, "claude sintetiza"),
            ],
        )
        self.assertEqual(
            persisted,
            [
                ("human", "oi"),
                (AGENT_CLAUDE, "claude responde"),
                (AGENT_CODEX, "codex comenta"),
                (AGENT_CLAUDE, "claude sintetiza"),
            ],
        )
        self.assertEqual(
            app.renderer.handoffs,
            [(AGENT_CLAUDE, AGENT_CODEX, "Revise este argumento.")],
        )

    def test_persist_message_saves_shared_state(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.shared_state = {"goal": "corrigir protocolo"}
        app.storage = DummyStorage()

        app.persist_message("human", "oi")

        self.assertEqual(app.storage.saved_shared_state, {"goal": "corrigir protocolo"})

    def test_auto_summarize_merges_with_existing_session_summary(self):
        class FakeContextManager:
            def __init__(self):
                self.saved_summary = None

            def load_session_summary(self):
                return "## Resumo da Conversa\n\n- Contexto anterior"

            def update_with_summary(self, summary):
                self.saved_summary = summary

        class FakeSessionSummarizer:
            def __init__(self):
                self.calls = []

            def summarize(self, history, existing_summary=None, preferred_agent=None):
                self.calls.append((history, existing_summary, preferred_agent))
                return "## Resumo da Conversa\n\n- Consolidado"

        app = QuimeraApp.__new__(QuimeraApp)
        app.history = [
            {"role": "human", "content": "m1"},
            {"role": "claude", "content": "m2"},
            {"role": "codex", "content": "m3"},
            {"role": "human", "content": "m4"},
        ]
        app.auto_summarize_threshold = 4
        app.prompt_builder = type("PromptBuilderStub", (), {"history_window": 2})()
        app.context_manager = FakeContextManager()
        app.session_summarizer = FakeSessionSummarizer()
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.shared_state = {"goal": "manter memória"}

        app._maybe_auto_summarize()

        self.assertEqual(
            app.session_summarizer.calls,
            [
                (
                    [
                        {"role": "human", "content": "m1"},
                        {"role": "claude", "content": "m2"},
                    ],
                    "## Resumo da Conversa\n\n- Contexto anterior",
                    "claude",
                )
            ],
        )
        self.assertEqual(app.context_manager.saved_summary, "## Resumo da Conversa\n\n- Consolidado")
        self.assertEqual(
            app.history,
            [
                {"role": "codex", "content": "m3"},
                {"role": "human", "content": "m4"},
            ],
        )
        self.assertEqual(app.storage.saved_shared_state, {"goal": "manter memória"})

    def test_shutdown_merges_existing_session_summary(self):
        class FakeContextManager:
            def __init__(self):
                self.saved_summary = None

            def load_session_summary(self):
                return "## Resumo da Conversa\n\n- Memória acumulada"

            def update_with_summary(self, summary):
                self.saved_summary = summary

        class FakeSessionSummarizer:
            def __init__(self):
                self.calls = []

            def summarize(self, history, existing_summary=None, preferred_agent=None):
                self.calls.append((history, existing_summary, preferred_agent))
                return "## Resumo da Conversa\n\n- Memória consolidada"

        app = QuimeraApp.__new__(QuimeraApp)
        app.history = [{"role": "human", "content": "mensagem final"}]
        app.context_manager = FakeContextManager()
        app.session_summarizer = FakeSessionSummarizer()
        app.renderer = DummyRenderer()
        app.summary_agent_preference = "codex"

        app.shutdown()

        self.assertEqual(
            app.session_summarizer.calls,
            [
                (
                    [{"role": "human", "content": "mensagem final"}],
                    "## Resumo da Conversa\n\n- Memória acumulada",
                    "codex",
                )
            ],
        )
        self.assertEqual(app.context_manager.saved_summary, "## Resumo da Conversa\n\n- Memória consolidada")

    def test_summarize_session_returns_none_when_all_backends_unavailable(self):
        class DummyRendererWithSystem(DummyRenderer):
            def __init__(self):
                super().__init__()
                self.system_messages = []

            def show_system(self, message):
                self.system_messages.append(message)

            def show_error(self, message):
                self.system_messages.append(message)

        renderer = DummyRendererWithSystem()
        summarizer = SessionSummarizer(renderer, summarizer_call=Mock(return_value=None))

        summary = summarizer.summarize(
            [{"role": "human", "content": "Precisamos validar o formato /caminho/absoluto/arquivo:linha."}],
            existing_summary="## Resumo anterior",
        )

        self.assertIsNone(summary)
        self.assertIn("[memória] resumidores indisponíveis", renderer.system_messages)

    def test_summarize_session_returns_none_when_backend_raises(self):
        class DummyRendererWithSystem(DummyRenderer):
            def __init__(self):
                super().__init__()
                self.system_messages = []

            def show_system(self, message):
                self.system_messages.append(message)

            def show_error(self, message):
                self.system_messages.append(message)

        def broken_summarizer(_prompt, preferred_agent=None):
            raise TypeError("backend bug")

        renderer = DummyRendererWithSystem()
        summarizer = SessionSummarizer(renderer, summarizer_call=broken_summarizer)

        summary = summarizer.summarize(
            [{"role": "human", "content": "Vamos fechar o contrato do resumidor."}],
            preferred_agent="codex",
        )

        self.assertIsNone(summary)
        self.assertEqual(renderer.system_messages, ["[memória] resumidores indisponíveis"])


class PluginTests(unittest.TestCase):
    def test_agent_plugin_fields(self):
        p = AgentPlugin(name="test", prefix="/test", cmd=["test", "-p"], style=("red", "Test"))

        self.assertEqual(p.name, "test")
        self.assertEqual(p.prefix, "/test")
        self.assertEqual(p.cmd, ["test", "-p"])
        self.assertEqual(p.style, ("red", "Test"))

    def test_register_and_get(self):
        p = AgentPlugin(name="dummy", prefix="/dummy", cmd=["dummy"], style=("yellow", "Dummy"))

        with patch.dict(plugins._registry, {}, clear=False):
            plugins.register(p)
            self.assertIs(plugins.get("dummy"), p)

    def test_get_returns_none_for_unknown(self):
        with patch.dict(plugins._registry, {}, clear=True):
            self.assertIsNone(plugins.get("naoexiste"))

    def test_default_plugins_loaded(self):
        self.assertIn("claude", plugins.all_names())
        self.assertIn("codex", plugins.all_names())

    def test_all_plugins_returns_agent_plugin_instances(self):
        for p in plugins.all_plugins():
            self.assertIsInstance(p, AgentPlugin)

    def test_all_names_matches_all_plugins(self):
        names = plugins.all_names()
        self.assertEqual(len(names), len(plugins.all_plugins()))
        for p in plugins.all_plugins():
            self.assertIn(p.name, names)

    def test_agent_style_returns_plugin_style(self):
        stub = AgentPlugin(name="stub", prefix="/stub", cmd=["stub"], style=("magenta", "Stub"))
        with patch.dict(plugins._registry, {"stub": stub}):
            self.assertEqual(_agent_style("stub"), ("magenta", "Stub"))

    def test_agent_style_fallback_for_unknown(self):
        with patch.dict(plugins._registry, {}, clear=True):
            color, label = _agent_style("unknown")
            self.assertEqual(color, "white")
            self.assertEqual(label, "Unknown")

    def test_agent_client_call_uses_plugin_cmd(self):
        stub = AgentPlugin(name="stub", prefix="/stub", cmd=["stub", "-x"], style=("white", "Stub"))
        renderer = Mock()

        with patch.dict(plugins._registry, {"stub": stub}):
            client = AgentClient(renderer)
            with patch.object(client, "run", return_value="ok") as mock_run:
                result = client.call("stub", "hello")

        mock_run.assert_called_once_with(["stub", "-x"], input_text="hello")
        self.assertEqual(result, "ok")

    def test_agent_client_call_error_on_unknown_agent(self):
        renderer = Mock()
        with patch.dict(plugins._registry, {}, clear=True):
            client = AgentClient(renderer)
            result = client.call("fantasma", "msg")

        self.assertIsNone(result)
        renderer.show_error.assert_called_once()
        self.assertIn("fantasma", renderer.show_error.call_args[0][0])

    def test_new_plugin_registration_visible_via_all_names(self):
        novo = AgentPlugin(name="novo", prefix="/novo", cmd=["novo"], style=("cyan", "Novo"))

        with patch.dict(plugins._registry, {}, clear=True):
            plugins.register(novo)
            self.assertEqual(plugins.all_names(), ["novo"])
            self.assertEqual(plugins.all_plugins(), [novo])


if __name__ == "__main__":
    unittest.main()

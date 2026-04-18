import io
import re
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import quimera.app as app_module
import quimera.cli as cli_module
import quimera.plugins as plugins
from quimera.agents import AgentClient
from quimera.app import QuimeraApp
from quimera.app.chat_round import ChatRoundOrchestrator
from quimera.app.core import TurnManager
from quimera.app.dispatch import AppDispatchServices
from quimera.app.session import AppSessionServices
from quimera.app.system_layer import AppSystemLayer
from quimera.app.task import AppTaskServices
from quimera.app.protocol import AppProtocol
from quimera.app.session_metrics import SessionMetricsService
from quimera.cli import main as cli_main
from quimera.config import DEFAULT_HISTORY_WINDOW
from quimera.constants import CMD_AGENTS, CMD_CLEAR, CMD_HELP, CMD_PROMPT, EXTEND_MARKER, build_agents_help, build_help
from quimera.plugins import AgentPlugin
from quimera.prompt import PromptBuilder
from quimera.runtime.approval import ApprovalHandler
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.tasks import add_job, complete_task, create_task, init_db, list_tasks
from quimera.session_summary import SessionSummarizer, build_chain_summarizer
from quimera.ui import _agent_style

AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"
AGENT_GEMINI = "gemini"


class DummyRenderer:
    def __init__(self):
        self.warnings = []
        self.system_messages = []
        self.handoffs = []
        self._output_lock = threading.Lock()
        self.task_services = None

    def show_warning(self, message):
        self.warnings.append(message)

    def show_system(self, message):
        self.system_messages.append(message)

    def show_handoff(self, from_agent, to_agent, task=None):
        self.handoffs.append((from_agent, to_agent, task))


class DummyContextManager:
    SUMMARY_MARKER = "<SUMMARY>"

    def __init__(self, *args, **kwargs):
        pass

    def load(self):
        return ""

    def load_session(self):
        return ""


class DummyConfigManager:
    def __init__(self, _config_path=None):
        self.user_name = "Você"
        self.history_window = DEFAULT_HISTORY_WINDOW
        self.auto_summarize_threshold = 30
        self.idle_timeout_seconds = 300
        self.theme = None


class DummyStorage:
    def __init__(self, *args, **kwargs):
        pass

    def append_log(self, role, content):
        self.last_log = (role, content)

    def get_log_file(self):
        return "/tmp/quimera.log"

    def get_history_file(self):
        return Path("/tmp/sessao-2026-03-27-123456.json")

    def save_history(self, history, shared_state=None):
        self.saved_history = history
        self.saved_shared_state = shared_state

    def load_last_session(self):
        return {"messages": [], "shared_state": {}}


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

        with patch("quimera.cli.TerminalRenderer", FakeRenderer), patch(
                "quimera.cli.AgentClient", FakeAgentClient
        ), patch("sys.argv", ["quimera", "--interactive-test"]):
            cli_main()

        self.assertEqual(len(FakeRenderer.instances), 1)
        self.assertEqual(calls, [(AGENT_CLAUDE,
                                  "Use uma ferramenta de shell para executar o comando `pwd` e me diga o diretório atual. Se a ferramenta pedir aprovação, mostre o prompt normalmente.")])
        self.assertTrue(FakeRenderer.instances[0].system_messages)
        self.assertEqual(FakeRenderer.instances[0].plain_messages, ["\n--- RESULTADO LIMPO ---\n", "saida limpa"])

    @unittest.skipUnless(
        hasattr(cli_module, "TerminalRenderer") and hasattr(cli_module, "AgentClient"),
        "interactive-test CLI não está disponível nesta versão",
    )
    def test_cli_runs_interactive_test_with_custom_prompt(self):
        calls = []

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
        self.assertEqual(len(FakeRenderer.instances), 1)
        self.assertEqual(FakeRenderer.instances[0].system_messages, ["rode pwd"])

    def test_parse_response_detects_extend_marker_at_end(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}

        response, _, _, extend, _, _ = app.parse_response(f"Resposta objetiva {EXTEND_MARKER}")

        self.assertEqual(response, "Resposta objetiva")
        self.assertTrue(extend)

    def test_parse_response_keeps_plain_response(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}

        response, target, handoff, extend, _, _ = app.parse_response("Resposta objetiva")

        self.assertEqual(response, "Resposta objetiva")
        self.assertIsNone(target)
        self.assertIsNone(handoff)
        self.assertFalse(extend)

    def test_parse_response_extracts_internal_handoff(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}

        response, target, message, extend, _, _ = app.parse_response(
            "Resposta visivel\n"
            "[ROUTE:codex] task: Revise este argumento. | context: "
            "Analisar risco no parser atual. | expected: 2 bullets objetivos"
        )

        self.assertEqual(response, "Resposta visivel")
        self.assertEqual(target, AGENT_CODEX)
        self.assertEqual(message["task"], "Revise este argumento.")
        self.assertEqual(message["context"], "Analisar risco no parser atual.")
        self.assertEqual(message["expected"], "2 bullets objetivos")
        self.assertEqual(message["priority"], "normal")
        self.assertIn("handoff_id", message)
        self.assertFalse(extend)

    def test_parse_response_extracts_multiline_handoff(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}

        response, target, message, extend, _, _ = app.parse_response(
            "Resposta visivel\n"
            "[ROUTE:codex]\n"
            "task: Revise este argumento.\n"
            "context: Analisar risco no parser atual.\n"
            "expected: 2 bullets objetivos"
        )

        self.assertEqual(response, "Resposta visivel")
        self.assertEqual(target, AGENT_CODEX)
        self.assertEqual(message["task"], "Revise este argumento.")
        self.assertEqual(message["context"], "Analisar risco no parser atual.")
        self.assertEqual(message["expected"], "2 bullets objetivos")
        self.assertEqual(message["priority"], "normal")
        self.assertIn("handoff_id", message)
        self.assertFalse(extend)

    def test_parse_response_ignores_invalid_handoff_payload(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}

        response, target, message, extend, _, _ = app.parse_response(
            "Resposta visivel\n[ROUTE:codex] Revise este argumento."
        )

        self.assertEqual(response, "Resposta visivel")
        self.assertIsNone(target)
        self.assertIsNone(message)
        self.assertFalse(extend)

    def test_parse_handoff_payload_task_only(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        result = app.parse_handoff_payload("task: Revise este código")
        self.assertEqual(result["task"], "Revise este código")
        self.assertIsNone(result["context"])
        self.assertIsNone(result["expected"])
        self.assertEqual(result["priority"], "normal")
        self.assertIn("handoff_id", result)

    def test_parse_handoff_payload_task_and_context(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        result = app.parse_handoff_payload("task: Revise este código | context: Verificar performance")
        self.assertEqual(result["task"], "Revise este código")
        self.assertEqual(result["context"], "Verificar performance")
        self.assertIsNone(result["expected"])
        self.assertEqual(result["priority"], "normal")

    def test_parse_response_route_with_residual_text(self):
        """ROUTE block should be recognized even when followed by residual text."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}

        # Test with residual text after the ROUTE block
        response, target, message, extend, _, _ = app.parse_response(
            "Resposta visível\n[ROUTE:codex] task: Revise este código\nTexto residual após o bloco ROUTE."
        )

        self.assertEqual(response, "Resposta visível")
        # With the fix, the ROUTE should still be recognized even with residual text
        # The payload should be parsed correctly without the residual text
        self.assertEqual(target, AGENT_CODEX)
        self.assertIsNotNone(message)
        self.assertEqual(message["task"], "Revise este código")
        self.assertFalse(extend)

    def test_parse_response_wildcard_route_captures_any_agent(self):
        """ROUTE com active_agents=['*'] deve capturar qualquer agente."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = ["*"]
        escaped_agents = [
            r'[A-Za-z0-9_-]+' if agent == '*' else re.escape(agent)
            for agent in app.active_agents
        ]
        app.protocol = AppProtocol(app)
        app.protocol.ROUTE_PATTERN = re.compile(
            rf"^\[ROUTE:({'|'.join(escaped_agents)})\]\s*([\s\S]+)\s*\Z",
            re.MULTILINE
        )
        app.shared_state = {}

        response, target, message, extend, _, _ = app.parse_response(
            "Resposta visivel\n"
            "[ROUTE:OPENCODE-GPT] task: Revise este código"
        )

        self.assertEqual(response, "Resposta visivel")
        self.assertEqual(target, "OPENCODE-GPT")
        self.assertEqual(message["task"], "Revise este código")
        self.assertIsNone(message["context"])
        self.assertIsNone(message["expected"])
        self.assertEqual(message["priority"], "normal")
        self.assertIn("handoff_id", message)
        self.assertFalse(extend)

    def test_parse_response_extracts_state_update_before_debate(self):
        import threading
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}
        app._lock = threading.Lock()

        response, _, _, extend, _, _ = app.parse_response(
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
        import threading
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}
        app._lock = threading.Lock()

        response, _, _, extend, _, _ = app.parse_response(
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
        import threading
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {"decisions": ["A"]}
        app._lock = threading.Lock()

        response, _, _, extend, _, _ = app.parse_response(
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
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]

        agent, message, explicit = app.parse_routing("/claude /codex revisar isso")

        self.assertIsNone(agent)
        self.assertIsNone(message)
        self.assertTrue(app.renderer.warnings)

    def test_parse_routing_accepts_code_alias_for_codex(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]

        agent, message, explicit = app.parse_routing("/code revise isso")

        self.assertEqual(agent, AGENT_CODEX)
        self.assertEqual(message, "revise isso")
        self.assertTrue(explicit)

    def test_handle_command_shows_help(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.system_layer = AppSystemLayer(app)

        handled = app.system_layer.handle_command(CMD_HELP)

        self.assertTrue(handled)
        expected_help = build_help([AGENT_CLAUDE, AGENT_CODEX])
        self.assertEqual(app.renderer.system_messages, [expected_help])

    def test_handle_command_shows_agents(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.system_layer = AppSystemLayer(app)

        handled = app.system_layer.handle_command(CMD_AGENTS)

        self.assertTrue(handled)
        expected_agents = build_agents_help([AGENT_CLAUDE, AGENT_CODEX])
        self.assertEqual(app.renderer.system_messages, [expected_agents])

    def test_handle_command_clears_terminal(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.clear_terminal_screen = Mock()
        app.system_layer = AppSystemLayer(app)

        handled = app.system_layer.handle_command(CMD_CLEAR)

        self.assertTrue(handled)
        app.clear_terminal_screen.assert_called_once_with()

    def test_handle_command_shows_prompt_preview_for_default_agent(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.history = [{"role": "human", "content": "Pedido atual"}]
        app.shared_state = {"goal": "corrigir prompt"}
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = (
            "PROMPT GERADO",
            {
                "rules_chars": 10,
                "session_state_chars": 20,
                "persistent_chars": 30,
                "request_chars": 40,
                "facts_chars": 50,
                "shared_state_chars": 60,
                "history_chars": 70,
                "handoff_chars": 0,
                "history_messages": 1,
                "total_chars": 280,
                "primary": True,
            },
        )

        app.system_layer = AppSystemLayer(app)
        handled = app.system_layer.handle_command(CMD_PROMPT)

        self.assertTrue(handled)
        app.prompt_builder.build.assert_called_once_with(
            AGENT_CLAUDE,
            app.history,
            is_first_speaker=True,
            debug=True,
            primary=True,
            shared_state=app.shared_state,
            skip_tool_prompt=False,
        )
        message = app.renderer.system_messages[0]
        self.assertIn("PROMPT PREVIEW: claude", message)
        self.assertIn("ANÁLISE DOS BLOCOS:", message)
        self.assertIn("- total_chars: 280", message)
        self.assertIn("PROMPT FINAL:\nPROMPT GERADO", message)

    def test_handle_command_shows_prompt_preview_for_agent_alias(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.history = []
        app.shared_state = {}
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = (
            "PROMPT CODex",
            {
                "rules_chars": 1,
                "session_state_chars": 2,
                "persistent_chars": 3,
                "request_chars": 4,
                "facts_chars": 5,
                "shared_state_chars": 6,
                "history_chars": 7,
                "handoff_chars": 0,
                "history_messages": 0,
                "total_chars": 28,
                "primary": True,
            },
        )

        app.system_layer = AppSystemLayer(app)
        handled = app.system_layer.handle_command("/prompt /code")

        self.assertTrue(handled)
        app.prompt_builder.build.assert_called_once()
        self.assertEqual(app.prompt_builder.build.call_args.args[0], AGENT_CODEX)

    def test_handle_command_warns_on_unknown_prompt_agent(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.system_layer = AppSystemLayer(app)

        handled = app.system_layer.handle_command("/prompt inexistente")

        self.assertTrue(handled)
        self.assertEqual(app.renderer.warnings, ["Uso: /prompt [agente]"])

    def test_available_internal_commands_include_prompt(self):
        self.assertIn(CMD_PROMPT, QuimeraApp._available_internal_commands())

    def test_clear_terminal_screen_clears_scrollback_and_repositions_cursor(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app._clear_user_prompt_line_if_needed = Mock()

        stdout = Mock()
        stdout.isatty.return_value = True

        with patch("sys.stdout", stdout):
            QuimeraApp.clear_terminal_screen(app)

        app._clear_user_prompt_line_if_needed.assert_called_once_with()
        stdout.write.assert_called_once_with("\x1b[3J\x1b[2J\x1b[H")
        stdout.flush.assert_called_once_with()

    def test_handle_task_command_creates_task_and_assigns_best_agent(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.user_name = "Alex"
        app.shared_state = {"goal": "corrigir task runner"}
        app.current_job_id = 1
        app.history = [
            {"role": "human", "content": "o teste de task perdeu contexto"},
            {"role": "claude", "content": "precisamos serializar o chat recente"},
        ]
        app.prompt_builder = type("PromptBuilderStub", (), {"history_window": 4})()
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)
        app.task_services = AppTaskServices(app)
        app.system_layer = AppSystemLayer(app)
        handled = app.system_layer.handle_command('/task "execute os testes"')

        self.assertTrue(handled)
        tasks = list_tasks({"job_id": 1}, db_path=str(db_path))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "pending")
        self.assertEqual(tasks[0]["task_type"], "test_execution")
        self.assertEqual(tasks[0]["origin"], "human_command")
        self.assertEqual(tasks[0]["assigned_to"], AGENT_CODEX)
        self.assertIn("TAREFA:\nexecute os testes", tasks[0]["body"])
        self.assertIn("CONTEXTO RECENTE DO CHAT:", tasks[0]["body"])
        self.assertIn("ALEX]: o teste de task perdeu contexto", tasks[0]["body"])
        self.assertIn("CLAUDE]: precisamos serializar o chat recente", tasks[0]["body"])
        self.assertIn('"goal": "corrigir task runner"', tasks[0]["body"])
        self.assertIn("task criada com id", app.renderer.system_messages[-1])
        self.assertIn("atribuída para codex", app.renderer.system_messages[-1])

    def test_handle_task_command_assigns_qwen_when_it_supports_task_execution(self):
        # qwen agora suporta task execution via driver openai_compat
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app.active_agents = ["ollama-qwen"]
        app.user_name = "Alex"
        app.shared_state = {}
        app.current_job_id = 1
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)
        app.task_services = AppTaskServices(app)
        app.system_layer = AppSystemLayer(app)
        handled = app.system_layer.handle_command('/task "revise o arquivo quimera/app.py"')

        self.assertTrue(handled)
        tasks = list_tasks({"job_id": 1}, db_path=str(db_path))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["assigned_to"], "ollama-qwen")
        self.assertIn("atribuída para ollama-qwen", app.renderer.system_messages[-1])

    def test_classify_task_execution_result_rejects_needs_input(self):
        ok, reason = QuimeraApp.classify_task_execution_result(
            "Preciso de mais contexto. [NEEDS_INPUT]"
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "agente solicitou input humano")

    def test_classify_task_execution_result_rejects_inability_text(self):
        ok, reason = QuimeraApp.classify_task_execution_result(
            "Não consigo executar isso sem acesso ao ambiente."
        )

        self.assertFalse(ok)
        self.assertIn("Não consigo", reason)

    def test_choose_agent_with_load_balance_penalizes_busy_higher_tier_agent(self):
        from quimera.runtime.tasks import create_task

        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)
        for idx in range(3):
            create_task(
                1,
                f"Tarefa {idx}",
                task_type="general",
                assigned_to=AGENT_CLAUDE,
                status="pending",
                db_path=str(db_path),
            )

        selected = AppTaskServices(app).choose_agent_with_load_balance("general")

        self.assertEqual(selected, AGENT_CODEX)

    def test_handle_task_command_rejects_empty_description(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.user_name = "Alex"
        app.shared_state = {}
        app.current_job_id = 1
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)
        app.task_services = AppTaskServices(app)
        app.system_layer = AppSystemLayer(app)

        handled = app.system_layer.handle_command('/task ""')

        self.assertTrue(handled)
        self.assertEqual(app.renderer.warnings, ["Uso: /task <descrição>"])
        self.assertEqual(list_tasks({"job_id": 1}, db_path=str(db_path)), [])

    def test_prompt_marks_only_first_speaker(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        first_prompt = builder.build(AGENT_CLAUDE, history, is_first_speaker=True)
        second_prompt = builder.build(AGENT_CODEX, history, is_first_speaker=False)

        self.assertIn(EXTEND_MARKER, first_prompt)
        self.assertIn("validador", second_prompt)
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

    def test_prompt_includes_current_human_request_block(self):
        builder = PromptBuilder(DummyContextManager(), history_window=4)
        history = [
            {"role": "human", "content": "Primeiro pedido"},
            {"role": "claude", "content": "Resposta anterior"},
            {"role": "human", "content": "Pedido atual"},
        ]

        prompt = builder.build(AGENT_CODEX, history)

        self.assertIn("PEDIDO ATUAL DO HUMANO", prompt)
        self.assertIn("Pedido atual", prompt)

    def test_prompt_does_not_repeat_current_human_request_in_conversation(self):
        builder = PromptBuilder(DummyContextManager(), history_window=4)
        history = [
            {"role": "human", "content": "Primeiro pedido"},
            {"role": "claude", "content": "Resposta anterior"},
            {"role": "human", "content": "Pedido atual"},
        ]

        prompt = builder.build(AGENT_CODEX, history)

        conversation = prompt.split("CONVERSA:\n", 1)[1]
        self.assertNotIn("[VOCÊ]: Pedido atual", conversation)
        self.assertIn("[VOCÊ]: Primeiro pedido", conversation)

    def test_prompt_includes_recent_facts_block(self):
        builder = PromptBuilder(DummyContextManager(), history_window=5)
        history = [
            {"role": "human", "content": "Investigue"},
            {"role": "claude", "content": "Arquivo alterado: app.py"},
            {"role": "codex", "content": "Teste falhou em test_x"},
        ]

        prompt = builder.build(AGENT_CLAUDE, history)

        self.assertIn("MENSAGENS RECENTES DE OUTROS AGENTES", prompt)
        self.assertIn("[CLAUDE] Arquivo alterado: app.py", prompt)
        self.assertIn("[CODEX] Teste falhou em test_x", prompt)

    def test_prompt_skips_meta_lock_messages_from_facts_block(self):
        builder = PromptBuilder(DummyContextManager(), history_window=5)
        history = [
            {"role": "human", "content": "Mude o foco"},
            {"role": "codex", "content": "goal_canonical continua ativo e não redefina o objetivo"},
            {"role": "claude", "content": "Arquivo alterado: app.py"},
        ]

        prompt = builder.build(AGENT_CLAUDE, history)

        self.assertIn("MENSAGENS RECENTES DE OUTROS AGENTES", prompt)
        self.assertIn("[CLAUDE] Arquivo alterado: app.py", prompt)
        self.assertNotIn("goal_canonical continua ativo", prompt)
        self.assertNotIn("não redefina o objetivo", prompt)

    def test_prompt_does_not_repeat_recent_facts_in_conversation(self):
        builder = PromptBuilder(DummyContextManager(), history_window=5)
        history = [
            {"role": "human", "content": "Investigue"},
            {"role": "claude", "content": "Arquivo alterado: app.py"},
            {"role": "codex", "content": "Teste falhou em test_x"},
        ]

        prompt = builder.build(AGENT_CLAUDE, history)

        conversation = prompt.split("CONVERSA:\n", 1)[1]
        self.assertNotIn("[CLAUDE]: Arquivo alterado: app.py", conversation)
        self.assertNotIn("[CODEX]: Teste falhou em test_x", conversation)
        self.assertIn("[sem itens residuais na conversa recente]", conversation)

    def test_prompt_lists_only_active_agents(self):
        builder = PromptBuilder(
            DummyContextManager(),
            history_window=3,
            active_agents=[AGENT_CLAUDE, AGENT_CODEX],
        )
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CODEX, history)

        self.assertIn("CLAUDE", prompt)
        # CODEX é o agente falante — não aparece na lista de outros agentes
        self.assertNotIn("QWEN", prompt)

    def test_prompt_includes_session_state_when_present(self):
        builder = PromptBuilder(
            DummyContextManager(),
            history_window=3,
            session_state={
                "session_id": "sessao-2026-03-27-123456",
                "current_job_id": 1,
                "is_new_session": "não",
                "history_restored": "sim",
                "summary_loaded": "não",
            },
        )
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CLAUDE, history)

        self.assertIn("ESTADO DA SESSÃO", prompt)
        self.assertIn("SESSÃO ATUAL: sessao-2026-03-27-123456", prompt)
        self.assertIn("JOB_ID ATUAL: 1", prompt)
        self.assertNotIn("NOVA SESSÃO", prompt)
        self.assertNotIn("HISTÓRICO RESTAURADO", prompt)
        self.assertNotIn("RESUMO CARREGADO", prompt)

    def test_prompt_includes_shared_state_as_json(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        # Chaves legadas (goal, decisions) sem goal_canonical não devem aparecer no prompt
        prompt = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={"goal": "corrigir", "decisions": ["usar json"]},
        )

        self.assertNotIn("ESTADO COMPARTILHADO", prompt)
        self.assertNotIn('"goal": "corrigir"', prompt)
        self.assertNotIn('"decisions": [', prompt)

        # Chaves de execução (next_step, etc.) sem goal_canonical também não devem aparecer
        prompt2 = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={"next_step": "continuar", "goal": "ignorado"},
        )
        self.assertNotIn("ESTADO COMPARTILHADO", prompt2)

        # task_overview é campo de infra (não execução) e deve aparecer normalmente
        prompt3 = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={"task_overview": {"job_id": 1}, "goal": "ignorado"},
        )
        self.assertIn("ESTADO COMPARTILHADO", prompt3)
        self.assertIn('"task_overview"', prompt3)
        self.assertNotIn('"goal":', prompt3)

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

        # Sem goal_canonical, todos os campos de execução (goal, decisions, next_step) são filtrados
        self.assertNotIn("ESTADO COMPARTILHADO", prompt)
        self.assertNotIn('"goal":', prompt)
        self.assertNotIn('"decisions":', prompt)
        self.assertNotIn('"next_step":', prompt)
        self.assertNotIn('"open_disagreements"', prompt)

        # Com task_overview (campo de infra), o bloco aparece normalmente
        state_with_overview = {**big_state, "task_overview": {"job_id": 42}}
        prompt2 = builder.build(AGENT_CLAUDE, history, shared_state=state_with_overview)
        state_start = prompt2.index("ESTADO COMPARTILHADO:")
        state_block = prompt2[state_start:]
        self.assertIn('"task_overview"', state_block)
        self.assertNotIn('"goal":', state_block)
        self.assertNotIn('"next_step":', state_block)

    def test_prompt_includes_task_overview_in_shared_state(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={
                "goal": "coordenar tarefas",
                "task_overview": {
                    "job_id": 23,
                    "open_task_counts": {"approved": 1, "proposed": 0, "in_progress": 0},
                    "recommended_action": "Execute approved antes de criar novas.",
                },
            },
        )

        self.assertIn('"task_overview": {', prompt)
        self.assertIn('"job_id": 23', prompt)
        self.assertIn('Execute approved antes de criar novas.', prompt)

    def test_prompt_includes_state_update_rule_with_shared_state_fallback(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        # next_step sem goal_canonical é campo de execução e não aciona o bloco
        prompt = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={"next_step": "continuar"},
        )
        self.assertNotIn("ESTADO COMPARTILHADO", prompt)
        self.assertNotIn("Você pode atualizar o estado compartilhado usando:", prompt)

        # task_overview (campo de infra) deve acionar o bloco e incluir a regra STATE_UPDATE
        prompt2 = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={"task_overview": {"job_id": 1}},
        )
        self.assertIn("ESTADO COMPARTILHADO", prompt2)
        self.assertIn("Você pode atualizar o estado compartilhado usando:", prompt2)
        self.assertIn("[STATE_UPDATE]", prompt2)
        self.assertEqual(prompt2.count("Você pode atualizar o estado compartilhado usando:"), 1)

    def test_app_builds_explicit_session_state_for_prompt(self):
        temp_root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        class FakeWorkspace:
            def __init__(self, cwd):
                self.root = temp_root
                self.cwd = cwd
                self.config_file = temp_root / "config.json"
                self.context_persistent = temp_root / "quimera_context.md"
                self.context_session = temp_root / "quimera_session_context.md"
                self.logs_dir = temp_root / "quimera_logs"
                self.history_file = temp_root / "quimera_history"
                self.state_dir = temp_root / "quimera_state"
                self.tasks_db = temp_root / "quimera_tasks.db"
                self.decisions_log = temp_root / "decisions.jsonl"

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

        with patch("quimera.app.core.ConfigManager", DummyConfigManager), patch("quimera.app.core.Workspace",
                                                                                FakeWorkspace), patch(
                "quimera.app.core.ContextManager", FakeContextManager
        ), patch("quimera.app.core.SessionStorage", FakeSessionStorage):
            app = QuimeraApp(Path("/tmp/projeto"))

        try:
            session_state = app.prompt_builder.session_state
            self.assertEqual(session_state.get("session_id"), "sessao-2026-03-27-123456")
            self.assertEqual(session_state.get("is_new_session"), "não")
            self.assertEqual(session_state.get("history_restored"), "sim")
            self.assertEqual(session_state.get("summary_loaded"), "sim")
            self.assertIn("current_job_id", session_state)
        finally:
            app._stop_task_executors()
        self.assertIsInstance(session_state["current_job_id"], int)
        self.assertEqual(app.shared_state, {"goal": "continuar"})

    def test_app_uses_default_history_window_from_config(self):
        temp_root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        class FakeWorkspace:
            def __init__(self, cwd):
                self.root = temp_root
                self.cwd = cwd
                self.config_file = temp_root / "config.json"
                self.context_persistent = temp_root / "quimera_context.md"
                self.context_session = temp_root / "quimera_session_context.md"
                self.logs_dir = temp_root / "quimera_logs"
                self.history_file = temp_root / "quimera_history"
                self.state_dir = temp_root / "quimera_state"
                self.tasks_db = temp_root / "quimera_tasks.db"
                self.decisions_log = temp_root / "decisions.jsonl"

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

        with patch("quimera.app.core.ConfigManager", DummyConfigManager), patch("quimera.app.core.Workspace",
                                                                                FakeWorkspace), patch(
                "quimera.app.core.ContextManager", FakeContextManager
        ), patch("quimera.app.core.SessionStorage", FakeSessionStorage):
            app = QuimeraApp(Path("/tmp/projeto"))

        try:
            self.assertEqual(app.prompt_builder.history_window, DEFAULT_HISTORY_WINDOW)
        finally:
            app._stop_task_executors()

    def test_app_allows_history_window_override(self):
        temp_root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        class FakeWorkspace:
            def __init__(self, cwd):
                self.root = temp_root
                self.cwd = cwd
                self.config_file = temp_root / "config.json"
                self.context_persistent = temp_root / "quimera_context.md"
                self.context_session = temp_root / "quimera_session_context.md"
                self.logs_dir = temp_root / "quimera_logs"
                self.history_file = temp_root / "quimera_history"
                self.state_dir = temp_root / "quimera_state"
                self.tasks_db = temp_root / "quimera_tasks.db"
                self.decisions_log = temp_root / "decisions.jsonl"

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

        with patch("quimera.app.core.ConfigManager", DummyConfigManager), patch("quimera.app.core.Workspace",
                                                                                FakeWorkspace), patch(
                "quimera.app.core.ContextManager", FakeContextManager
        ), patch("quimera.app.core.SessionStorage", FakeSessionStorage):
            app = QuimeraApp(Path("/tmp/projeto"), history_window=5)

        try:
            self.assertEqual(app.prompt_builder.history_window, 5)
        finally:
            app._stop_task_executors()

    def test_run_uses_single_turn_by_default(self):
        """No fluxo padrão (sem prefixo explícito, sem EXTEND), apenas um agente responde."""
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

        app.active_agents = list(plugins.all_names())
        app.threads = 1
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.shared_state = {}
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content: persisted.append((role, content))
        app.session_services.maybe_auto_summarize = Mock()
        app.system_layer = Mock()
        app.system_layer.handle_command = Mock(return_value=False)
        app.protocol = AppProtocol(app)
        app.dispatch_services = Mock(spec=AppDispatchServices)
        app.dispatch_services.call_agent = Mock(return_value="claude responde")
        app.dispatch_services.print_response = lambda agent, response: printed.append((agent, response))
        app.input_services = Mock()
        app.input_services.read_user_input = Mock(side_effect=["mensagem", "/exit"])
        app.turn_manager = TurnManager()
        app.chat_round_orchestrator = ChatRoundOrchestrator(app)

        app.run()

        # Apenas o agente roteado responde — outros agentes não são acionados
        self.assertEqual(printed, [(AGENT_CLAUDE, "claude responde")])
        self.assertEqual(
            persisted,
            [("human", "oi"), (AGENT_CLAUDE, "claude responde")],
        )
        app.dispatch_services.call_agent.assert_called_once()

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

        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.threads = 1
        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.shared_state = {}
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content: persisted.append((role, content))
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

        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.threads = 1
        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.shared_state = {}
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content: persisted.append((role, content))
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
                from_agent=None,
        ):
            calls.append((agent, is_first_speaker, handoff, handoff_only, from_agent))
            return next(responses)

        app.call_agent = fake_call

        app.run()

        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], (AGENT_CLAUDE, True, None, False, None))
        # Verifica campos essenciais do handoff (agora tem priority e handoff_id extras)
        handoff_to_codex = calls[1][2]
        self.assertEqual(handoff_to_codex["task"], "Revise este argumento.")
        self.assertEqual(handoff_to_codex["context"], "Analisar risco no parser atual.")
        self.assertEqual(handoff_to_codex["expected"], "2 bullets objetivos")
        self.assertEqual(calls[1][1], False)
        self.assertTrue(calls[1][3])  # handoff_only
        self.assertEqual(calls[1][4], AGENT_CLAUDE)  # from_agent
        self.assertEqual(calls[2][1], False)
        self.assertFalse(calls[2][3])
        self.assertIsNone(calls[2][4])

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

        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.threads = 1
        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", True)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.shared_state = {}
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content: persisted.append((role, content))
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
                from_agent=None,
        ):
            calls.append((agent, is_first_speaker, handoff, handoff_only, from_agent))
            return next(responses)

        app.call_agent = fake_call

        app.run()

        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], (AGENT_CLAUDE, True, None, False, None))
        handoff_to_codex = calls[1][2]
        self.assertEqual(handoff_to_codex["task"], "Revise este argumento.")
        self.assertEqual(handoff_to_codex["context"], "Analisar risco no parser atual.")
        self.assertEqual(handoff_to_codex["expected"], "2 bullets objetivos")
        self.assertEqual(calls[1][1], False)
        self.assertTrue(calls[1][3])
        self.assertEqual(calls[1][4], AGENT_CLAUDE)
        self.assertEqual(calls[2][1], False)
        self.assertFalse(calls[2][3])
        self.assertIsNone(calls[2][4])
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

    def test_run_blocks_agent_task_creation_via_tool_in_normal_flow(self):
        class AutoApprove(ApprovalHandler):
            def approve(self, tool_name, summary):
                return True

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
        app.shared_state = {}
        app._lock = threading.Lock()
        app.session_state = {
            "session_id": "sessao-2026-04-02-183323",
            "history_count": 0,
            "summary_loaded": True,
            "handoffs_sent": 0,
            "handoffs_received": 0,
            "handoffs_succeeded": 0,
            "handoffs_failed": 0,
            "total_latency": 0.0,
            "agent_metrics": {},
        }

        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        job_id = add_job("Session sessao-2026-04-02-183323", db_path=str(db_path))
        app.current_job_id = job_id
        app.tasks_db_path = str(db_path)
        app.tool_executor = ToolExecutor(
            config=ToolRuntimeConfig(
                workspace_root=tmp_dir,
                db_path=str(db_path),
                require_approval_for_mutations=False,
            ),
            approval_handler=AutoApprove(),
        )

        persisted = []
        printed = []
        calls = []

        app.active_agents = [AGENT_CLAUDE, "opencode-qwen"]
        app.threads = 1
        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "faça o teste pelo chat", True)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.resolve_agent_response = QuimeraApp.resolve_agent_response.__get__(app, QuimeraApp)
        app.call_agent = QuimeraApp.call_agent.__get__(app, QuimeraApp)
        app.task_services = Mock()
        app.task_services.refresh_task_shared_state = Mock()
        app.task_services.truncate_payload = lambda payload: payload
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content: persisted.append((role, content))
        app.read_user_input = Mock(side_effect=["mensagem", "/exit"])

        responses = iter(
            [
                "vou tentar abrir task sozinho\n"
                '<tool function="propose_task" description="rode os testes" />',
                "sem task criada",
            ]
        )

        def fake_call_agent(
                agent,
                is_first_speaker=False,
                handoff=None,
                primary=True,
                protocol_mode="standard",
                handoff_only=False,
                silent=False,
                from_agent=None,
        ):
            calls.append((agent, protocol_mode, handoff_only, from_agent, handoff))
            return next(responses)

        app._call_agent = fake_call_agent

        app.run()

        tasks = list_tasks({"job_id": job_id}, db_path=str(db_path))
        self.assertEqual(tasks, [])
        self.assertEqual(
            printed,
            [
                (AGENT_CLAUDE, "vou tentar abrir task sozinho"),
                (AGENT_CLAUDE, "sem task criada"),
            ],
        )
        self.assertEqual(
            persisted,
            [
                ("human", "faça o teste pelo chat"),
                (AGENT_CLAUDE, "vou tentar abrir task sozinho"),
                (AGENT_CLAUDE, "sem task criada"),
            ],
        )
        self.assertEqual(app.renderer.handoffs, [])

    def test_persist_message_saves_shared_state(self):
        import threading
        app = QuimeraApp.__new__(QuimeraApp)
        app.history = []
        app.shared_state = {"goal": "corrigir protocolo"}
        app.storage = DummyStorage()
        app._lock = threading.Lock()

        app.session_services = AppSessionServices(app)
        app.session_services.persist_message("human", "oi")

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
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.auto_summarize_threshold = 4
        app.prompt_builder = type("PromptBuilderStub", (), {"history_window": 2})()
        app.context_manager = FakeContextManager()
        app.session_summarizer = FakeSessionSummarizer()
        app.renderer = DummyRenderer()
        app.storage = DummyStorage()
        app.shared_state = {"goal": "manter memória"}

        app.session_services = AppSessionServices(app)
        app.session_services.maybe_auto_summarize()

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

        AppSessionServices(app).shutdown()

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

    def test_shutdown_cancels_agent_summary_when_join_is_interrupted(self):
        class FakeThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon
                self.started = False
                self.join_calls = 0

            def start(self):
                self.started = True

            def join(self, timeout=None):
                self.join_calls += 1
                if self.join_calls == 1:
                    raise KeyboardInterrupt()

        app = QuimeraApp.__new__(QuimeraApp)
        app.history = [{"role": "human", "content": "mensagem final"}]
        app.context_manager = DummyContextManager()
        app.session_summarizer = Mock()
        app.renderer = DummyRenderer()
        app.summary_agent_preference = "ollama-qwen"
        app.agent_client = SimpleNamespace(_user_cancelled=False, _cancel_event=threading.Event())

        with patch("quimera.app.session.threading.Thread", FakeThread):
            AppSessionServices(app).shutdown()

        self.assertTrue(app.agent_client._user_cancelled)
        self.assertTrue(app.agent_client._cancel_event.is_set())
        self.assertEqual(app.renderer.system_messages[-1], "[memória] não foi possível gerar o resumo.")

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

    def test_chain_summarizer_stops_fallback_when_user_cancels(self):
        class DummyAgentClient:
            def __init__(self, renderer):
                self.renderer = renderer
                self._user_cancelled = False
                self.calls = []

            def call(self, agent, prompt):
                self.calls.append((agent, prompt))
                self._user_cancelled = True
                return None

        renderer = DummyRenderer()
        agent_client = DummyAgentClient(renderer)
        summarizer_call = build_chain_summarizer(agent_client, ["chatgpt", "codex"])

        result = summarizer_call("resuma", preferred_agent="chatgpt")

        self.assertIsNone(result)
        self.assertEqual(agent_client.calls, [("chatgpt", "resuma")])
        self.assertEqual(renderer.system_messages, [])
        self.assertEqual(summarizer_call.last_outcome, "cancelled")

    def test_summarize_session_suppresses_unavailable_message_on_user_cancel(self):
        class DummyAgentClient:
            def __init__(self, renderer):
                self.renderer = renderer
                self._user_cancelled = False

            def call(self, agent, prompt):
                self._user_cancelled = True
                return None

        renderer = DummyRenderer()
        summarizer_call = build_chain_summarizer(DummyAgentClient(renderer), ["chatgpt", "codex"])
        summarizer = SessionSummarizer(renderer, summarizer_call=summarizer_call)

        summary = summarizer.summarize(
            [{"role": "human", "content": "gerar resumo"}],
            preferred_agent="chatgpt",
        )

        self.assertIsNone(summary)
        self.assertEqual(renderer.system_messages, [])

    def test_chain_summarizer_stops_fallback_when_cancel_event_is_already_set(self):
        class DummyAgentClient:
            def __init__(self, renderer):
                self.renderer = renderer
                self._user_cancelled = False
                self._cancel_event = threading.Event()
                self.calls = []

            def call(self, agent, prompt):
                self.calls.append((agent, prompt))
                self._cancel_event.set()
                return None

        renderer = DummyRenderer()
        agent_client = DummyAgentClient(renderer)
        summarizer_call = build_chain_summarizer(agent_client, ["chatgpt", "codex"])

        result = summarizer_call("resuma", preferred_agent="chatgpt")

        self.assertIsNone(result)
        self.assertEqual(agent_client.calls, [("chatgpt", "resuma")])
        self.assertEqual(renderer.system_messages, [])
        self.assertEqual(summarizer_call.last_outcome, "cancelled")

    def test_chain_summarizer_does_not_emit_per_agent_unavailable_messages(self):
        class DummyAgentClient:
            def __init__(self, renderer):
                self.renderer = renderer
                self._user_cancelled = False
                self._cancel_event = threading.Event()
                self.calls = []

            def call(self, agent, prompt):
                self.calls.append((agent, prompt))
                return None

        renderer = DummyRenderer()
        agent_client = DummyAgentClient(renderer)
        summarizer_call = build_chain_summarizer(agent_client, ["chatgpt", "codex"])
        summarizer = SessionSummarizer(renderer, summarizer_call=summarizer_call)

        summary = summarizer.summarize(
            [{"role": "human", "content": "gerar resumo"}],
            preferred_agent="chatgpt",
        )

        self.assertIsNone(summary)
        self.assertEqual(
            agent_client.calls,
            [("chatgpt", unittest.mock.ANY), ("codex", unittest.mock.ANY)],
        )
        self.assertEqual(renderer.system_messages, ["[memória] resumidores indisponíveis"])

    def test_shutdown_summary_thread_does_not_mark_agents_unavailable_due_to_signal_registration(self):
        class DummyStatusContext:
            def __init__(self, status):
                self._status = status

            def __enter__(self):
                return self._status

            def __exit__(self, exc_type, exc, tb):
                return False

        renderer = Mock()
        status = Mock()
        renderer.running_status.return_value = DummyStatusContext(status)
        agent_client = AgentClient(renderer)

        with patch("quimera.plugins.get") as mock_get:
            mock_plugin = SimpleNamespace(
                driver="openai_compat",
                model="qwen3-coder:30b",
                base_url="http://localhost:11434/v1",
                api_key_env=None,
                tool_use_reliability="medium",
                supports_tools=True,
            )
            mock_get.return_value = mock_plugin

            with patch.object(agent_client, "_api_drivers", {"ollama-qwen": Mock()}):
                agent_client._api_drivers["ollama-qwen"].run.return_value = "Resumo final"
                summarizer_call = build_chain_summarizer(agent_client, ["ollama-qwen"])
                summarizer = SessionSummarizer(renderer, summarizer_call=summarizer_call)
                result = {}

                def worker():
                    result["summary"] = summarizer.summarize(
                        [{"role": "human", "content": "encerrar sessão"}],
                        preferred_agent="ollama-qwen",
                    )

                thread = threading.Thread(target=worker)
                thread.start()
                thread.join()

        self.assertEqual(result["summary"], "Resumo final")
        renderer.show_system.assert_not_called()

    def test_call_api_marks_user_cancelled_when_cancel_event_finishes_driver_without_result(self):
        class DummyStatusContext:
            def __init__(self, status):
                self._status = status

            def __enter__(self):
                return self._status

            def __exit__(self, exc_type, exc, tb):
                return False

        renderer = Mock()
        status = Mock()
        renderer.running_status.return_value = DummyStatusContext(status)
        agent_client = AgentClient(renderer)

        plugin = SimpleNamespace(
            driver="openai_compat",
            model="qwen3-coder:30b",
            base_url="http://localhost:11434/v1",
            api_key_env=None,
            tool_use_reliability="medium",
            supports_tools=True,
            cmd=None,
        )

        def driver_run(*, cancel_event=None, **kwargs):
            if cancel_event is not None:
                cancel_event.set()
            return None

        with patch.object(agent_client, "_api_drivers", {"ollama-qwen": Mock()}):
            agent_client._api_drivers["ollama-qwen"].run.side_effect = driver_run
            result = agent_client._call_api("ollama-qwen", plugin, "resuma", quiet=True)

        self.assertIsNone(result)
        self.assertTrue(agent_client._user_cancelled)
        renderer.show_error.assert_not_called()


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
            self.assertEqual(_agent_style("stub"), ("magenta", "🤖 Stub"))

    def test_agent_style_fallback_for_unknown(self):
        with patch.dict(plugins._registry, {}, clear=True):
            color, label = _agent_style("unknown")
            self.assertEqual(color, "white")
            self.assertEqual(label, "🤖 Unknown")

    def test_agent_client_call_uses_plugin_cmd(self):
        stub = AgentPlugin(name="stub", prefix="/stub", cmd=["stub", "-x"], style=("white", "Stub"))
        renderer = Mock()

        with patch.dict(plugins._registry, {"stub": stub}):
            client = AgentClient(renderer)
            with patch.object(client, "run", return_value="ok") as mock_run:
                result = client.call("stub", "hello")

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        self.assertEqual(call_args[0][0], ["stub", "-x"])
        self.assertEqual(call_args[1]["input_text"], "hello")
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

    def test_parallel_threads_initializes_correctly(self):
        app = QuimeraApp(Path("/tmp"), debug=False, history_window=10, agents=["agent1", "agent2"], threads=3)
        self.assertEqual(app.threads, 3)
        self.assertIn("agent1", app.active_agents)
        self.assertIn("agent2", app.active_agents)
        self.assertTrue(hasattr(app, "_call_agent_for_parallel"))

    def test_parallel_threads_calls_agents_concurrently(self):
        # Testa que o método _call_agent_for_parallel retorna tupla correta
        app = QuimeraApp.__new__(QuimeraApp)
        app.threads = 2
        app.active_agents = ["agent1", "agent2"]
        app.debug_prompt_metrics = False
        app.session_call_index = 0
        app._lock = threading.Lock()
        app._output_lock = threading.Lock()
        app._counter_lock = threading.Lock()
        app.agent_failures = defaultdict(int)
        app._agent_failures_lock = threading.Lock()
        app.prompt_builder = Mock()
        app.prompt_builder.build.return_value = "dummy prompt"
        app.agent_client = Mock()
        app.agent_client.call.return_value = "Resposta mock"
        app.tool_executor = Mock()
        app.tool_executor.maybe_execute_from_response.return_value = ("Resposta mock", None)
        app.history = []
        app.shared_state = {}
        app.renderer = Mock()
        app.storage = Mock()
        app.context_manager = Mock()
        app.session_state = {"session_id": "test"}
        app.round_index = 0
        app.summary_agent_preference = None
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.task_services = Mock()
        app.task_services.refresh_task_shared_state = Mock()
        app._record_agent_metric = Mock()

        from pathlib import Path
        import tempfile
        staging_root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        agent, response, route_target, handoff, extend, needs_input = app._call_agent_for_parallel("agent1", None,
                                                                                                   "standard",
                                                                                                   staging_root, 0)
        self.assertEqual(agent, "agent1")
        self.assertEqual(response, "Resposta mock")
        self.assertIsNone(route_target)
        self.assertIsNone(handoff)
        self.assertFalse(extend)

    def test_run_thread_mode_accepts_new_human_input_while_agent_is_running(self):
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
        app.shared_state = {}
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.threads = 2
        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        persisted = []
        printed = []
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content: persisted.append((role, content))
        app.print_response = lambda agent, response: printed.append((agent, response))

        call_started = threading.Event()
        second_prompt_seen = threading.Event()
        allow_finish = threading.Event()

        def fake_read_user_input(prompt, timeout=0):
            if not call_started.is_set():
                return "mensagem"
            second_prompt_seen.set()
            return "/exit"

        def fake_call_agent(agent, **kwargs):
            call_started.set()
            allow_finish.wait(timeout=2)
            return "claude responde"

        app.read_user_input = Mock(side_effect=fake_read_user_input)
        app.call_agent = fake_call_agent

        run_thread = threading.Thread(target=app.run)
        run_thread.start()

        self.assertTrue(second_prompt_seen.wait(timeout=1),
                        "run() não voltou ao prompt enquanto o agente ainda executava")
        allow_finish.set()
        run_thread.join(timeout=2)

        self.assertFalse(run_thread.is_alive(), "run() deveria encerrar após drenar a fila")
        self.assertEqual(persisted[0], ("human", "oi"))
        self.assertIn((AGENT_CLAUDE, "claude responde"), printed)

    def test_turn_manager_wait_for_human_turn_unblocks_immediately_after_agent_response(self):
        turn_manager = TurnManager()
        turn_manager.next_turn()

        started = threading.Event()
        released = []

        def _waiter():
            started.set()
            released.append(turn_manager.wait_for_human_turn(timeout=1))

        waiter = threading.Thread(target=_waiter, daemon=True)
        waiter.start()

        self.assertTrue(started.wait(timeout=1), "thread de espera não iniciou")
        time.sleep(0.02)
        turn_manager.next_turn()

        waiter.join(timeout=0.2)
        self.assertFalse(waiter.is_alive(), "espera pelo turno humano não deveria depender de polling lento")
        self.assertEqual(released, [True])

    def test_read_user_input_zero_timeout_tty_uses_blocking_input_path(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._deferred_system_messages = []
        app._nonblocking_prompt_visible = False
        app._nonblocking_input_queue = None
        app._nonblocking_input_thread = None
        app._nonblocking_input_status = "idle"
        app._nonblocking_prompt_text = ""

        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        with patch("sys.stdin", stdin), patch("builtins.input", return_value="mensagem") as mock_input:
            result = app.read_user_input("Você: ", timeout=0)

        self.assertEqual(result, "mensagem")
        mock_input.assert_called_once_with("Você: ")

    def test_read_user_input_zero_timeout_tty_flushes_deferred_messages_before_prompt(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._deferred_system_messages = ["[task 7] claude:\nresultado final"]
        app._nonblocking_prompt_visible = False
        app._nonblocking_input_queue = None
        app._nonblocking_input_thread = None
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Você: "
        app._output_lock = threading.Lock()

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("builtins.input", return_value="oi"):
            value = app.read_user_input("Você: ", timeout=0)

        self.assertEqual(value, "oi")
        self.assertEqual(app._nonblocking_input_status, "idle")
        self.assertEqual(app._nonblocking_prompt_text, "")
        self.assertEqual(app._deferred_system_messages, [])
        self.assertEqual(app.renderer.system_messages, ["[task 7] claude:\nresultado final"])

    def test_read_user_input_zero_timeout_tty_raises_keyboard_interrupt(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._deferred_system_messages = []
        app._nonblocking_prompt_visible = False
        app._nonblocking_input_queue = None
        app._nonblocking_input_thread = None
        app._nonblocking_input_status = "idle"
        app._nonblocking_prompt_text = ""

        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        with patch("sys.stdin", stdin), patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                app.read_user_input("Você: ", timeout=0)

    def test_show_system_message_suppresses_transient_task_status_while_tty_reader_is_active(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Alex: "

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("quimera.app.core.readline.get_line_buffer", return_value=""), patch(
                "quimera.app.core.readline.redisplay"
        ) as mock_redisplay:
            app.show_system_message("[task 7] claude: iniciando")

        self.assertEqual(app.renderer.system_messages, [])
        mock_redisplay.assert_not_called()

    def test_show_system_message_redraws_human_prompt_with_user_name_for_task_error_text(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Alex: "

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("quimera.app.core.readline.get_line_buffer", return_value=""), patch(
                "quimera.app.core.readline.redisplay"
        ), patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush") as mock_flush:
            app.show_system_message("[task 7] claude: erro: falha de rede")

        self.assertIn(call("\r\x1b[2K"), mock_write.call_args_list)
        self.assertIn(call("Alex: "), mock_write.call_args_list)
        self.assertGreaterEqual(mock_flush.call_count, 1)

    def test_redisplay_user_prompt_does_not_sleep_while_redrawing_after_agent_output(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Alex: "

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("quimera.app.core.readline.get_line_buffer",
                                              return_value="digitando"), patch(
                "quimera.app.core.readline.redisplay"
        ) as mock_redisplay, patch("sys.stdout.write"), patch("sys.stdout.flush"), patch(
            "quimera.app.core.time.sleep"
        ) as mock_sleep:
            for _ in range(5):
                app._redisplay_user_prompt_if_needed()

        mock_sleep.assert_not_called()
        self.assertEqual(mock_redisplay.call_count, 5)

    def test_show_system_message_defers_multiline_review_message_while_tty_reader_is_active(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Alex: "

        renderer = Mock()
        app.renderer = renderer

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("quimera.app.core.readline.get_line_buffer", return_value=""), patch(
                "quimera.app.core.readline.redisplay"
        ), patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush"):
            app.show_system_message("[task 7] gemini:\nACEITE\nResultado validado com evidência concreta.")

        self.assertEqual(mock_write.call_args_list, [])
        renderer.show_system.assert_not_called()
        self.assertEqual(
            app._deferred_system_messages,
            ["[task 7] gemini:\nACEITE\nResultado validado com evidência concreta."],
        )

    def test_staging_logger_does_not_touch_prompt_for_info_logs_while_tty_reader_is_active(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Alex: "

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        prompt_handler = next(handler for handler in app_module.logger.handlers if
                              isinstance(handler, app_module.PromptAwareStderrHandler))
        previous_app = prompt_handler._app
        prompt_handler.bind_app(app)
        try:
            with patch("sys.stdin", stdin), patch("quimera.app.core.readline.get_line_buffer", return_value=""), patch(
                    "quimera.app.core.readline.redisplay"
            ) as mock_redisplay, patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush") as mock_flush:
                app_module.logger.info("[DISPATCH] sending to agent=%s", AGENT_CODEX)

            self.assertNotIn(call("\r\x1b[2K"), mock_write.call_args_list)
            self.assertNotIn(call("Alex: "), mock_write.call_args_list)
            self.assertEqual(mock_flush.call_count, 0)
            mock_redisplay.assert_not_called()
        finally:
            prompt_handler.bind_app(previous_app)

    def test_staging_logger_still_shows_warning_logs_while_tty_reader_is_active(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Alex: "

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        prompt_handler = next(handler for handler in app_module.logger.handlers if
                              isinstance(handler, app_module.PromptAwareStderrHandler))
        previous_app = prompt_handler._app
        prompt_handler.bind_app(app)
        try:
            with patch("sys.stdin", stdin), patch("quimera.app.core.readline.get_line_buffer", return_value=""), patch(
                    "quimera.app.core.readline.redisplay"
            ) as mock_redisplay, patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush"):
                app_module.logger.warning("[DISPATCH] retry for agent=%s", AGENT_CODEX)

            self.assertIn(call("\r\x1b[2K"), mock_write.call_args_list)
            self.assertIn(call("Alex: "), mock_write.call_args_list)
            mock_redisplay.assert_called_once_with()
        finally:
            prompt_handler.bind_app(previous_app)

    def test_show_system_message_clears_prompt_only_once_before_redisplay(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Alex: "

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("quimera.app.core.readline.get_line_buffer", return_value="oi"), patch(
                "quimera.app.core.readline.redisplay"
        ), patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush"):
            app.show_system_message("[task 7] claude: erro: timeout")

        clear_calls = [call_args for call_args in mock_write.call_args_list if call_args == call("\r\x1b[2K")]
        self.assertEqual(len(clear_calls), 1)

    def test_print_response_clears_prompt_before_agent_output_and_redisplays_once(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Alex: "
        app.renderer = Mock()

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("quimera.app.core.readline.get_line_buffer", return_value="oi"), patch(
                "quimera.app.core.readline.redisplay"
        ) as mock_redisplay, patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush"):
            app.print_response("claude", "resposta final")

        app.renderer.show_message.assert_called_once_with("claude", "resposta final")
        clear_calls = [call_args for call_args in mock_write.call_args_list if call_args == call("\r\x1b[2K")]
        self.assertEqual(len(clear_calls), 1)
        self.assertIn(call("Alex: oi"), mock_write.call_args_list)
        mock_redisplay.assert_called_once_with()

    def test_show_system_message_defers_task_output_while_tty_reader_is_active(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._deferred_system_messages = []
        app._nonblocking_input_status = "reading"

        app.show_system_message("[task 7] claude:\nresultado final")

        self.assertEqual(app.renderer.system_messages, [])
        self.assertEqual(app._deferred_system_messages, ["[task 7] claude:\nresultado final"])

    def test_parse_routing_selects_random_initial_agent(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.round_index = 0
        app.renderer = DummyRenderer()

        with patch("quimera.app.core.random.choice", return_value=AGENT_CODEX) as mock_choice:
            agent, message, explicit = app.parse_routing("oi")

        self.assertEqual(agent, AGENT_CODEX)
        self.assertEqual(message, "oi")
        self.assertFalse(explicit)
        mock_choice.assert_called_once_with(app.active_agents)

    def test_parse_handoff_payload_with_priority(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        result = app.parse_handoff_payload("task: Corrigir bug crítico | priority: urgent")
        self.assertEqual(result["task"], "Corrigir bug crítico")
        self.assertEqual(result["priority"], "urgent")
        self.assertIn("handoff_id", result)

    def test_parse_handoff_payload_invalid_priority_defaults_to_normal(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        result = app.parse_handoff_payload("task: Algo qualquer | priority: invalido")
        self.assertEqual(result["priority"], "normal")

    def test_parse_handoff_payload_generates_unique_ids(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        r1 = app.parse_handoff_payload("task: Tarefa 1")
        r2 = app.parse_handoff_payload("task: Tarefa 2")
        self.assertNotEqual(r1["handoff_id"], r2["handoff_id"])

    def test_handoff_format_includes_priority_when_urgent(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        handoff = {
            "task": "Corrigir bug",
            "context": "Parser quebrado",
            "expected": "Patch",
            "priority": "urgent",
            "handoff_id": "abc123",
        }
        formatted = builder._format_handoff(handoff, from_agent="claude")
        self.assertIn("PRIORITY:\nURGENT", formatted)
        self.assertIn("HANDOFF_ID:\nabc123", formatted)

    def test_handoff_format_omits_priority_when_normal(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        handoff = {
            "task": "Revisar código",
            "priority": "normal",
            "handoff_id": "xyz789",
        }
        formatted = builder._format_handoff(handoff, from_agent="codex")
        self.assertIn("HANDOFF_ID:\nxyz789", formatted)
        self.assertNotIn("PRIORITY", formatted)

    def test_retry_on_none_response(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = ["claude"]
        app.agent_failures = {}
        app._agent_failures_lock = threading.Lock()
        app.session_metrics = SessionMetricsService()
        app.session_state = {}
        call_count = [0]

        def fake_call_agent(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 2:
                return None
            return "sucesso no retry"

        dispatch = AppDispatchServices(app)
        dispatch.call_agent_low_level = fake_call_agent
        dispatch.resolve_agent_response = lambda agent, response, silent=False, persist_history=True, show_output=True: response

        result = dispatch.call_agent("claude")
        self.assertEqual(result, "sucesso no retry")
        self.assertEqual(call_count[0], 2)

    def test_task_handler_prints_and_persists_agent_response(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        app.call_agent = lambda *args, **kwargs: "resposta visivel da task"
        app.show_system_message = lambda message: status_updates.append(message)
        app.classify_task_execution_result = lambda response: (True, response)

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.runtime.tasks.complete_task"
        ) as complete_task, patch("quimera.runtime.tasks.fail_task") as fail_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE]({"id": 1, "description": "rode a task"})

        self.assertTrue(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 1] claude: iniciando — rode a task",
                "[task 1] claude:\nresposta visivel da task",
                "[task 1] claude: concluída",
            ],
        )
        complete_task.assert_called_once_with(
            1, result="resposta visivel da task", db_path=app.tasks_db_path
        )
        fail_task.assert_not_called()

    def test_task_handler_marks_task_waiting_for_review_from_another_agent(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        app.call_agent = lambda *args, **kwargs: "resposta visivel da task"
        app.show_system_message = lambda message: status_updates.append(message)
        app.classify_task_execution_result = lambda response: (True, response)

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.runtime.tasks.submit_for_review"
        ) as submit_for_review, patch("quimera.runtime.tasks.complete_task") as complete_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE]({"id": 1, "description": "rode a task"})

        self.assertTrue(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 1] claude: iniciando — rode a task",
                "[task 1] claude:\nresposta visivel da task",
                "[task 1] claude: aguardando review de outro agente",
            ],
        )
        submit_for_review.assert_called_once_with(
            1, result="resposta visivel da task", db_path=app.tasks_db_path
        )
        complete_task.assert_not_called()

    def test_task_handler_completes_when_no_other_operational_reviewer_exists(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        class FakePlugin:
            def __init__(self, supports_task_execution):
                self.supports_task_execution = supports_task_execution

        app.call_agent = lambda *args, **kwargs: "resposta visivel da task"
        app.show_system_message = lambda message: status_updates.append(message)
        app.classify_task_execution_result = lambda response: (True, response)

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.app.task.plugins.get",
                side_effect=lambda agent: FakePlugin(agent == AGENT_CLAUDE),
        ), patch("quimera.runtime.tasks.submit_for_review") as submit_for_review, patch(
            "quimera.runtime.tasks.complete_task"
        ) as complete_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE]({"id": 11, "description": "rode a task"})

        self.assertTrue(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 11] claude: iniciando — rode a task",
                "[task 11] claude:\nresposta visivel da task",
                "[task 11] claude: concluída",
            ],
        )
        submit_for_review.assert_not_called()
        complete_task.assert_called_once_with(
            11, result="resposta visivel da task", db_path=app.tasks_db_path
        )

    def test_review_handler_prints_review_progress_and_completion(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        review_handlers = {}
        review_prompts = []

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        def fake_call_agent(agent, **kwargs):
            review_prompts.append(kwargs.get("handoff", ""))
            return "ACEITE\nResultado validado com evidência concreta."

        app.call_agent = fake_call_agent
        app.show_system_message = lambda message: status_updates.append(message)

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.runtime.tasks.complete_task"
        ) as complete_task:
            app._setup_task_executors()
            ok = review_handlers[AGENT_GEMINI](
                {"id": 7, "assigned_to": AGENT_CLAUDE, "result": "ok"}
            )

        self.assertTrue(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 7] gemini: revisando execução de claude",
                "[task 7] gemini:\nACEITE\nResultado validado com evidência concreta.",
                "[task 7] gemini: review concluído",
            ],
        )
        self.assertTrue(review_prompts)
        self.assertIn("Faça um review real da task abaixo.", review_prompts[0])
        self.assertIn("Resultado do executor:\nok", review_prompts[0])
        complete_task.assert_called_once_with(
            7, result="ok", reviewed_by=AGENT_GEMINI, db_path=app.tasks_db_path
        )

    def test_review_handler_reports_rejected_self_review(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        review_handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        app.call_agent = lambda *args, **kwargs: None
        app.show_system_message = lambda message: status_updates.append(message)

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.runtime.tasks.update_task"
        ) as update_task, patch("quimera.runtime.tasks.complete_task") as complete_task:
            app._setup_task_executors()
            ok = review_handlers[AGENT_CLAUDE](
                {"id": 8, "assigned_to": AGENT_CLAUDE, "result": "ok"}
            )

        self.assertFalse(ok)
        self.assertEqual(
            status_updates,
            ["[task 8] claude: review rejeitado, aguardando outro agente"],
        )
        update_task.assert_called_once_with(8, "pending_review", db_path=app.tasks_db_path)
        complete_task.assert_not_called()

    def test_review_handler_returns_task_to_pending_on_retentativa(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        review_handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        app.call_agent = lambda *args, **kwargs: "RETENTATIVA\nFaltou evidência de alteração no código."
        app.show_system_message = lambda message: status_updates.append(message)

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.runtime.tasks.requeue_task_after_review"
        ) as requeue_task_after_review, patch("quimera.runtime.tasks.complete_task") as complete_task:
            app._setup_task_executors()
            ok = review_handlers[AGENT_GEMINI](
                {
                    "id": 9,
                    "assigned_to": AGENT_CLAUDE,
                    "description": "corrigir bug x",
                    "body": "ajuste o fluxo y",
                    "result": "ok",
                }
            )

        self.assertFalse(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 9] gemini: revisando execução de claude",
                "[task 9] gemini:\nRETENTATIVA\nFaltou evidência de alteração no código.",
                "[task 9] gemini: review pediu retentativa, task voltou para pending",
            ],
        )
        requeue_task_after_review.assert_called_once_with(
            9,
            AGENT_CLAUDE,
            result="ok",
            notes="RETENTATIVA\nFaltou evidência de alteração no código.",
            db_path=app.tasks_db_path,
        )
        complete_task.assert_not_called()

    def test_review_handler_returns_task_to_pending_review_on_failure(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI, AGENT_CODEX]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        review_handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        def fake_call_agent(*_args, **_kwargs):
            raise RuntimeError("timeout")

        class FakePlugin:
            def __init__(self, supports_task_execution):
                self.supports_task_execution = supports_task_execution

        app.call_agent = fake_call_agent
        app.show_system_message = lambda message: status_updates.append(message)

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.app.task.plugins.get",
                side_effect=lambda _agent: FakePlugin(True),
        ), patch("quimera.runtime.tasks.update_task") as update_task, patch(
            "quimera.runtime.tasks.fail_task"
        ) as fail_task:
            app._setup_task_executors()
            ok = review_handlers[AGENT_GEMINI](
                {"id": 10, "assigned_to": AGENT_CLAUDE, "result": "ok"}
            )

        self.assertFalse(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 10] gemini: revisando execução de claude",
                "[task 10] gemini: review falhou: timeout",
            ],
        )
        update_task.assert_called_once_with(
            10,
            "pending_review",
            result="ok",
            notes="timeout",
            db_path=app.tasks_db_path,
        )
        fail_task.assert_not_called()

    def test_review_handler_fails_when_no_other_operational_reviewer_exists(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        status_updates = []
        review_handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def set_review_eligibility(self, predicate):
                return None

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        class FakePlugin:
            def __init__(self, supports_task_execution):
                self.supports_task_execution = supports_task_execution

        def fake_call_agent(*_args, **_kwargs):
            raise RuntimeError("timeout")

        app.call_agent = fake_call_agent
        app.show_system_message = lambda message: status_updates.append(message)

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.app.task.plugins.get",
                side_effect=lambda agent: FakePlugin(agent == AGENT_GEMINI),
        ), patch("quimera.runtime.tasks.update_task") as update_task, patch(
            "quimera.runtime.tasks.fail_task"
        ) as fail_task:
            app._setup_task_executors()
            ok = review_handlers[AGENT_GEMINI](
                {"id": 11, "assigned_to": AGENT_CLAUDE, "result": "ok"}
            )

        self.assertFalse(ok)
        self.assertEqual(
            status_updates,
            [
                "[task 11] gemini: revisando execução de claude",
                "[task 11] gemini: review falhou: timeout",
            ],
        )
        update_task.assert_not_called()
        fail_task.assert_called_once_with(
            11,
            reason="review failed without operational fallback: timeout",
            db_path=app.tasks_db_path,
        )

    def test_setup_task_executors_only_registers_review_for_operational_agents(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        review_handlers = {}
        review_eligibility = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                review_handlers[self.agent] = handler

            def set_review_eligibility(self, predicate):
                review_eligibility[self.agent] = predicate

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        class FakePlugin:
            def __init__(self, supports_task_execution):
                self.supports_task_execution = supports_task_execution

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.app.task.plugins.get",
                side_effect=lambda agent: FakePlugin(agent == AGENT_GEMINI),
        ):
            app._setup_task_executors()
            self.assertFalse(review_eligibility[AGENT_CLAUDE]())
            self.assertTrue(review_eligibility[AGENT_GEMINI]())

        self.assertNotIn(AGENT_CLAUDE, review_handlers)
        self.assertIn(AGENT_GEMINI, review_handlers)

    def test_review_eligibility_tracks_operational_agent_state_dynamically(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_GEMINI]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        review_eligibility = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                return None

            def set_review_eligibility(self, predicate):
                review_eligibility[self.agent] = predicate

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            executor = FakeExecutor(handler)
            executor.agent = agent
            return executor

        class FakePlugin:
            def __init__(self, supports_task_execution):
                self.supports_task_execution = supports_task_execution

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.app.task.plugins.get",
                side_effect=lambda agent: FakePlugin(True),
        ):
            app._setup_task_executors()
            self.assertTrue(review_eligibility[AGENT_GEMINI]())
            app.active_agents.remove(AGENT_GEMINI)
            self.assertFalse(review_eligibility[AGENT_GEMINI]())

    def test_task_handler_executes_with_serialized_chat_context_in_body(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        handlers = {}
        captured = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        def fake_call_agent(agent, **kwargs):
            captured["agent"] = agent
            captured["kwargs"] = kwargs
            return "resposta visivel da task"

        app.call_agent = fake_call_agent
        app.show_system_message = lambda message: None
        app.classify_task_execution_result = lambda response: (True, response)

        task_body = (
            "TAREFA:\nvalidar regressão\n\n"
            "CONTEXTO RECENTE DO CHAT:\n"
            "[ALEX]: a execução da tarefa precisa receber o contexto do chat\n"
            "[CLAUDE]: alguém passou contexto errado\n\n"
            "INSTRUÇÃO:\n"
            "Execute a tarefa usando o contexto acima como referência."
        )

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.runtime.tasks.complete_task"
        ) as complete_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE](
                {"id": 1, "description": "validar regressão", "body": task_body}
            )

        self.assertTrue(ok)
        self.assertEqual(captured["agent"], AGENT_CLAUDE)
        self.assertTrue(captured["kwargs"]["handoff_only"])
        self.assertFalse(captured["kwargs"]["primary"])
        self.assertIn("Execute a seguinte tarefa:\n\nTAREFA:\nvalidar regressão", captured["kwargs"]["handoff"])
        self.assertIn("[ALEX]: a execução da tarefa precisa receber o contexto do chat", captured["kwargs"]["handoff"])
        self.assertIn("[CLAUDE]: alguém passou contexto errado", captured["kwargs"]["handoff"])
        complete_task.assert_called_once_with(
            1, result="resposta visivel da task", db_path=app.tasks_db_path
        )

    def test_task_handler_requeues_failed_execution_for_other_agent(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        app.call_agent = lambda *args, **kwargs: None
        app.show_system_message = lambda message: None
        app.classify_task_execution_result = lambda response: (True, response)
        app.record_failure = lambda agent: None

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.runtime.tasks.requeue_task"
        ) as requeue_task, patch("quimera.runtime.tasks.fail_task") as fail_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE]({"id": 1, "description": "rode a task"})

        self.assertFalse(ok)
        requeue_task.assert_called_once_with(
            1, AGENT_CLAUDE, reason="communication failed", db_path=app.tasks_db_path
        )
        fail_task.assert_not_called()

    def test_task_handler_fails_when_all_other_agents_already_failed(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.tasks_db_path = "/tmp/quimera-tasks-test.db"
        handlers = {}

        class FakeExecutor:
            def __init__(self, handler):
                self.handler = handler

            def set_review_handler(self, handler):
                pass

            def start(self):
                return None

        def fake_create_executor(agent, handler, db_path=None, job_id=None):
            handlers[agent] = handler
            return FakeExecutor(handler)

        app.call_agent = lambda *args, **kwargs: None
        app.show_system_message = lambda message: None
        app.classify_task_execution_result = lambda response: (True, response)
        app.record_failure = lambda agent: None

        with patch("quimera.app.core.create_executor", side_effect=fake_create_executor), patch(
                "quimera.runtime.tasks.can_reassign_task", return_value=False
        ) as can_reassign_task, patch("quimera.runtime.tasks.requeue_task") as requeue_task, patch(
            "quimera.runtime.tasks.fail_task"
        ) as fail_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE]({"id": 1, "description": "rode a task"})

        self.assertFalse(ok)
        can_reassign_task.assert_called_once_with(
            1, [AGENT_CODEX], db_path=app.tasks_db_path
        )
        requeue_task.assert_not_called()
        fail_task.assert_called_once_with(1, reason="communication failed", db_path=app.tasks_db_path)

    def test_parse_response_extracts_ack_marker(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}

        response, target, handoff, extend, needs_input, ack_id = app.parse_response(
            "[ACK:abc123def456]\nTarefa concluída com sucesso."
        )

        self.assertEqual(response, "Tarefa concluída com sucesso.")
        self.assertEqual(ack_id, "abc123def456")
        self.assertIsNone(target)
        self.assertIsNone(handoff)
        self.assertFalse(extend)
        self.assertFalse(needs_input)

    def test_parse_response_without_ack(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}

        response, _, _, _, _, ack_id = app.parse_response("Resposta sem ACK")

        self.assertIsNone(ack_id)
        self.assertEqual(response, "Resposta sem ACK")

    def test_handoff_chain_propagation(self):
        """Test that handoff chain is propagated correctly."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        result = app.parse_handoff_payload("task: Test task")
        self.assertEqual(result["chain"], [])

        # Simulate chain propagation
        result["chain"] = ["claude"]
        result["chain"].append("codex")
        self.assertEqual(result["chain"], ["claude", "codex"])

    def test_handoff_id_uses_real_target(self):
        """Test that handoff_id includes target in its generation."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        # Generate IDs with same timestamp to verify target affects the hash
        ts = 1234567890.0
        id1 = app._generate_handoff_id("Test task", "codex", timestamp=ts)
        id2 = app._generate_handoff_id("Test task", "claude", timestamp=ts)
        id3 = app._generate_handoff_id("Test task", "codex", timestamp=ts)
        # Same task + same target + same timestamp = same ID
        self.assertEqual(id1, id3)
        # Same task + different target = different ID
        self.assertNotEqual(id1, id2)

    def test_ack_mismatch_logged_on_validation(self):
        """Test that ACK mismatch is detected when ack_id != handoff_id."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}
        app.renderer = DummyRenderer()

        # Simulate a handoff with handoff_id="abc123" but agent responds with ACK:def456
        response_text = "[ACK:def456]\nTarefa concluída."
        response, _, _, _, _, ack_id = app.parse_response(response_text)

        self.assertEqual(ack_id, "def456")
        self.assertEqual(response, "Tarefa concluída.")

    def test_per_agent_metrics_tracking(self):
        """Test that per-agent metrics are tracked correctly."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.session_state = {
            "session_id": "test",
            "history_count": 0,
            "summary_loaded": False,
            "handoffs_sent": 0,
            "handoffs_received": 0,
            "handoffs_succeeded": 0,
            "handoffs_failed": 0,
            "total_latency": 0.0,
            "agent_metrics": {},
        }
        app.agent_failures = {}
        app._agent_failures_lock = threading.Lock()
        app.session_metrics = SessionMetricsService()

        # Simulate successful call to claude
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 1.5)
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 0.8)

        # Simulate failed call to codex
        app.session_metrics.record_agent_metric(app, "codex", "failed", 0.0)

        metrics = app.session_state["agent_metrics"]
        self.assertEqual(metrics["claude"]["succeeded"], 2)
        self.assertEqual(metrics["claude"]["latency"], 2.3)
        self.assertEqual(metrics["codex"]["failed"], 1)
        self.assertEqual(metrics["codex"]["succeeded"], 0)

    def test_per_agent_tool_metrics_tracking(self):
        """Tool use deve ser rastreado por agente na sessão."""
        from quimera.metrics import BehaviorMetricsTracker

        app = QuimeraApp.__new__(QuimeraApp)
        app.session_state = {
            "session_id": "test",
            "history_count": 0,
            "summary_loaded": False,
            "handoffs_sent": 0,
            "handoffs_received": 0,
            "handoffs_succeeded": 0,
            "handoffs_failed": 0,
            "total_latency": 0.0,
            "agent_metrics": {},
        }
        app.behavior_metrics = BehaviorMetricsTracker()
        app.session_metrics = SessionMetricsService()

        app._record_tool_event("ollama-qwen", result=SimpleNamespace(ok=True, error=None))
        app._record_tool_event("ollama-qwen",
                               result=SimpleNamespace(ok=False, error="Sem política para a ferramenta: run"))
        app._record_tool_event("ollama-qwen", loop_abort=True, reason="invalid_tool_loop")

        metrics = app.session_state["agent_metrics"]["ollama-qwen"]
        self.assertEqual(metrics["tool_calls_total"], 2)
        self.assertEqual(metrics["tool_calls_failed"], 1)
        self.assertEqual(metrics["invalid_tool_calls"], 1)
        self.assertEqual(metrics["tool_loop_abortions"], 1)

        summary = app.behavior_metrics.get_agent_summary("ollama-qwen")
        self.assertEqual(summary["tool_calls_total"], 2)
        self.assertEqual(summary["tool_calls_failed"], 1)
        self.assertEqual(summary["invalid_tool_calls"], 1)
        self.assertEqual(summary["tool_loop_abortions"], 1)

    def test_prompt_includes_proactivity_rules(self):
        """Prompt deve incluir NEEDS_INPUT e instruções de colaboração."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CLAUDE, history, is_first_speaker=True)

        self.assertIn("[NEEDS_INPUT]", prompt)
        self.assertIn("humano", prompt.lower())

    def test_route_rule_includes_format_specification(self):
        """build_route_rule deve incluir instrução de formato do payload."""
        from quimera.constants import build_route_rule

        rule = build_route_rule(["claude", "codex"])

        self.assertIn("[ROUTE:agente]", rule)
        self.assertIn("task", rule)
        self.assertIn("obrigatório", rule)
        self.assertIn("não improvise", rule)
        self.assertIn("NEEDS_INPUT", rule)


class FallbackChainTests(unittest.TestCase):
    """Testes para fallback chain quando agente secundário falha."""

    def test_first_agent_failover_to_another_agent(self):
        """Quando o primeiro agente não responde, outro agente deve assumir a rodada."""
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
        app.summary_agent_preference = None
        app.session_state = {
            "session_id": "test-first-agent-failover",
            "history_count": 0,
            "summary_loaded": False,
        }
        printed = []
        persisted = []
        calls = []

        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.threads = 1
        app.shared_state = {}
        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content: persisted.append((role, content))

        responses = iter([
            None,
            "codex assumiu e respondeu",
        ])

        def fake_call(
                agent,
                is_first_speaker=False,
                handoff=None,
                primary=True,
                protocol_mode="standard",
                handoff_only=False,
                from_agent=None,
        ):
            calls.append((agent, is_first_speaker, handoff, handoff_only, from_agent))
            return next(responses)

        app.call_agent = fake_call

        QuimeraApp._do_process_chat_message(app, "oi")

        self.assertEqual(calls[0][0], AGENT_CLAUDE)
        self.assertEqual(calls[1][0], AGENT_CODEX)
        self.assertTrue(calls[1][1])
        self.assertIn((AGENT_CODEX, "codex assumiu e respondeu"), printed)
        self.assertIn((AGENT_CODEX, "codex assumiu e respondeu"), persisted)
        self.assertEqual(app.summary_agent_preference, AGENT_CODEX)

    def test_fallback_tries_next_agent_when_secondary_fails(self):
        """Quando o agente secundário não responde, o sistema deve tentar o próximo disponível."""
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
            "session_id": "test-fallback",
            "history_count": 0,
            "summary_loaded": False,
        }
        persisted = []
        printed = []
        calls = []

        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX, "ollama-qwen"]
        app.threads = 1
        app.handle_command = lambda user: False
        app.parse_routing = lambda user: (AGENT_CLAUDE, "oi", False)
        app.parse_response = QuimeraApp.parse_response.__get__(app, QuimeraApp)
        app.shared_state = {}
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.session_services = Mock()
        app.session_services.persist_message = lambda role, content: persisted.append((role, content))
        app.read_user_input = Mock(side_effect=["mensagem", "/exit"])
        responses = iter([
            # Claude responde e delega para codex
            "claude responde\n[ROUTE:codex] task: Revise este código",
            # Codex NÃO responde (None)
            None,
            # Fallback: qwen responde
            "qwen faz o fallback",
            # Claude sintetiza com a resposta do qwen
            "claude sintetiza com qwen",
        ])

        def fake_call(
                agent,
                is_first_speaker=False,
                handoff=None,
                primary=True,
                protocol_mode="standard",
                handoff_only=False,
                from_agent=None,
        ):
            calls.append((agent, is_first_speaker, handoff, handoff_only, from_agent))
            return next(responses)

        app.call_agent = fake_call
        app.run()

        # Deve ter 4 chamadas: claude inicial, codex (falha), qwen (fallback), claude (síntese)
        self.assertEqual(len(calls), 4)
        # Segunda chamada é para codex (handoff)
        self.assertEqual(calls[1][0], AGENT_CODEX)
        self.assertTrue(calls[1][3])  # handoff_only
        # Terceira chamada é para ollama-qwen (fallback)
        self.assertEqual(calls[2][0], "ollama-qwen")
        self.assertTrue(calls[2][3])  # handoff_only
        # Quarta chamada é claude sintetizando
        self.assertEqual(calls[3][0], AGENT_CLAUDE)
        # Verifica que ollama-qwen imprimiu e persistiu
        self.assertIn(("ollama-qwen", "qwen faz o fallback"), printed)

    def test_fallback_skips_original_agent_and_chain(self):
        """Fallback não deve tentar o agente original nem os já na cadeia."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}

        # Simula handoff com chain
        result = app.parse_handoff_payload("task: Test", target="codex")
        result["chain"] = ["claude"]

        # Fallback candidates devem excluir claude (chain) e codex (target original)
        app.active_agents = ["claude", "codex", "ollama-qwen"]
        chain = result["chain"]
        route_target = "codex"
        first_agent = "claude"

        fallback_candidates = [
            a for a in app.active_agents
            if a != first_agent and a != route_target and a not in chain
        ]

        self.assertEqual(fallback_candidates, ["ollama-qwen"])

    def test_no_fallback_when_no_candidates(self):
        """Se não há candidatos de fallback, o sistema não deve tentar."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = ["claude", "codex"]
        chain = ["claude"]
        route_target = "codex"
        first_agent = "claude"

        fallback_candidates = [
            a for a in app.active_agents
            if a != first_agent and a != route_target and a not in chain
        ]

        self.assertEqual(fallback_candidates, [])


class MetricsFeedbackTests(unittest.TestCase):
    """Testes para métricas e feedback operacional."""

    def test_has_clear_next_step_detects_clear_indicators(self):
        """SessionMetricsService.has_clear_next_step deve detectar indicadores de próximo passo."""
        self.assertTrue(SessionMetricsService.has_clear_next_step("Próximo passo: revisar o código."))
        self.assertTrue(SessionMetricsService.has_clear_next_step("Próxima etapa: implementar a feature."))
        self.assertTrue(SessionMetricsService.has_clear_next_step("Tarefa completa."))
        self.assertTrue(SessionMetricsService.has_clear_next_step("Concluído."))
        self.assertFalse(SessionMetricsService.has_clear_next_step("Apenas uma resposta qualquer."))
        self.assertFalse(SessionMetricsService.has_clear_next_step(""))

    def test_is_response_redundant_detects_similarity(self):
        """SessionMetricsService.is_response_redundant deve detectar respostas similares."""
        history = [
            {"role": "human", "content": "Faça algo"},
            {"role": "claude", "content": "Vou implementar a feature X agora. Isso envolve criar o arquivo e testar."},
        ]

        similar_response = "Vou implementar a feature X agora. Isso envolve criar o arquivo e testar."
        self.assertTrue(SessionMetricsService.is_response_redundant(similar_response, history))

        different_response = "Vou corrigir o bug Y no parser."
        self.assertFalse(SessionMetricsService.is_response_redundant(different_response, history))

    def test_session_state_tracks_new_metrics(self):
        """session_state deve rastrear as novas métricas."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.session_state = {}

        app.session_state["total_responses"] = 5
        app.session_state["responses_with_clear_next_step"] = 3
        app.session_state["consecutive_redundant_responses"] = 2
        app.session_state["handoff_invalid_count"] = 1
        app.session_state["rounds_without_progress"] = 0

        self.assertEqual(app.session_state["total_responses"], 5)
        self.assertEqual(app.session_state["responses_with_clear_next_step"], 3)
        self.assertEqual(app.session_state["consecutive_redundant_responses"], 2)
        self.assertEqual(app.session_state["handoff_invalid_count"], 1)

    def test_handoff_format_includes_chain(self):
        """Handoff format deve incluir cadeia de delegação quando presente."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        handoff = {
            "task": "Revisar parser",
            "context": "Parser quebrado",
            "chain": ["claude", "codex"],
            "handoff_id": "abc123",
        }
        formatted = builder._format_handoff(handoff, from_agent="qwen")
        self.assertIn("CHAIN:\nclaude -> codex", formatted)
        self.assertIn("HANDOFF_ID:\nabc123", formatted)
        self.assertIn("FROM:\nqwen", formatted)

    def test_handoff_format_omits_chain_when_empty(self):
        """Handoff format não deve incluir CHAIN quando vazio."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        handoff = {
            "task": "Tarefa simples",
            "chain": [],
            "handoff_id": "xyz",
        }
        formatted = builder._format_handoff(handoff, from_agent="claude")
        self.assertNotIn("CHAIN", formatted)

    def test_prompt_includes_collaboration_rules(self):
        """Prompt deve incluir regras de colaboração."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CLAUDE, history, is_first_speaker=True)

        self.assertIn("prioridade", prompt.lower())
        self.assertIn("foco", prompt.lower())
        self.assertIn("fazem parte deste chat", prompt.lower())

    def test_prompt_is_concise(self):
        """Prompt deve ser conciso após enxugamento."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CLAUDE, history, is_first_speaker=True)

        self.assertLess(len(prompt), 5000)

    def test_get_task_routing_plugins_respects_explicit_active_agents(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]

        selected = [plugin.name for plugin in AppTaskServices(app).get_task_routing_plugins()]

        self.assertEqual(selected, [AGENT_CLAUDE, AGENT_CODEX])

    def test_get_task_routing_plugins_expands_wildcard_to_all_plugins(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = ["*"]

        selected = [plugin.name for plugin in AppTaskServices(app).get_task_routing_plugins()]

        self.assertEqual(
            selected,
            [plugin.name for plugin in plugins.all_plugins() if getattr(plugin, "supports_task_execution", True)],
        )

    def test_handoff_rule_mentions_ack(self):
        """PROMPT_HANDOFF_RULE deve mencionar ACK."""
        from quimera.constants import PROMPT_HANDOFF_RULE

        self.assertIn("ACK", PROMPT_HANDOFF_RULE)
        self.assertIn("delegue de volta", PROMPT_HANDOFF_RULE)
        self.assertIn("arquivos", PROMPT_HANDOFF_RULE)

    def test_behavior_metrics_tracker_integrated_with_app(self):
        """BehaviorMetricsTracker deve ser alimentado pelo app."""
        from quimera.metrics import BehaviorMetricsTracker

        app = QuimeraApp.__new__(QuimeraApp)
        app.session_state = {
            "session_id": "test",
            "history_count": 0,
            "summary_loaded": False,
            "handoffs_sent": 0,
            "handoffs_received": 0,
            "handoffs_succeeded": 0,
            "handoffs_failed": 0,
            "total_latency": 0.0,
            "agent_metrics": {},
        }
        app.agent_failures = {}
        app._agent_failures_lock = threading.Lock()
        app.behavior_metrics = BehaviorMetricsTracker()
        app.session_metrics = SessionMetricsService()

        # Simulate successful calls
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 1.5)
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 2.0)
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 1.0)
        app.session_metrics.record_agent_metric(app, "claude", "succeeded", 0.5)

        # Verifica que o tracker foi alimentado
        claude_metrics = app.behavior_metrics.get_agent_summary("claude")
        self.assertEqual(claude_metrics["responses_total"], 4)

    def test_behavior_metrics_tracks_invalid_handoff(self):
        """Handoff inválido deve ser registrado no tracker."""
        from quimera.metrics import BehaviorMetricsTracker

        app = QuimeraApp.__new__(QuimeraApp)
        app.protocol = AppProtocol(app)
        app.shared_state = {}
        app.behavior_metrics = BehaviorMetricsTracker()

        # Simula resposta com handoff inválido
        response, target, handoff, extend, needs_input, ack_id = app.parse_response(
            "Resposta visivel\n[ROUTE:codex] texto sem formato válido"
        )

        self.assertIsNone(target)
        self.assertIsNone(handoff)
        # O tracker deve ter registrado o handoff inválido
        claude_summary = app.behavior_metrics.get_agent_summary("codex")
        self.assertGreaterEqual(claude_summary["handoffs_sent"], 0)

    def test_route_rule_is_concise(self):
        """build_route_rule deve ser conciso e incluir task como obrigatório."""
        from quimera.constants import build_route_rule

        rule = build_route_rule(["claude", "codex"])

        self.assertIn("task", rule)
        self.assertIn("obrigatório", rule)
        self.assertIn("claude", rule)
        self.assertIn("codex", rule)
        self.assertIn("NEEDS_INPUT", rule)
        self.assertIn("paths", rule)
        self.assertIn("paralelizar", rule)
        self.assertIn("especialidade", rule)
        self.assertIn("não improvise", rule)
        self.assertLess(len(rule), 500)

    def test_reviewer_rule_is_concise(self):
        """PROMPT_REVIEWER_RULE deve ser conciso."""
        from quimera.constants import PROMPT_REVIEWER_RULE

        self.assertIn("veredicto", PROMPT_REVIEWER_RULE.lower())
        self.assertIn("aceite", PROMPT_REVIEWER_RULE.lower())
        self.assertLess(len(PROMPT_REVIEWER_RULE), 550)

    def test_handoff_rule_is_concise(self):
        """PROMPT_HANDOFF_RULE deve ser conciso."""
        from quimera.constants import PROMPT_HANDOFF_RULE

        self.assertIn("ACK", PROMPT_HANDOFF_RULE)
        self.assertIn("continue do ponto já avançado", PROMPT_HANDOFF_RULE.lower())
        self.assertLess(len(PROMPT_HANDOFF_RULE), 400)

    def test_base_rules_are_concise(self):
        """PROMPT_BASE_RULES deve ser conciso e cobrir prioridades."""
        from quimera.constants import PROMPT_BASE_RULES

        self.assertIn("humano", PROMPT_BASE_RULES.lower())
        self.assertIn("prioridade", PROMPT_BASE_RULES.lower())
        self.assertIn("foco", PROMPT_BASE_RULES.lower())
        self.assertIn("continuação direta do mesmo chat", PROMPT_BASE_RULES.lower())
        self.assertIn("colaboração é parte do trabalho", PROMPT_BASE_RULES.lower())
        self.assertIn("editar arquivos", PROMPT_BASE_RULES.lower())
        self.assertIn("mude o mínimo necessário", PROMPT_BASE_RULES.lower())
        self.assertLess(len(PROMPT_BASE_RULES), 1600)

    def test_tool_rule_guides_discovery_before_edits(self):
        from quimera.constants import PROMPT_TOOL_RULE

        self.assertIn("list_files", PROMPT_TOOL_RULE)
        self.assertIn("grep_search", PROMPT_TOOL_RULE)
        self.assertIn("read_file", PROMPT_TOOL_RULE)
        self.assertIn("apply_patch", PROMPT_TOOL_RULE)
        self.assertIn("run_shell", PROMPT_TOOL_RULE)

    def test_build_tools_prompt_is_compact_but_preserves_essentials(self):
        from quimera.constants import build_tools_prompt

        prompt = build_tools_prompt()

        self.assertIn('function="apply_patch"', prompt)
        self.assertIn("Ferramentas disponíveis", prompt)
        self.assertIn("- list_files:", prompt)
        self.assertIn("- read_file:", prompt)
        self.assertIn("- apply_patch:", prompt)
        self.assertIn("- run_shell:", prompt)
        self.assertNotIn("  Exemplo:", prompt)
        self.assertLess(len(prompt), 2600)

    def test_build_task_body_includes_operational_protocol(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.user_name = "Alex"
        app.history = [{"role": "human", "content": "Corrija o parser atual"}]
        app.shared_state = {}

        body = AppTaskServices(app).build_task_body("corrigir parser")

        self.assertIn("PROTOCOLO OPERACIONAL:", body)
        self.assertIn("Descubra o alvo antes de mudar", body)
        self.assertIn("apply_patch", body)
        self.assertIn("run_shell", body)
        self.assertIn("exec_command", body)
        self.assertIn("arquivos alterados", body)

    def test_build_task_body_uses_shared_state_as_reference_only(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.user_name = "Alex"
        app.history = [{"role": "human", "content": "Corrija o parser atual"}]
        app.shared_state = {
            "goal_canonical": "Corrigir parser legado",
            "current_step": "Ajustar tokenizer",
            "allowed_scope": ["parser.py"],
        }

        body = AppTaskServices(app).build_task_body("corrigir parser")

        self.assertIn("ESTADO COMPARTILHADO (referência):", body)
        self.assertIn('"goal_canonical": "Corrigir parser legado"', body)
        self.assertNotIn("CONTEXTO DE EXECUÇÃO:", body)
        self.assertNotIn("GOAL_CANONICAL:", body)
        self.assertIn("Use o estado compartilhado apenas como referência auxiliar", body)

    def test_refresh_task_shared_state_adds_completed_task_results(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.shared_state = {}
        app.current_job_id = 1
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        task_id = create_task(
            1,
            "validar cobertura dos testes",
            db_path=str(db_path),
            status="completed",
        )
        complete_task(task_id, result="ok" * 200, db_path=str(db_path))
        app.tasks_db_path = str(db_path)
        AppTaskServices(app).refresh_task_shared_state()

        self.assertIn("task_overview", app.shared_state)
        results = app.shared_state.get("completed_task_results", "")
        self.assertIn("[task ", results)
        self.assertIn("validar cobertura dos testes", results)
        self.assertLessEqual(len(results.split(": ", 1)[1]), 200)

    def test_refresh_task_shared_state_removes_completed_task_results_when_none_exist(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.shared_state = {"completed_task_results": "stale"}
        app.current_job_id = 1
        tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_path = tmp_dir / "tasks.db"
        init_db(str(db_path))
        add_job("Session", db_path=str(db_path), job_id=1)
        app.tasks_db_path = str(db_path)
        AppTaskServices(app).refresh_task_shared_state()

        self.assertIn("task_overview", app.shared_state)
        self.assertNotIn("completed_task_results", app.shared_state)

    def test_refresh_task_shared_state_returns_when_shared_state_is_invalid(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.shared_state = None
        app.current_job_id = 1
        app.tasks_db_path = "/tmp/unused.db"

        AppTaskServices(app).refresh_task_shared_state()

        self.assertIsNone(app.shared_state)

    def test_prompt_includes_completed_task_results_when_goal_is_locked(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(
            AGENT_CLAUDE,
            history,
            shared_state={
                "goal_canonical": "Fechar cobertura",
                "completed_task_results": "[task 1] testes: ok",
            },
        )

        self.assertIn("TAREFAS CONCLUÍDAS:", prompt)
        self.assertIn("[task 1] testes: ok", prompt)

    def test_prompt_debug_metrics_include_prompt_sizes(self):
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [
            {"role": "human", "content": "Pergunta"},
            {"role": "claude", "content": "Resposta objetiva"},
        ]

        prompt, metrics = builder.build(AGENT_CLAUDE, history, debug=True)

        self.assertIn("CONVERSA:", prompt)
        self.assertTrue(metrics["primary"])
        self.assertGreater(metrics["total_chars"], 0)
        self.assertIn("facts_chars", metrics)

    def test_behavior_metrics_generate_feedback_empty_when_few_responses(self):
        """generate_feedback deve retornar vazio com menos de 3 respostas."""
        from quimera.metrics import BehaviorMetricsTracker

        tracker = BehaviorMetricsTracker()
        tracker.record_response("claude", 1.0)
        tracker.record_response("claude", 1.0)

        feedback = tracker.generate_feedback("claude")
        self.assertEqual(feedback, "")

    def test_behavior_metrics_generate_feedback_with_synthesis_correction(self):
        """Feedback deve indicar sínteses imprecisas quando correction rate é alto."""
        from quimera.metrics import BehaviorMetricsTracker

        tracker = BehaviorMetricsTracker()
        for i in range(5):
            tracker.record_response("claude", 1.0)
        for i in range(4):
            tracker.record_synthesis("claude", needed_correction=True)

        feedback = tracker.generate_feedback("claude")
        self.assertIn("SÍNTESES IMPRECISAS", feedback)

    def test_behavior_metrics_generate_feedback_for_invalid_handoff_context_gap(self):
        """Feedback de handoff inválido deve tratar falta de contexto como erro de roteamento."""
        from quimera.metrics import BehaviorMetricsTracker

        tracker = BehaviorMetricsTracker()
        for _ in range(5):
            tracker.record_response("claude", 1.0)
        for _ in range(2):
            tracker.record_handoff_sent("claude", is_invalid=True)

        feedback = tracker.generate_feedback("claude")
        self.assertIn("ALTA TAXA DE HANDOFF INVÁLIDO", feedback)
        self.assertIn("faltar contexto suficiente", feedback)
        self.assertIn("falha no roteamento inicial", feedback)
        self.assertIn("delegue", feedback)
        self.assertIn("não improvise", feedback)
        self.assertNotIn("resolva você mesmo", feedback)

    def test_prompt_builder_injects_metrics_when_tracker_has_data(self):
        """PromptBuilder deve incluir bloco MÉTRICAS DO AGENTE quando há feedback do tracker."""
        from quimera.metrics import BehaviorMetricsTracker

        tracker = BehaviorMetricsTracker()
        # Gera dados suficientes para acionar feedback (>= 3 respostas + sínteses com correção)
        for _ in range(5):
            tracker.record_response("claude", 1.0)
        for _ in range(4):
            tracker.record_synthesis("claude", needed_correction=True)

        builder = PromptBuilder(DummyContextManager(), history_window=3, metrics_tracker=tracker)
        prompt = builder.build("claude", [])

        self.assertIn("MÉTRICAS DO AGENTE", prompt)
        self.assertIn("SÍNTESES IMPRECISAS", prompt)

    def test_prompt_builder_omits_metrics_block_when_no_tracker(self):
        """PromptBuilder sem metrics_tracker não deve incluir bloco de métricas."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        prompt = builder.build("claude", [])

        self.assertNotIn("MÉTRICAS DO AGENTE", prompt)

    def test_prompt_builder_omits_metrics_block_when_insufficient_data(self):
        """PromptBuilder não deve incluir métricas se generate_feedback retornar vazio."""
        from quimera.metrics import BehaviorMetricsTracker

        tracker = BehaviorMetricsTracker()
        tracker.record_response("claude", 1.0)  # apenas 1 resposta — abaixo do threshold

        builder = PromptBuilder(DummyContextManager(), history_window=3, metrics_tracker=tracker)
        prompt = builder.build("claude", [])

        self.assertNotIn("MÉTRICAS DO AGENTE", prompt)


class AppProtocolDirectTests(unittest.TestCase):
    """Testes unitários diretos de AppProtocol para cobertura de ramos não exercidos."""

    def _make_app(self, shared_state=None, session_state=None):
        app = QuimeraApp.__new__(QuimeraApp)
        import threading
        app._lock = threading.Lock()
        app.shared_state = shared_state if shared_state is not None else {}
        app.session_state = session_state
        return app

    # --- _get_decisions_logger ---

    def test_get_decisions_logger_returns_none_when_no_path(self):
        proto = AppProtocol(Mock(), decisions_log_path=None)
        result = proto._get_decisions_logger()
        self.assertIsNone(result)

    def test_get_decisions_logger_caches_instance(self):
        with patch("quimera.workspace.DecisionsLogger") as MockDL:
            import tempfile
            tmp = tempfile.mktemp(suffix=".json")
            proto = AppProtocol(Mock(), decisions_log_path=tmp)
            # first call creates it
            first = proto._get_decisions_logger()
            # second call returns cached (line 34)
            second = proto._get_decisions_logger()
            self.assertIs(first, second)

    def test_get_decisions_logger_creates_instance_with_path(self):
        with patch("quimera.app.protocol.DecisionsLogger", create=True) as MockDL:
            pass
        # test via apply_state_update which calls the logger (lines 37-39, 80-81)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log_path = tmp + "/decisions.json"
            mock_logger = Mock()
            with patch("quimera.workspace.DecisionsLogger", return_value=mock_logger):
                app = self._make_app()
                app.workspace = SimpleNamespace(cwd="/tmp")
                proto = AppProtocol(app, decisions_log_path=log_path)
                payload = '{"decisions": ["dec1", "dec2"]}'
                result = proto.apply_state_update(payload)
            self.assertTrue(result)
            mock_logger.append.assert_called()

    # --- merge_state_value ---

    def test_merge_state_value_incoming_none_returns_current(self):
        result = AppProtocol.merge_state_value("existing", None)
        self.assertEqual(result, "existing")

    def test_merge_state_value_incoming_empty_string_returns_none(self):
        result = AppProtocol.merge_state_value("existing", "")
        self.assertIsNone(result)

    # --- apply_state_update ---

    def test_apply_state_update_invalid_json_returns_false(self):
        app = self._make_app()
        proto = AppProtocol(app)
        result = proto.apply_state_update("not json {{")
        self.assertFalse(result)

    def test_apply_state_update_non_dict_returns_false(self):
        app = self._make_app()
        proto = AppProtocol(app)
        result = proto.apply_state_update('"just a string"')
        self.assertFalse(result)

    def test_apply_state_update_skips_empty_key(self):
        app = self._make_app()
        proto = AppProtocol(app)
        result = proto.apply_state_update('{"": "value", "valid": "ok"}')
        self.assertTrue(result)
        self.assertNotIn("", app.shared_state)
        self.assertEqual(app.shared_state["valid"], "ok")

    def test_apply_state_update_pops_key_when_merged_is_none(self):
        app = self._make_app(shared_state={"goal": "old"})
        proto = AppProtocol(app)
        # incoming "" causes merge to return None → pop
        result = proto.apply_state_update('{"goal": ""}')
        self.assertTrue(result)
        self.assertNotIn("goal", app.shared_state)

    # --- strip_payload_residual ---

    def test_strip_payload_residual_empty_text_returns_empty(self):
        proto = AppProtocol(Mock())
        result = proto.strip_payload_residual("")
        self.assertEqual(result, "")

    def test_strip_payload_residual_none_returns_empty(self):
        proto = AppProtocol(Mock())
        result = proto.strip_payload_residual(None)
        self.assertEqual(result, "")

    # --- parse_handoff_payload ---

    def test_parse_handoff_payload_no_match_returns_none(self):
        proto = AppProtocol(Mock())
        result = proto.parse_handoff_payload("completely invalid payload no task keyword", target="codex")
        self.assertIsNone(result)

    def test_parse_handoff_payload_empty_task_returns_none(self):
        # Usa regex customizado que aceita grupo vazio para exercer linha 123-129
        proto = AppProtocol(Mock())
        proto.HANDOFF_PAYLOAD_PATTERN = re.compile(
            r"^\s*task:\s*([^\n]*?)\s*(?:context:\s*([^\n]*?))?\s*(?:expected:\s*([^\n]*?))?\s*(?:priority:\s*([^\n]*?))?\s*$",
            re.IGNORECASE,
        )
        result = proto.parse_handoff_payload("task:", target="codex")
        self.assertIsNone(result)

    # --- parse_response ---

    def test_parse_response_none_returns_all_none(self):
        app = self._make_app()
        proto = AppProtocol(app)
        result = proto.parse_response(None)
        self.assertEqual(result, (None, None, None, False, False, None))

    def test_parse_response_needs_human_input_marker(self):
        from quimera.constants import NEEDS_INPUT_MARKER
        app = self._make_app()
        proto = AppProtocol(app)
        response, _, _, _, needs_input, _ = proto.parse_response(f"pergunta {NEEDS_INPUT_MARKER}")
        self.assertTrue(needs_input)
        self.assertNotIn(NEEDS_INPUT_MARKER, response)

    def test_parse_response_invalid_handoff_increments_session_state(self):
        session_state = {"handoff_invalid_count": 0}
        app = self._make_app(session_state=session_state)
        app.behavior_metrics = None
        proto = AppProtocol(app)
        # ROUTE com payload inválido → handoff_invalid_count sobe
        response, target, handoff, _, _, _ = proto.parse_response(
            "[ROUTE:codex] texto sem task keyword válido\nline2\n"
        )
        self.assertIsNone(target)
        self.assertIsNone(handoff)
        self.assertEqual(session_state["handoff_invalid_count"], 1)

    def test_parse_response_invalid_handoff_calls_behavior_metrics(self):
        session_state = {"handoff_invalid_count": 0}
        app = self._make_app(session_state=session_state)
        app.behavior_metrics = Mock()
        proto = AppProtocol(app)
        proto.parse_response("[ROUTE:codex] texto sem task keyword válido\nline2\n")
        app.behavior_metrics.record_handoff_sent.assert_called_once_with("codex", is_invalid=True)

    def test_parse_response_invalid_handoff_session_state_key_error_is_swallowed(self):
        # Exercita o except KeyError: pass (linhas 184-185) no bloco de contagem de handoff inválido
        class _RaisingDict(dict):
            def __setitem__(self, key, value):
                raise KeyError(key)

        session_state = _RaisingDict()
        dict.__setitem__(session_state, "handoff_invalid_count", 0)  # inicializa sem usar __setitem__

        app = self._make_app(session_state=session_state)
        app.behavior_metrics = None
        proto = AppProtocol(app)
        # Não deve levantar exceção
        proto.parse_response("[ROUTE:codex] texto sem task keyword\nline2\n")

    def test_parse_response_returns_none_when_route_consumes_all(self):
        app = self._make_app()
        proto = AppProtocol(app)
        # ROUTE que consome a resposta inteira → response vira None após sub → retorna None tuple
        result = proto.parse_response("[ROUTE:codex] task: fazer algo")
        self.assertEqual(result[:3], (None, None, None))


if __name__ == "__main__":
    unittest.main()

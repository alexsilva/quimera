import io
import re
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import Mock, call, patch

import quimera.app as app_module
import quimera.cli as cli_module
import quimera.plugins as plugins
from quimera.agents import AgentClient
from quimera.app import QuimeraApp
from quimera.cli import main as cli_main
from quimera.config import DEFAULT_HISTORY_WINDOW
from quimera.constants import CMD_HELP, EXTEND_MARKER, build_help
from quimera.plugins import AgentPlugin
from quimera.prompt import PromptBuilder
from quimera.runtime.approval import ApprovalHandler
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.tasks import add_job, init_db, list_tasks
from quimera.session_summary import SessionSummarizer
from quimera.ui import _agent_style

AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"


class DummyRenderer:
    def __init__(self):
        self.warnings = []
        self.system_messages = []
        self.handoffs = []
        self._output_lock = threading.Lock()

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
    def __init__(self):
        self.user_name = "Você"
        self.history_window = DEFAULT_HISTORY_WINDOW
        self.auto_summarize_threshold = 30
        self.idle_timeout_seconds = 300


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

        response, _, _, extend, _, _ = app.parse_response(f"Resposta objetiva {EXTEND_MARKER}")

        self.assertEqual(response, "Resposta objetiva")
        self.assertTrue(extend)

    def test_parse_response_keeps_plain_response(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
        app.shared_state = {}

        response, target, handoff, extend, _, _ = app.parse_response("Resposta objetiva")

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
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
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
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
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
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        result = app.parse_handoff_payload("task: Revise este código")
        self.assertEqual(result["task"], "Revise este código")
        self.assertIsNone(result["context"])
        self.assertIsNone(result["expected"])
        self.assertEqual(result["priority"], "normal")
        self.assertIn("handoff_id", result)

    def test_parse_handoff_payload_task_and_context(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        result = app.parse_handoff_payload("task: Revise este código | context: Verificar performance")
        self.assertEqual(result["task"], "Revise este código")
        self.assertEqual(result["context"], "Verificar performance")
        self.assertIsNone(result["expected"])
        self.assertEqual(result["priority"], "normal")

    def test_parse_response_route_with_residual_text(self):
        """ROUTE block should be recognized even when followed by residual text."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
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
        app.ROUTE_PATTERN = re.compile(
            rf"^\[ROUTE:({'|'.join(escaped_agents)})\]\s*([\s\S]+)\s*\Z",
            re.MULTILINE
        )
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
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
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
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
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
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
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
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

    def test_handle_command_shows_help(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]

        handled = app.handle_command(CMD_HELP)

        self.assertTrue(handled)
        expected_help = build_help([AGENT_CLAUDE, AGENT_CODEX])
        self.assertEqual(app.renderer.system_messages, [expected_help])

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
        app._refresh_task_shared_state = QuimeraApp._refresh_task_shared_state.__get__(app, QuimeraApp)
        app._build_task_overview = QuimeraApp._build_task_overview.__get__(app, QuimeraApp)

        handled = app.handle_command('/task "execute os testes"')

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
        app._refresh_task_shared_state = QuimeraApp._refresh_task_shared_state.__get__(app, QuimeraApp)
        app._build_task_overview = QuimeraApp._build_task_overview.__get__(app, QuimeraApp)

        handled = app.handle_command('/task "revise o arquivo quimera/app.py"')

        self.assertTrue(handled)
        tasks = list_tasks({"job_id": 1}, db_path=str(db_path))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["assigned_to"], "ollama-qwen")
        self.assertIn("atribuída para ollama-qwen", app.renderer.system_messages[-1])

    def test_classify_task_execution_result_rejects_needs_input(self):
        ok, reason = QuimeraApp._classify_task_execution_result(
            "Preciso de mais contexto. [NEEDS_INPUT]"
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "agente solicitou input humano")

    def test_classify_task_execution_result_rejects_inability_text(self):
        ok, reason = QuimeraApp._classify_task_execution_result(
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
        app._get_task_routing_plugins = QuimeraApp._get_task_routing_plugins.__get__(app, QuimeraApp)
        app._count_agent_open_tasks = QuimeraApp._count_agent_open_tasks.__get__(app, QuimeraApp)

        for idx in range(3):
            create_task(
                1,
                f"Tarefa {idx}",
                task_type="general",
                assigned_to=AGENT_CLAUDE,
                status="pending",
                db_path=str(db_path),
            )

        selected = QuimeraApp._choose_agent_with_load_balance(app, "general")

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

        handled = app.handle_command('/task ""')

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

    def test_prompt_includes_recent_facts_block(self):
        builder = PromptBuilder(DummyContextManager(), history_window=5)
        history = [
            {"role": "human", "content": "Investigue"},
            {"role": "claude", "content": "Arquivo alterado: app.py"},
            {"role": "codex", "content": "Teste falhou em test_x"},
        ]

        prompt = builder.build(AGENT_CLAUDE, history)

        self.assertIn("FATOS OBSERVADOS RECENTES", prompt)
        self.assertIn("[CLAUDE] Arquivo alterado: app.py", prompt)
        self.assertIn("[CODEX] Teste falhou em test_x", prompt)

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

    def test_app_builds_explicit_session_state_for_prompt(self):
        temp_root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        class FakeWorkspace:
            def __init__(self, cwd):
                self.root = temp_root
                self.cwd = cwd
                self.context_persistent = temp_root / "quimera_context.md"
                self.context_session = temp_root / "quimera_session_context.md"
                self.logs_dir = temp_root / "quimera_logs"
                self.history_file = temp_root / "quimera_history"
                self.state_dir = temp_root / "quimera_state"
                self.tasks_db = temp_root / "quimera_tasks.db"

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
                self.context_persistent = temp_root / "quimera_context.md"
                self.context_session = temp_root / "quimera_session_context.md"
                self.logs_dir = temp_root / "quimera_logs"
                self.history_file = temp_root / "quimera_history"
                self.state_dir = temp_root / "quimera_state"
                self.tasks_db = temp_root / "quimera_tasks.db"

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
                self.context_persistent = temp_root / "quimera_context.md"
                self.context_session = temp_root / "quimera_session_context.md"
                self.logs_dir = temp_root / "quimera_logs"
                self.history_file = temp_root / "quimera_history"
                self.state_dir = temp_root / "quimera_state"
                self.tasks_db = temp_root / "quimera_tasks.db"

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

        try:
            self.assertEqual(app.prompt_builder.history_window, 5)
        finally:
            app._stop_task_executors()

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

        app.active_agents = list(plugins.all_names())
        app.threads = 1
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

        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.threads = 1
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
        app._refresh_task_shared_state = lambda: None
        app.print_response = lambda agent, response: printed.append((agent, response))
        app.persist_message = lambda role, content: persisted.append((role, content))
        app.shutdown = lambda: None
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
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
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
        app._maybe_auto_summarize = lambda preferred_agent=None: None
        app._record_agent_metric = Mock()

        from pathlib import Path
        import tempfile
        staging_root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        agent, response, route_target, handoff, extend, needs_input = app._call_agent_for_parallel("agent1", None, "standard", staging_root, 0)
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
        app._maybe_auto_summarize = lambda preferred_agent=None: None
        app.shutdown = lambda: None

        persisted = []
        printed = []
        app.persist_message = lambda role, content: persisted.append((role, content))
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

        self.assertTrue(second_prompt_seen.wait(timeout=1), "run() não voltou ao prompt enquanto o agente ainda executava")
        allow_finish.set()
        run_thread.join(timeout=2)

        self.assertFalse(run_thread.is_alive(), "run() deveria encerrar após drenar a fila")
        self.assertEqual(persisted[0], ("human", "oi"))
        self.assertIn((AGENT_CLAUDE, "claude responde"), printed)

    def test_read_user_input_nonblocking_tty_preserves_input_path(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._nonblocking_prompt_visible = False
        app._nonblocking_input_queue = None
        app._nonblocking_input_thread = None
        app._nonblocking_input_status = "idle"

        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        started = threading.Event()

        def fake_input(prompt):
            started.set()
            time.sleep(0.05)
            return "mensagem"

        with patch("sys.stdin", stdin), patch("quimera.app.input", side_effect=fake_input) as mock_input:
            first = app.read_user_input("Você: ", timeout=0)
            self.assertIsNone(first)
            self.assertTrue(started.wait(timeout=1), "reader assíncrono não iniciou")

            deadline = time.time() + 1
            second = None
            while time.time() < deadline and second is None:
                second = app.read_user_input("Você: ", timeout=0)
                if second is None:
                    time.sleep(0.01)

        self.assertEqual(second, "mensagem")
        mock_input.assert_called_once_with("Você: ")

    def test_show_system_message_redisplays_prompt_for_task_status_text_while_tty_reader_is_active(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Alex: "

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("quimera.app.readline.get_line_buffer", return_value=""), patch(
            "quimera.app.readline.redisplay"
        ) as mock_redisplay:
            app.show_system_message("[task 7] claude: iniciando")

        self.assertEqual(app.renderer.system_messages, ["[task 7] claude: iniciando"])
        mock_redisplay.assert_called_once_with()

    def test_show_system_message_redraws_human_prompt_with_user_name_for_task_status_text(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app._output_lock = threading.Lock()
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Alex: "

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        with patch("sys.stdin", stdin), patch("quimera.app.readline.get_line_buffer", return_value=""), patch(
            "quimera.app.readline.redisplay"
        ), patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush") as mock_flush:
            app.show_system_message("[task 7] claude: concluída")

        self.assertIn(call("\r\x1b[2K"), mock_write.call_args_list)
        self.assertIn(call("Alex: "), mock_write.call_args_list)
        self.assertGreaterEqual(mock_flush.call_count, 1)

    def test_staging_logger_clears_and_redisplays_prompt_while_tty_reader_is_active(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app._output_lock = threading.Lock()
        app._nonblocking_input_status = "reading"
        app._nonblocking_prompt_text = "Alex: "

        stdin = io.StringIO("")
        stdin.isatty = lambda: True

        prompt_handler = next(handler for handler in app_module.logger.handlers if
                              isinstance(handler, app_module.PromptAwareStderrHandler))
        previous_app = prompt_handler._app
        prompt_handler.bind_app(app)
        try:
            with patch("sys.stdin", stdin), patch("quimera.app.readline.get_line_buffer", return_value=""), patch(
                "quimera.app.readline.redisplay"
            ) as mock_redisplay, patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush") as mock_flush:
                app_module.logger.info("[DISPATCH] sending to agent=%s", AGENT_CODEX)

            self.assertIn(call("\r\x1b[2K"), mock_write.call_args_list)
            self.assertIn(call("Alex: "), mock_write.call_args_list)
            self.assertGreaterEqual(mock_flush.call_count, 2)
            mock_redisplay.assert_called_once_with()
        finally:
            prompt_handler.bind_app(previous_app)

    def test_parse_routing_selects_random_initial_agent(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]
        app.round_index = 0
        app.renderer = DummyRenderer()

        with patch("quimera.app.random.choice", return_value=AGENT_CODEX) as mock_choice:
            agent, message, explicit = app.parse_routing("oi")

        self.assertEqual(agent, AGENT_CODEX)
        self.assertEqual(message, "oi")
        self.assertFalse(explicit)
        mock_choice.assert_called_once_with(app.active_agents)

    def test_parse_handoff_payload_with_priority(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        result = app.parse_handoff_payload("task: Corrigir bug crítico | priority: urgent")
        self.assertEqual(result["task"], "Corrigir bug crítico")
        self.assertEqual(result["priority"], "urgent")
        self.assertIn("handoff_id", result)

    def test_parse_handoff_payload_invalid_priority_defaults_to_normal(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        result = app.parse_handoff_payload("task: Algo qualquer | priority: invalido")
        self.assertEqual(result["priority"], "normal")

    def test_parse_handoff_payload_generates_unique_ids(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
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
        call_count = [0]

        def fake_call_agent(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 2:
                return None
            return "sucesso no retry"

        app._call_agent = fake_call_agent
        app.resolve_agent_response = lambda agent, response, silent=False, persist_history=True, show_output=True: response

        result = app.call_agent("claude")
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
        app._classify_task_execution_result = lambda response: (True, response)

        with patch("quimera.app.create_executor", side_effect=fake_create_executor), patch(
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
        app._classify_task_execution_result = lambda response: (True, response)

        task_body = (
            "TAREFA:\nvalidar regressão\n\n"
            "CONTEXTO RECENTE DO CHAT:\n"
            "[ALEX]: a execução da tarefa precisa receber o contexto do chat\n"
            "[CLAUDE]: alguém passou contexto errado\n\n"
            "INSTRUÇÃO:\n"
            "Execute a tarefa usando o contexto acima como referência."
        )

        with patch("quimera.app.create_executor", side_effect=fake_create_executor), patch(
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
        app._classify_task_execution_result = lambda response: (True, response)
        app._record_failure = lambda agent: None

        with patch("quimera.app.create_executor", side_effect=fake_create_executor), patch(
            "quimera.runtime.tasks.requeue_task"
        ) as requeue_task, patch("quimera.runtime.tasks.fail_task") as fail_task:
            app._setup_task_executors()
            ok = handlers[AGENT_CLAUDE]({"id": 1, "description": "rode a task"})

        self.assertFalse(ok)
        requeue_task.assert_called_once_with(
            1, AGENT_CLAUDE, reason="communication failed", db_path=app.tasks_db_path
        )
        fail_task.assert_not_called()

    def test_parse_response_extracts_ack_marker(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.ACK_PATTERN = QuimeraApp.ACK_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
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
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.ACK_PATTERN = QuimeraApp.ACK_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
        app.shared_state = {}

        response, _, _, _, _, ack_id = app.parse_response("Resposta sem ACK")

        self.assertIsNone(ack_id)
        self.assertEqual(response, "Resposta sem ACK")

    def test_handoff_chain_propagation(self):
        """Test that handoff chain is propagated correctly."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        result = app.parse_handoff_payload("task: Test task")
        self.assertEqual(result["chain"], [])
        
        # Simulate chain propagation
        result["chain"] = ["claude"]
        result["chain"].append("codex")
        self.assertEqual(result["chain"], ["claude", "codex"])

    def test_handoff_id_uses_real_target(self):
        """Test that handoff_id includes target in its generation."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
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
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.ACK_PATTERN = QuimeraApp.ACK_PATTERN
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
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

        # Simulate successful call to claude
        app._record_agent_metric("claude", "succeeded", 1.5)
        app._record_agent_metric("claude", "succeeded", 0.8)
        
        # Simulate failed call to codex
        app._record_agent_metric("codex", "failed", 0.0)

        metrics = app.session_state["agent_metrics"]
        self.assertEqual(metrics["claude"]["succeeded"], 2)
        self.assertEqual(metrics["claude"]["latency"], 2.3)
        self.assertEqual(metrics["codex"]["failed"], 1)
        self.assertEqual(metrics["codex"]["succeeded"], 0)

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


class FallbackChainTests(unittest.TestCase):
    """Testes para fallback chain quando agente secundário falha."""

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
        app.persist_message = lambda role, content: persisted.append((role, content))
        app.shutdown = lambda: None
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
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
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
        """_has_clear_next_step deve detectar indicadores de próximo passo."""
        app = QuimeraApp.__new__(QuimeraApp)
        
        self.assertTrue(app._has_clear_next_step("Próximo passo: revisar o código."))
        self.assertTrue(app._has_clear_next_step("Próxima etapa: implementar a feature."))
        self.assertTrue(app._has_clear_next_step("Tarefa completa."))
        self.assertTrue(app._has_clear_next_step("Concluído."))
        self.assertFalse(app._has_clear_next_step("Apenas uma resposta qualquer."))
        self.assertFalse(app._has_clear_next_step(""))

    def test_is_response_redundant_detects_similarity(self):
        """_is_response_redundant deve detectar respostas similares."""
        app = QuimeraApp.__new__(QuimeraApp)
        
        history = [
            {"role": "human", "content": "Faça algo"},
            {"role": "claude", "content": "Vou implementar a feature X agora. Isso envolve criar o arquivo e testar."},
        ]
        
        similar_response = "Vou implementar a feature X agora. Isso envolve criar o arquivo e testar."
        self.assertTrue(app._is_response_redundant(similar_response, history))
        
        different_response = "Vou corrigir o bug Y no parser."
        self.assertFalse(app._is_response_redundant(different_response, history))

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

    def test_prompt_is_concise(self):
        """Prompt deve ser conciso após enxugamento."""
        builder = PromptBuilder(DummyContextManager(), history_window=3)
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build(AGENT_CLAUDE, history, is_first_speaker=True)

        self.assertLess(len(prompt), 5000)

    def test_get_task_routing_plugins_respects_explicit_active_agents(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = [AGENT_CLAUDE, AGENT_CODEX]

        selected = [plugin.name for plugin in app._get_task_routing_plugins()]

        self.assertEqual(selected, [AGENT_CLAUDE, AGENT_CODEX])

    def test_get_task_routing_plugins_expands_wildcard_to_all_plugins(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = ["*"]

        selected = [plugin.name for plugin in app._get_task_routing_plugins()]

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

        # Simulate successful calls
        app._record_agent_metric("claude", "succeeded", 1.5)
        app._record_agent_metric("claude", "succeeded", 2.0)
        app._record_agent_metric("claude", "succeeded", 1.0)
        app._record_agent_metric("claude", "succeeded", 0.5)

        # Verifica que o tracker foi alimentado
        claude_metrics = app.behavior_metrics.get_agent_summary("claude")
        self.assertEqual(claude_metrics["responses_total"], 4)

    def test_behavior_metrics_tracks_invalid_handoff(self):
        """Handoff inválido deve ser registrado no tracker."""
        from quimera.metrics import BehaviorMetricsTracker

        app = QuimeraApp.__new__(QuimeraApp)
        app.ROUTE_PATTERN = QuimeraApp.ROUTE_PATTERN
        app.HANDOFF_PAYLOAD_PATTERN = QuimeraApp.HANDOFF_PAYLOAD_PATTERN
        app.STATE_UPDATE_PATTERN = QuimeraApp.STATE_UPDATE_PATTERN
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
        self.assertLess(len(rule), 500)

    def test_reviewer_rule_is_concise(self):
        """PROMPT_REVIEWER_RULE deve ser conciso."""
        from quimera.constants import PROMPT_REVIEWER_RULE
        
        self.assertIn("veredicto", PROMPT_REVIEWER_RULE.lower())
        self.assertLess(len(PROMPT_REVIEWER_RULE), 550)

    def test_handoff_rule_is_concise(self):
        """PROMPT_HANDOFF_RULE deve ser conciso."""
        from quimera.constants import PROMPT_HANDOFF_RULE
        
        self.assertIn("ACK", PROMPT_HANDOFF_RULE)
        self.assertLess(len(PROMPT_HANDOFF_RULE), 400)

    def test_base_rules_are_concise(self):
        """PROMPT_BASE_RULES deve ser conciso e cobrir prioridades."""
        from quimera.constants import PROMPT_BASE_RULES
        
        self.assertIn("humano", PROMPT_BASE_RULES.lower())
        self.assertIn("prioridade", PROMPT_BASE_RULES.lower())
        self.assertIn("foco", PROMPT_BASE_RULES.lower())
        self.assertIn("editar arquivos", PROMPT_BASE_RULES.lower())
        self.assertLess(len(PROMPT_BASE_RULES), 1600)

    def test_tool_rule_guides_discovery_before_edits(self):
        from quimera.constants import PROMPT_TOOL_RULE

        self.assertIn("list_files", PROMPT_TOOL_RULE)
        self.assertIn("grep_search", PROMPT_TOOL_RULE)
        self.assertIn("read_file", PROMPT_TOOL_RULE)
        self.assertIn("apply_patch", PROMPT_TOOL_RULE)
        self.assertIn("run_shell", PROMPT_TOOL_RULE)

    def test_build_task_body_includes_operational_protocol(self):
        app = QuimeraApp.__new__(QuimeraApp)
        app.user_name = "Alex"
        app.history = [{"role": "human", "content": "Corrija o parser atual"}]
        app.shared_state = {}

        body = app._build_task_body("corrigir parser")

        self.assertIn("PROTOCOLO OPERACIONAL:", body)
        self.assertIn("Descubra o alvo antes de mudar", body)
        self.assertIn("apply_patch", body)
        self.assertIn("run_shell", body)
        self.assertIn("arquivos alterados", body)

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


if __name__ == "__main__":
    unittest.main()

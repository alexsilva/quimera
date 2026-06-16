"""Testes para quimera.modes e integração com ToolPolicy e parse_routing."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from quimera.modes import MODES, ExecutionMode, get_mode
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicy, ToolPolicyError


class TestExecutionMode(unittest.TestCase):
    def test_all_modes_defined(self):
        """Verifica que todos os modos esperados estão registrados em MODES."""
        for key in ["/planning", "/analysis", "/design", "/review", "/execute"]:
            self.assertIn(key, MODES)

    def test_get_mode_returns_correct_mode(self):
        """Verifica que get_mode retorna o modo correto para um comando válido."""
        mode = get_mode("/planning")
        self.assertIsNotNone(mode)
        self.assertEqual(mode.name, "planning")

    def test_get_mode_case_insensitive(self):
        """Verifica que get_mode é insensível a maiúsculas/minúsculas."""
        self.assertIsNotNone(get_mode("/PLANNING"))
        self.assertIsNotNone(get_mode("/Analysis"))

    def test_get_mode_unknown_returns_none(self):
        """Verifica que get_mode retorna None para comandos desconhecidos ou inválidos."""
        self.assertIsNone(get_mode("/unknown"))
        self.assertIsNone(get_mode("planning"))
        self.assertIsNone(get_mode(""))

    def test_planning_mode_properties(self):
        """Verifica as propriedades do modo /planning: somente leitura, rede permitida e ferramentas bloqueadas."""
        mode = get_mode("/planning")
        self.assertTrue(mode.read_only_fs)
        self.assertTrue(mode.allow_network)
        self.assertIn("write_file", mode.blocked_tools)
        self.assertIn("apply_patch", mode.blocked_tools)
        self.assertNotIn("run_shell", mode.blocked_tools)
        self.assertNotIn("exec_command", mode.blocked_tools)
        self.assertNotEqual(mode.prompt_addon, "")

    def test_analysis_mode_allows_network(self):
        """Verifica que o modo /analysis é somente leitura e permite acesso à rede."""
        mode = get_mode("/analysis")
        self.assertTrue(mode.read_only_fs)
        self.assertTrue(mode.allow_network)
        self.assertIn("write_file", mode.blocked_tools)
        self.assertNotIn("run_shell", mode.blocked_tools)

    def test_execute_mode_no_restrictions(self):
        """Verifica que o modo /execute não possui restrições de filesystem nem ferramentas bloqueadas."""
        mode = get_mode("/execute")
        self.assertFalse(mode.read_only_fs)
        self.assertTrue(mode.allow_network)
        self.assertEqual(mode.blocked_tools, [])
        self.assertIn("não muda a intenção", mode.prompt_addon.lower())
        self.assertIn("se o humano pedir análise, analise", mode.prompt_addon.lower())

    def test_design_review_are_read_only_with_network(self):
        """Verifica que /design e /review são somente leitura e permitem rede."""
        for cmd in ["/design", "/review"]:
            mode = get_mode(cmd)
            self.assertTrue(mode.read_only_fs, f"{cmd} should be read_only_fs")
            self.assertTrue(mode.allow_network, f"{cmd} should allow network")


class TestToolPolicyBlockedTools(unittest.TestCase):
    def _make_policy(self, blocked=None):
        config = ToolRuntimeConfig(workspace_root=Path("/tmp"))
        policy = ToolPolicy(config)
        if blocked:
            policy.blocked_tools = blocked
        return policy

    def _call(self, name, args=None):
        return ToolCall(name=name, arguments=args or {})

    def test_no_blocked_tools_by_default(self):
        """Verifica que a política não tem ferramentas bloqueadas por padrão."""
        policy = self._make_policy()
        self.assertEqual(policy.blocked_tools, [])

    def test_blocked_tool_raises(self):
        """Verifica que validar uma ferramenta bloqueada lança ToolPolicyError com mensagem adequada."""
        policy = self._make_policy(blocked=["write_file"])
        call = self._call("write_file", {"path": "foo.py", "content": "x"})
        with self.assertRaises(ToolPolicyError) as ctx:
            policy.validate(call)
        self.assertIn("bloqueada", str(ctx.exception))

    def test_non_blocked_tool_passes_policy(self):
        """Verifica que ferramenta não bloqueada não lança erro de 'bloqueada' na validação."""
        policy = self._make_policy(blocked=["write_file"])
        # read_file is not blocked — should pass policy check (file existence may fail,
        # but the blocked_tools check itself should not raise)
        call = self._call("read_file", {"path": "nonexistent.py"})
        # We expect ToolPolicyError but for a different reason (file not found), not blocked
        try:
            policy.validate(call)
        except ToolPolicyError as exc:
            self.assertNotIn("bloqueada", str(exc))

    def test_multiple_blocked_tools(self):
        """Verifica que múltiplas ferramentas bloqueadas são todas rejeitadas pela política."""
        blocked = ["write_file", "apply_patch", "run_shell"]
        policy = self._make_policy(blocked=blocked)
        for tool in blocked:
            call = self._call(tool, {"path": "x", "content": "y", "patch": "z", "command": "ls"})
            with self.assertRaises(ToolPolicyError):
                policy.validate(call)

    def test_blocked_tools_cleared(self):
        """Verifica que limpar blocked_tools remove todas as restrições da política."""
        policy = self._make_policy(blocked=["write_file"])
        policy.blocked_tools = []
        # write_file now must pass the blocked check (may fail for other reasons)
        call = self._call("write_file", {"path": "/tmp/test.txt", "content": "x"})
        try:
            policy.validate(call)
        except ToolPolicyError as exc:
            self.assertNotIn("bloqueada", str(exc))

    def test_execution_modes_keep_blocked_tools_enforced(self):
        """Verifica que ferramentas bloqueadas pelo modo de execução são aplicadas pela política."""
        policy = self._make_policy()
        analysis_mode = get_mode("/analysis")
        policy.blocked_tools = list(analysis_mode.blocked_tools)
        with self.assertRaises(ToolPolicyError) as ctx:
            policy.validate(self._call("write_file", {"path": "x.py", "content": "x"}))
        self.assertIn("bloqueada", str(ctx.exception))

        execute_mode = get_mode("/execute")
        policy.blocked_tools = list(execute_mode.blocked_tools)
        self.assertEqual(policy.blocked_tools, [])


class TestParseRoutingWithModes(unittest.TestCase):
    """Testa que parse_routing detecta comandos de modo e os aplica."""

    def _make_app(self):
        """Cria um QuimeraApp mínimo com mocks."""
        from quimera.app.core import QuimeraApp
        app = QuimeraApp.__new__(QuimeraApp)
        app._lock = __import__("threading").Lock()
        app.active_agents = ["claude", "codex"]
        app.selected_agents = ["claude", "codex"]
        app.execution_mode = None
        app.renderer = MagicMock()

        # Plugins mock
        mock_claude = MagicMock()
        mock_claude.prefix = "/claude"
        mock_claude.name = "claude"
        mock_codex = MagicMock()
        mock_codex.prefix = "/codex"
        mock_codex.name = "codex"

        def fake_get(name):
            return {"claude": mock_claude, "codex": mock_codex}.get(name)

        # ToolExecutor mock com policy
        from quimera.runtime.policy import ToolPolicy
        from quimera.runtime.config import ToolRuntimeConfig
        policy = ToolPolicy(ToolRuntimeConfig(workspace_root=Path("/tmp")))
        mock_executor = MagicMock()
        mock_executor.policy = policy
        app.tool_executor = mock_executor

        # AgentClient mock
        mock_agent_client = MagicMock()
        mock_agent_client.execution_mode = None
        app.agent_client = mock_agent_client

        app._set_execution_mode = QuimeraApp._set_execution_mode.__get__(app, QuimeraApp)
        app.parse_routing = QuimeraApp.parse_routing.__get__(app, QuimeraApp)

        with patch("quimera.app.core.plugins") as mock_plugins:
            mock_plugins.get = fake_get
            return app, mock_plugins, None

    def test_mode_command_sets_execution_mode(self):
        """Verifica que um comando de modo define execution_mode na instância do app."""
        app, _, _ = self._make_app()
        with patch("quimera.app.core.plugins") as mp:
            mp.get = lambda n: {"claude": MagicMock(prefix="/claude", name="claude"),
                                "codex": MagicMock(prefix="/codex", name="codex")}.get(n)
            app.parse_routing("/planning")
        self.assertIsNotNone(app.execution_mode)
        self.assertEqual(app.execution_mode.name, "planning")

    def test_mode_propagates_to_policy(self):
        app, _, _ = self._make_app()
        with patch("quimera.app.core.plugins") as mp:
            mp.get = lambda n: {"claude": MagicMock(prefix="/claude", name="claude"),
                                "codex": MagicMock(prefix="/codex", name="codex")}.get(n)
            app.parse_routing("/planning faz algo")
        self.assertIn("write_file", app.tool_executor.policy.blocked_tools)

    def test_mode_propagates_to_agent_client(self):
        app, _, _ = self._make_app()
        with patch("quimera.app.core.plugins") as mp:
            mp.get = lambda n: {"claude": MagicMock(prefix="/claude", name="claude"),
                                "codex": MagicMock(prefix="/codex", name="codex")}.get(n)
            app.parse_routing("/analysis analise o código")
        self.assertIsNotNone(app.agent_client.execution_mode)
        self.assertEqual(app.agent_client.execution_mode.name, "analysis")

    def test_execute_mode_clears_blocked_tools(self):
        app, _, _ = self._make_app()
        # Primeiro ativa planning
        app.tool_executor.policy.blocked_tools = ["write_file", "apply_patch"]
        with patch("quimera.app.core.plugins") as mp:
            mp.get = lambda n: {"claude": MagicMock(prefix="/claude", name="claude"),
                                "codex": MagicMock(prefix="/codex", name="codex")}.get(n)
            app.parse_routing("/execute faz a tarefa")
        self.assertEqual(app.tool_executor.policy.blocked_tools, [])

    def test_execute_mode_announces_previous_restrictions_were_removed(self):
        app, _, _ = self._make_app()
        with patch("quimera.app.core.plugins") as mp:
            mp.get = lambda n: {"claude": MagicMock(prefix="/claude", name="claude"),
                                "codex": MagicMock(prefix="/codex", name="codex")}.get(n)
            app.parse_routing("/execute faz a tarefa")
        app.renderer.show_system.assert_any_call(
            "[modo] execute ativado — restrições anteriores removidas; ferramentas bloqueadas: nenhuma"
        )

    def test_set_execution_mode_none_clears_blocked(self):
        app, _, _ = self._make_app()
        app.tool_executor.policy.blocked_tools = ["write_file"]
        app._set_execution_mode(None)
        self.assertEqual(app.tool_executor.policy.blocked_tools, [])
        self.assertIsNone(app.execution_mode)

    # --- novos: /modo sem texto retorna None como agente ---

    def test_planning_alone_returns_none_agent(self):
        app, _, _ = self._make_app()
        with patch("quimera.app.core.plugins") as mp:
            mp.get = lambda n: None
            agent, msg, explicit = app.parse_routing("/planning")
        self.assertIsNone(agent)
        self.assertEqual(msg, "")
        self.assertFalse(explicit)

    def test_analysis_alone_returns_none_agent(self):
        app, _, _ = self._make_app()
        with patch("quimera.app.core.plugins") as mp:
            mp.get = lambda n: None
            agent, msg, explicit = app.parse_routing("/analysis")
        self.assertIsNone(agent)

    def test_design_alone_returns_none_agent(self):
        app, _, _ = self._make_app()
        with patch("quimera.app.core.plugins") as mp:
            mp.get = lambda n: None
            agent, _, _ = app.parse_routing("/design")
        self.assertIsNone(agent)

    def test_review_alone_returns_none_agent(self):
        app, _, _ = self._make_app()
        with patch("quimera.app.core.plugins") as mp:
            mp.get = lambda n: None
            agent, _, _ = app.parse_routing("/review")
        self.assertIsNone(agent)

    def test_execute_alone_returns_none_agent(self):
        app, _, _ = self._make_app()
        with patch("quimera.app.core.plugins") as mp:
            mp.get = lambda n: None
            agent, _, _ = app.parse_routing("/execute")
        self.assertIsNone(agent)

    def test_mode_with_text_routes_to_agent(self):
        """Modo seguido de texto deve rotear normalmente (não retornar None)."""
        app, _, _ = self._make_app()
        mock_claude = MagicMock()
        mock_claude.prefix = "/claude"
        mock_claude.name = "claude"
        mock_claude.aliases = []
        mock_codex = MagicMock()
        mock_codex.prefix = "/codex"
        mock_codex.name = "codex"
        mock_codex.aliases = []
        app.active_agents = ["claude", "codex"]

        with patch("quimera.app.core.plugins") as mp:
            mp.get = lambda n: {"claude": mock_claude, "codex": mock_codex}.get(n)
            with patch.object(app, "get_active_agent_plugins", return_value=[mock_claude, mock_codex]):
                agent, msg, _ = app.parse_routing("/planning analisa o código")
        self.assertIsNotNone(agent)
        self.assertEqual(msg, "analisa o código")


class TestBuildInputPrompt(unittest.TestCase):
    """Testa _build_input_prompt: prompt visível com nome e modo."""

    def _make_app(self, mode_cmd=None, user_name="Você"):
        from quimera.app.core import QuimeraApp
        app = QuimeraApp.__new__(QuimeraApp)
        app.user_name = user_name
        app.execution_mode = get_mode(mode_cmd) if mode_cmd else None
        app._build_input_prompt = QuimeraApp._build_input_prompt.__get__(app, QuimeraApp)
        return app

    def test_no_mode_plain_prompt(self):
        app = self._make_app()
        self.assertEqual(app._build_input_prompt(), "Você: ")

    def test_execute_mode_plain_prompt(self):
        app = self._make_app("/execute")
        self.assertEqual(app._build_input_prompt(), "Você: ")

    def test_planning_shows_mode_label(self):
        app = self._make_app("/planning")
        self.assertEqual(app._build_input_prompt(), "Você [planning]: ")

    def test_analysis_shows_mode_label(self):
        app = self._make_app("/analysis")
        self.assertEqual(app._build_input_prompt(), "Você [analysis]: ")

    def test_design_shows_mode_label(self):
        app = self._make_app("/design")
        self.assertEqual(app._build_input_prompt(), "Você [design]: ")

    def test_review_shows_mode_label(self):
        app = self._make_app("/review")
        self.assertEqual(app._build_input_prompt(), "Você [review]: ")

    def test_custom_user_name(self):
        app = self._make_app("/planning", user_name="Alex")
        self.assertEqual(app._build_input_prompt(), "Alex [planning]: ")

    def test_symbol_name_preserved_as_fallback(self):
        app = self._make_app(user_name=">>>")
        self.assertEqual(app._build_input_prompt(), ">>> ")


class TestInputContextAndWelcome(unittest.TestCase):
    def test_build_input_toolbar_context_exposes_responder_and_model(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 1
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "capacity": 0, "active_agents": ()}
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        context = app._build_input_toolbar_context()
        self.assertEqual(context["responder"], "unknown")
        self.assertEqual(context["model"], "unknown")
        self.assertNotIn("cwd", context)

    def test_build_input_toolbar_context_exposes_parallel_status_when_threads_enabled(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 2
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 1, "queued": 2, "capacity": 2, "active_agents": ("codex",)}
        app.runtime_state.chat_inflight_count = 1
        app.runtime_state.chat_inflight_lock = threading.Lock()
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_chat_inflight_count = QuimeraApp._get_chat_inflight_count.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        context = app._build_input_toolbar_context()
        self.assertNotIn("threads", context)
        self.assertEqual(context["parallel"], "1/2 · 📥 2")

    def test_build_input_toolbar_context_defaults_capacity_to_thread_count(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 2
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "active_agents": ()}
        app.runtime_state.chat_inflight_count = 0
        app.runtime_state.chat_inflight_lock = threading.Lock()
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_chat_inflight_count = QuimeraApp._get_chat_inflight_count.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        context = app._build_input_toolbar_context()
        self.assertEqual(context["parallel"], "0/2")

    def test_build_input_toolbar_context_exposes_turns_when_history_available(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 1
        app.history = ["msg1", "msg2", "msg3"]
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "capacity": 0, "active_agents": ()}
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        context = app._build_input_toolbar_context()
        self.assertEqual(context.get("turns"), "3")

    def test_build_input_toolbar_context_omits_turns_when_no_history(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 1
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "capacity": 0, "active_agents": ()}
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        context = app._build_input_toolbar_context()
        self.assertNotIn("turns", context)

    def test_build_input_toolbar_context_tolerates_bug_store_exception(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 1
        app.history = ["msg"]
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "capacity": 0, "active_agents": ()}
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        # bug_store que lança exceção no query()
        app.bug_store = MagicMock()
        app.bug_store.query.side_effect = RuntimeError("query falhou")

        context = app._build_input_toolbar_context()
        self.assertEqual(context.get("turns"), "1")
        self.assertNotIn("open_bugs", context)

    def test_build_input_toolbar_context_caches_open_bugs_between_redraws(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 1
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "capacity": 0, "active_agents": ()}
        app._pending_input_for = None
        app.storage = MagicMock(session_id="sessao-12345678")
        app.bug_store = MagicMock()
        app.bug_store.query.return_value = [MagicMock(), MagicMock()]
        app._toolbar_bug_count_cache = {"session_id": "", "count": 0, "ts": 0.0}
        app._toolbar_bug_count_ttl_sec = 30.0
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        first = app._build_input_toolbar_context()
        second = app._build_input_toolbar_context()

        self.assertEqual(first.get("open_bugs"), "2")
        self.assertEqual(second.get("open_bugs"), "2")
        app.bug_store.query.assert_called_once_with(
            session_id="sessao-12345678", status="open", limit=100
        )

    def test_build_input_toolbar_context_exposes_mode_when_set(self):
        from quimera.app.core import QuimeraApp
        from quimera.modes import ExecutionMode

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 1
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "capacity": 0, "active_agents": ()}
        app._pending_input_for = None
        app.execution_mode = ExecutionMode(name="planning")
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        context = app._build_input_toolbar_context()
        self.assertEqual(context.get("mode"), "planning")

    def test_build_input_toolbar_context_omits_mode_when_none(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 1
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "capacity": 0, "active_agents": ()}
        app._pending_input_for = None
        app.execution_mode = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        context = app._build_input_toolbar_context()
        self.assertNotIn("mode", context)

    def test_build_input_toolbar_context_exposes_active_agents(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 2
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 1, "queued": 0, "capacity": 2, "active_agents": ("codex", "claude")}
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        context = app._build_input_toolbar_context()
        self.assertEqual(context.get("active_agents"), "codex, claude")

    def test_build_input_toolbar_context_compacts_many_active_agents(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 4
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {
            "active": 3,
            "queued": 0,
            "capacity": 4,
            "active_agents": ("codex", "claude", "qwen", "nemotron"),
        }
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        context = app._build_input_toolbar_context()
        self.assertEqual(context.get("active_agents"), "codex, claude, qwen +1")

    def test_build_input_toolbar_context_exposes_branch_when_set(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.workspace.branch = "feature-x"
        app.active_agents = []
        app.threads = 1
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "capacity": 0, "active_agents": ()}
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        context = app._build_input_toolbar_context()
        self.assertEqual(context.get("branch"), "feature-x")

    def test_build_input_toolbar_context_omits_branch_when_none(self):
        from quimera.app.core import QuimeraApp
 
        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        # Explicitly set branch to None to simulate absence
        app.workspace.branch = None
        app.active_agents = []
        app.threads = 1
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "capacity": 0, "active_agents": ()}
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)
 
        context = app._build_input_toolbar_context()
        self.assertNotIn("branch", context)

    def test_build_input_toolbar_context_omits_active_agents_when_empty(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.active_agents = []
        app.threads = 1
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "capacity": 1, "active_agents": ()}
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)

        context = app._build_input_toolbar_context()
        self.assertNotIn("active_agents", context)

    def _make_elapsed_app(self):
        """Retorna app mínimo para testar o campo elapsed."""
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/quimera-project"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app.workspace.branch = None
        app.active_agents = []
        app.threads = 1
        app._parallel_toolbar_lock = threading.Lock()
        app._parallel_toolbar_state = {"active": 0, "queued": 0, "capacity": 0, "active_agents": ()}
        app._pending_input_for = None
        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)
        app._get_parallel_toolbar_state = QuimeraApp._get_parallel_toolbar_state.__get__(app, QuimeraApp)
        app._build_input_toolbar_context = QuimeraApp._build_input_toolbar_context.__get__(app, QuimeraApp)
        return app

    def test_elapsed_formats_seconds(self):
        """elapsed < 60s → formato 'Xs'."""
        app = self._make_elapsed_app()
        app._session_started_at = 1000.0
        with patch("quimera.app.core.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0 + 45
            context = app._build_input_toolbar_context()
        self.assertEqual(context.get("elapsed"), "45s")

    def test_elapsed_formats_minutes_and_seconds(self):
        """60s <= elapsed < 3600s → formato 'Xm XXs'."""
        app = self._make_elapsed_app()
        app._session_started_at = 1000.0
        with patch("quimera.app.core.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0 + 754  # 12m 34s
            context = app._build_input_toolbar_context()
        self.assertEqual(context.get("elapsed"), "12m 34s")

    def test_elapsed_formats_hours(self):
        """elapsed >= 3600s → formato 'Xh XXm'."""
        app = self._make_elapsed_app()
        app._session_started_at = 1000.0
        with patch("quimera.app.core.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0 + 4530  # 1h 15m
            context = app._build_input_toolbar_context()
        self.assertEqual(context.get("elapsed"), "1h 15m")

    def test_elapsed_without_session_started_at_shows_zero(self):
        """Sem _session_started_at, elapsed cai para ~0s → formato 'Xs'."""
        app = self._make_elapsed_app()
        # _session_started_at não definido; monotonic chamado duas vezes com mesmo valor
        with patch("quimera.app.core.time") as mock_time:
            mock_time.monotonic.return_value = 5000.0
            context = app._build_input_toolbar_context()
        self.assertEqual(context.get("elapsed"), "0s")

    def test_build_welcome_message_includes_version_and_project_path(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.workspace = MagicMock(cwd=Path("/tmp/projeto"), tasks_db=Path("/tmp/quimera_test_tasks.db"))
        app._build_welcome_message = QuimeraApp._build_welcome_message.__get__(app, QuimeraApp)

        with patch.object(QuimeraApp, "_resolve_app_version", return_value="0.1.0"):
            message = app._build_welcome_message()

        self.assertIn("v0.1.0", message)
        self.assertNotIn("projeto:", message)

    def test_resolver_next_responder_prefers_pending_input_target(self):
        from quimera.app.core import QuimeraApp

        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = ["codex", "claude"]
        app._pending_input_for = "claude"
        app._resolve_next_responder_label = QuimeraApp._resolve_next_responder_label.__get__(app, QuimeraApp)

        self.assertEqual(app._resolve_next_responder_label(), "claude")

    def test_resolve_active_model_label_extracts_model_from_cli_equals(self):
        from quimera.app.core import QuimeraApp
        from quimera.plugins.base import CliConnection

        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = ["codex"]

        plugin = MagicMock()
        plugin.name = "codex"
        plugin.model = None
        plugin.cmd = ["codex", "exec", "--model=codex-5", "--json"]
        plugin.effective_connection.return_value = CliConnection(cmd=list(plugin.cmd))
        app.get_agent_plugin = MagicMock(return_value=plugin)

        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        self.assertEqual(app._resolve_active_model_label(), "codex-5")

    def test_resolve_active_model_label_extracts_model_from_cli_next_arg(self):
        from quimera.app.core import QuimeraApp
        from quimera.plugins.base import CliConnection

        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = ["opencode"]

        plugin = MagicMock()
        plugin.name = "opencode"
        plugin.model = None
        plugin.cmd = ["opencode", "--model", "gpt-5-mini", "run"]
        plugin.effective_connection.return_value = CliConnection(cmd=list(plugin.cmd))
        app.get_agent_plugin = MagicMock(return_value=plugin)

        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        self.assertEqual(app._resolve_active_model_label(), "gpt-5-mini")

    def test_resolve_active_model_label_falls_back_to_plugin_name_when_cli_has_no_model(self):
        from quimera.app.core import QuimeraApp
        from quimera.plugins.base import CliConnection

        app = QuimeraApp.__new__(QuimeraApp)
        app.active_agents = ["claude"]

        plugin = MagicMock()
        plugin.name = "claude"
        plugin.model = None
        plugin.cmd = ["claude", "--output-format=stream-json", "-p"]
        plugin.effective_connection.return_value = CliConnection(cmd=list(plugin.cmd))
        app.get_agent_plugin = MagicMock(return_value=plugin)

        app._resolve_active_model_label = QuimeraApp._resolve_active_model_label.__get__(app, QuimeraApp)
        plugin.resolve_runtime_model.return_value = None
        self.assertEqual(app._resolve_active_model_label(), "claude")


class TestCliRuntimeModelResolution(unittest.TestCase):
    def test_codex_plugin_resolve_runtime_model_reads_codex_config(self):
        from quimera.plugins import get

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex_dir = home / ".codex"
            codex_dir.mkdir(parents=True, exist_ok=True)
            (codex_dir / "config.toml").write_text(
                'model = "gpt-5.4"\nmodel_reasoning_effort = "high"\n[projects."/tmp"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )

            plugin = get("codex")
            self.assertIsNotNone(plugin)
            with patch("quimera.plugins.codex.Path.home", return_value=home):
                model = plugin.resolve_runtime_model(cwd="/tmp")

        self.assertEqual(model, "gpt-5.4")

    def test_claude_plugin_resolve_runtime_model_reads_project_last_model_usage(self):
        from quimera.plugins import get

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            (claude_dir / "settings.json").write_text("{}", encoding="utf-8")
            state = {
                "projects": {
                    "/tmp": {
                        "lastModelUsage": {
                            "claude-sonnet-4-6": {
                                "inputTokens": 1,
                            }
                        }
                    }
                }
            }
            (home / ".claude.json").write_text(json.dumps(state), encoding="utf-8")

            plugin = get("claude")
            self.assertIsNotNone(plugin)
            with patch("quimera.plugins.claude.Path.home", return_value=home):
                model = plugin.resolve_runtime_model(cwd="/tmp/projeto")

        self.assertEqual(model, "claude-sonnet-4-6")


# =========================================================================
# Fase 0 — Guardrails: contratos públicos de modes
# =========================================================================


class TestModeGuardrails(unittest.TestCase):
    """Guardrails mínimos para o módulo modes."""

    def test_get_mode_none_returns_none(self):
        """get_mode(None) retorna None sem levantar."""
        self.assertIsNone(get_mode(None))

    def test_get_mode_whitespace_returns_none(self):
        """get_mode com espaços retorna None."""
        self.assertIsNone(get_mode("   "))

    def test_all_modes_have_unique_names(self):
        """Cada modo tem name único."""
        names = [m.name for m in MODES.values()]
        self.assertEqual(len(names), len(set(names)))

    def test_execution_mode_defaults(self):
        """ExecutionMode default tem valores esperados."""
        mode = ExecutionMode(name="test")
        self.assertEqual(mode.name, "test")
        self.assertFalse(mode.read_only_fs)
        self.assertTrue(mode.allow_network)
        self.assertEqual(mode.blocked_tools, [])
        self.assertEqual(mode.prompt_addon, "")

    def test_planning_mode_allows_shell(self):
        """/planning permite shell, só bloqueia write_file/apply_patch."""
        mode = get_mode("/planning")
        self.assertNotIn("run_shell", mode.blocked_tools)
        self.assertNotIn("exec_command", mode.blocked_tools)

    def test_review_mode_rejects_execution(self):
        """/review bloqueia execução além de escrita."""
        mode = get_mode("/review")
        self.assertIn("run_shell", mode.blocked_tools)
        self.assertIn("exec_command", mode.blocked_tools)

    def test_get_mode_preserves_registered_keys(self):
        """Todos os modos registrados em MODES são acessíveis via get_mode."""
        for key in MODES:
            self.assertIsNotNone(get_mode(key))
            self.assertEqual(get_mode(key).name, MODES[key].name)

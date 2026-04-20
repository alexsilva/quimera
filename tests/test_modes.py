"""Testes para quimera.modes e integração com ToolPolicy e parse_routing."""
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from quimera.modes import MODES, get_mode
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicy, ToolPolicyError


class TestExecutionMode(unittest.TestCase):
    def test_all_modes_defined(self):
        for key in ["/planning", "/analysis", "/design", "/review", "/execute"]:
            self.assertIn(key, MODES)

    def test_get_mode_returns_correct_mode(self):
        mode = get_mode("/planning")
        self.assertIsNotNone(mode)
        self.assertEqual(mode.name, "planning")

    def test_get_mode_case_insensitive(self):
        self.assertIsNotNone(get_mode("/PLANNING"))
        self.assertIsNotNone(get_mode("/Analysis"))

    def test_get_mode_unknown_returns_none(self):
        self.assertIsNone(get_mode("/unknown"))
        self.assertIsNone(get_mode("planning"))
        self.assertIsNone(get_mode(""))

    def test_planning_mode_properties(self):
        mode = get_mode("/planning")
        self.assertTrue(mode.read_only_fs)
        self.assertTrue(mode.allow_network)
        self.assertIn("write_file", mode.blocked_tools)
        self.assertIn("apply_patch", mode.blocked_tools)
        self.assertNotIn("run_shell", mode.blocked_tools)
        self.assertNotIn("exec_command", mode.blocked_tools)
        self.assertNotEqual(mode.prompt_addon, "")

    def test_analysis_mode_allows_network(self):
        mode = get_mode("/analysis")
        self.assertTrue(mode.read_only_fs)
        self.assertTrue(mode.allow_network)
        self.assertIn("write_file", mode.blocked_tools)
        self.assertNotIn("run_shell", mode.blocked_tools)

    def test_execute_mode_no_restrictions(self):
        mode = get_mode("/execute")
        self.assertFalse(mode.read_only_fs)
        self.assertTrue(mode.allow_network)
        self.assertEqual(mode.blocked_tools, [])
        self.assertIn("não muda a intenção", mode.prompt_addon.lower())
        self.assertIn("se o humano pedir análise, analise", mode.prompt_addon.lower())

    def test_design_review_are_read_only_with_network(self):
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
        policy = self._make_policy()
        self.assertEqual(policy.blocked_tools, [])

    def test_blocked_tool_raises(self):
        policy = self._make_policy(blocked=["write_file"])
        call = self._call("write_file", {"path": "foo.py", "content": "x"})
        with self.assertRaises(ToolPolicyError) as ctx:
            policy.validate(call)
        self.assertIn("bloqueada", str(ctx.exception))

    def test_non_blocked_tool_passes_policy(self):
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
        blocked = ["write_file", "apply_patch", "run_shell"]
        policy = self._make_policy(blocked=blocked)
        for tool in blocked:
            call = self._call(tool, {"path": "x", "content": "y", "patch": "z", "command": "ls"})
            with self.assertRaises(ToolPolicyError):
                policy.validate(call)

    def test_blocked_tools_cleared(self):
        policy = self._make_policy(blocked=["write_file"])
        policy.blocked_tools = []
        # write_file now must pass the blocked check (may fail for other reasons)
        call = self._call("write_file", {"path": "/tmp/test.txt", "content": "x"})
        try:
            policy.validate(call)
        except ToolPolicyError as exc:
            self.assertNotIn("bloqueada", str(exc))


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

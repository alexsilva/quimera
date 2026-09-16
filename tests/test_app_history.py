import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestAppHistory(unittest.TestCase):
    """Testes para o histórico do app Quimera."""

    def setUp(self):
        self.tmp_cwd = Path("/tmp/quimera_test_cwd")
        self.history_file = Path("/tmp/quimera_test_workspace/data/history/test.jsonl")

    def _setup_common_mocks(self, mock_storage, mock_context):
        mock_storage.return_value.get_history_file.return_value = Path("test.json")
        mock_storage.return_value.session_id = "test"
        mock_storage.return_value.load_last_session.return_value = {"messages": [], "shared_state": {}}
        mock_context.SUMMARY_MARKER = "SUMMARY"
        mock_context.load_session.return_value = ""

    def _make_input_gate_factory(self, target_kwargs: dict | None = None):
        instance = MagicMock()
        def factory(**kw):
            if target_kwargs is not None:
                target_kwargs.update(kw)
                target_kwargs["instance"] = instance
            return instance
        return factory

    @patch("quimera.tasks.api.init_db")
    @patch("quimera.tasks.api.add_job")
    @patch("quimera.app.bootstrap.wiring.TerminalRenderer")
    @patch("quimera.app.bootstrap.wiring.RenderAuditLogger")
    @patch("quimera.app.bootstrap.wiring.ConfigManager")
    @patch("quimera.app.bootstrap.wiring.ContextManager")
    @patch("quimera.app.bootstrap.wiring.SessionStorage")
    @patch("quimera.app.bootstrap.wiring.AgentClient")
    @patch("quimera.app.bootstrap.wiring.SessionSummarizer")
    def test_input_gate_receives_history_file_and_command_resolver(
        self,
        mock_session_sum,
        mock_agent,
        mock_storage,
        mock_context,
        mock_config,
        mock_audit_logger,
        mock_term,
        mock_add_job,
        mock_init_db,
    ):
        """Verifica que o InputGate recebe o history_file e o command_resolver corretamente."""
        mock_add_job.return_value = 1
        self._setup_common_mocks(mock_storage, mock_context)

        captured = {}
        input_gate_factory = self._make_input_gate_factory(captured)

        tmp_root = Path("/tmp/quimera_test_workspace_tmp")
        mock_ws_instance = MagicMock()
        mock_ws_instance.cwd = Path("/tmp/quimera_test_cwd")
        mock_ws_instance.cwd_hash = "test-workspace"
        mock_ws_instance.history_file_for.return_value = self.history_file
        mock_ws_instance.root = Path("/tmp/quimera_test_workspace")
        mock_ws_instance.tasks_db = Path("/tmp/quimera_test_tasks.db")

        with patch("quimera.app.bootstrap.wiring.create_executor"):
            from quimera.app import QuimeraApp

            app = QuimeraApp(
                self.tmp_cwd,
                workspace=mock_ws_instance,
                input_gate_factory=input_gate_factory,
            )

        self.assertEqual(captured["history_file"], self.history_file)
        self.assertTrue(callable(captured["command_resolver"]))
        captured["instance"].set_toolbar_context_resolver.assert_called_once_with(
            app.toolbar_coordinator.build_input_toolbar_context
        )

    @patch("quimera.tasks.api.init_db")
    @patch("quimera.tasks.api.add_job")
    @patch("quimera.app.bootstrap.wiring.TerminalRenderer")
    @patch("quimera.app.bootstrap.wiring.ConfigManager")
    @patch("quimera.app.bootstrap.wiring.ContextManager")
    @patch("quimera.app.bootstrap.wiring.SessionStorage")
    @patch("quimera.app.bootstrap.wiring.AgentClient")
    @patch("quimera.app.bootstrap.wiring.SessionSummarizer")
    def test_read_user_input_delegates_to_input_gate(
        self,
        mock_session_sum,
        mock_agent,
        mock_storage,
        mock_context,
        mock_config,
        mock_term,
        mock_add_job,
        mock_init_db,
    ):
        """Verifica que read_user_input delega para input_gate.read_input."""
        mock_add_job.return_value = 1
        self._setup_common_mocks(mock_storage, mock_context)

        gate_mock = MagicMock()
        gate_mock.read_input.return_value = "test input"
        gate_mock.return_value = "test input"

        tmp_root = Path("/tmp/quimera_test_workspace_tmp")
        mock_ws_instance = MagicMock()
        mock_ws_instance.cwd = Path("/tmp/quimera_test_cwd")
        mock_ws_instance.cwd_hash = "test-workspace"
        mock_ws_instance.history_file_for.return_value = self.history_file
        mock_ws_instance.root = Path("/tmp/quimera_test_workspace")
        mock_ws_instance.tasks_db = Path("/tmp/quimera_test_tasks.db")
        mock_ws_instance.render_logs_dir = Path("/tmp/quimera_test_workspace/data/logs/render")

        with patch("quimera.app.bootstrap.wiring.create_executor"):
            from quimera.app import QuimeraApp

            app = QuimeraApp(
                self.tmp_cwd,
                workspace=mock_ws_instance,
                input_gate_factory=lambda **kw: gate_mock,
            )

        result = app.input_services.read_user_input(prompt="user: ", timeout=-1)

        self.assertEqual(result, "test input")
        gate_mock.assert_called_once_with("user: ")

    @patch("quimera.tasks.api.init_db")
    @patch("quimera.tasks.api.add_job")
    @patch("quimera.app.bootstrap.wiring.TerminalRenderer")
    @patch("quimera.app.bootstrap.wiring.RenderAuditLogger")
    @patch("quimera.app.bootstrap.wiring.ConfigManager")
    @patch("quimera.app.bootstrap.wiring.ContextManager")
    @patch("quimera.app.bootstrap.wiring.SessionStorage")
    @patch("quimera.app.bootstrap.wiring.AgentClient")
    @patch("quimera.app.bootstrap.wiring.SessionSummarizer")
    def test_debug_mode_injects_render_audit_logger(
        self,
        mock_session_sum,
        mock_agent,
        mock_storage,
        mock_context,
        mock_config,
        mock_audit_logger,
        mock_term,
        mock_add_job,
        mock_init_db,
    ):
        """Verifica que o modo debug injeta o RenderAuditLogger no renderer."""
        mock_add_job.return_value = 1
        self._setup_common_mocks(mock_storage, mock_context)

        audit_instance = MagicMock()
        mock_audit_logger.return_value = audit_instance

        mock_ws_instance = MagicMock()
        mock_ws_instance.cwd = Path("/tmp/quimera_test_cwd")
        mock_ws_instance.cwd_hash = "test-workspace"
        mock_ws_instance.history_file_for.return_value = self.history_file
        mock_ws_instance.root = Path("/tmp/quimera_test_workspace")
        mock_ws_instance.tasks_db = Path("/tmp/quimera_test_tasks.db")

        with patch("quimera.app.bootstrap.wiring.create_executor"):
            from quimera.app import QuimeraApp

            app = QuimeraApp(
                self.tmp_cwd,
                workspace=mock_ws_instance,
                debug=True,
                input_gate_factory=lambda **kw: MagicMock(),
            )

        mock_audit_logger.assert_called_once_with(
            app.session_paths.render_log_path_for("test"),
            app.session_paths.render_ansi_path_for("test"),
        )
        _, kwargs = mock_term.call_args
        self.assertIs(kwargs["audit_logger"], audit_instance)


if __name__ == "__main__":
    unittest.main()

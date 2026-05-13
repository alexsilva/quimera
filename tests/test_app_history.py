import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestAppHistory(unittest.TestCase):
    def setUp(self):
        self.tmp_cwd = Path("/tmp/quimera_test_cwd")
        self.history_file = Path("/tmp/quimera_test_workspace/history")

    def _setup_common_mocks(self, mock_storage, mock_context):
        mock_storage.get_history_file.return_value = Path("test.json")
        mock_storage.load_last_session.return_value = {"messages": [], "shared_state": {}}
        mock_context.SUMMARY_MARKER = "SUMMARY"
        mock_context.load_session.return_value = ""

    @patch("quimera.runtime.tasks.init_db")
    @patch("quimera.runtime.tasks.add_job")
    @patch("quimera.app.core.TerminalRenderer")
    @patch("quimera.app.core.ConfigManager")
    @patch("quimera.app.core.ContextManager")
    @patch("quimera.app.core.SessionStorage")
    @patch("quimera.app.core.AgentClient")
    @patch("quimera.app.core.SessionSummarizer")
    @patch("quimera.app.core.InputGate")
    def test_input_gate_receives_history_file_and_command_resolver(
        self,
        mock_input_gate,
        mock_session_sum,
        mock_agent,
        mock_storage,
        mock_context,
        mock_config,
        mock_term,
        mock_add_job,
        mock_init_db,
    ):
        mock_add_job.return_value = 1
        self._setup_common_mocks(mock_storage, mock_context)
        mock_gate_instance = MagicMock()
        mock_input_gate.return_value = mock_gate_instance

        with patch("quimera.app.core.Workspace") as mock_ws:
            mock_ws_instance = MagicMock()
            mock_ws_instance.history_file = self.history_file
            mock_ws_instance.root = Path("/tmp/quimera_test_workspace")
            mock_ws_instance.tasks_db = Path("/tmp/quimera_test_tasks.db")
            mock_ws.return_value = mock_ws_instance

            with patch("quimera.app.core.create_executor"):
                from quimera.app import QuimeraApp

                app = QuimeraApp(self.tmp_cwd)

        _, kwargs = mock_input_gate.call_args
        self.assertEqual(kwargs["history_file"], self.history_file)
        self.assertTrue(callable(kwargs["command_resolver"]))
        mock_gate_instance.set_toolbar_context_resolver.assert_called_once_with(app._build_input_toolbar_context)

    @patch("quimera.runtime.tasks.init_db")
    @patch("quimera.runtime.tasks.add_job")
    @patch("quimera.app.core.TerminalRenderer")
    @patch("quimera.app.core.ConfigManager")
    @patch("quimera.app.core.ContextManager")
    @patch("quimera.app.core.SessionStorage")
    @patch("quimera.app.core.AgentClient")
    @patch("quimera.app.core.SessionSummarizer")
    @patch("builtins.input", return_value="test input")
    def test_read_user_input_uses_input_function(
        self,
        mock_input,
        mock_session_sum,
        mock_agent,
        mock_storage,
        mock_context,
        mock_config,
        mock_term,
        mock_add_job,
        mock_init_db,
    ):
        mock_add_job.return_value = 1
        self._setup_common_mocks(mock_storage, mock_context)

        with patch("quimera.app.core.Workspace") as mock_ws:
            mock_ws_instance = MagicMock()
            mock_ws_instance.history_file = self.history_file
            mock_ws_instance.root = Path("/tmp/quimera_test_workspace")
            mock_ws_instance.tasks_db = Path("/tmp/quimera_test_tasks.db")
            mock_ws.return_value = mock_ws_instance

            with patch("quimera.app.core.create_executor"):
                from quimera.app import QuimeraApp

                app = QuimeraApp(self.tmp_cwd)
            app.user_name = "user"
            app.input_gate._session = None

            result = app.input_services.read_user_input(prompt="user: ", timeout=-1)

            self.assertEqual(result, "test input")
            mock_input.assert_called_with("user: ")


if __name__ == "__main__":
    unittest.main()

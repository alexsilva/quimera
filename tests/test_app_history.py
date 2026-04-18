import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestAppHistory(unittest.TestCase):
    def setUp(self):
        self.tmp_cwd = Path("/tmp/quimera_test_cwd")
        self.history_file = Path("/tmp/quimera_test_workspace/history")

    def _setup_common_mocks(self, mock_storage, mock_context):
        mock_storage.get_history_file.return_value = Path("test.json")
        mock_storage.load_last_session.return_value = {"messages": [], "shared_state": {}}
        mock_context.SUMMARY_MARKER = "SUMMARY"
        mock_context.load_session.return_value = ""

    @patch('quimera.runtime.tasks.init_db')
    @patch('quimera.runtime.tasks.add_job')
    @patch('quimera.app.core.TerminalRenderer')
    @patch('quimera.app.core.ConfigManager')
    @patch('quimera.app.core.ContextManager')
    @patch('quimera.app.core.SessionStorage')
    @patch('quimera.app.core.AgentClient')
    @patch('quimera.app.core.SessionSummarizer')
    @patch('quimera.app.core.readline')
    def test_history_loading_on_init(self, mock_readline, mock_session_sum, mock_agent, mock_storage, mock_context, mock_config, mock_term, mock_add_job, mock_init_db):
        mock_add_job.return_value = 1
        self._setup_common_mocks(mock_storage, mock_context)
        
        with patch('quimera.app.core.Workspace') as mock_ws:
            mock_ws_instance = MagicMock()
            mock_ws_instance.history_file = self.history_file
            mock_ws_instance.root = Path("/tmp/quimera_test_workspace")
            mock_ws.return_value = mock_ws_instance
            
            with patch('quimera.app.core.create_executor'):
                with patch.object(Path, 'exists', return_value=True):
                    from quimera.app import QuimeraApp
                    QuimeraApp(self.tmp_cwd)
                    
                    mock_readline.read_history_file.assert_called_with(str(self.history_file))
                    mock_readline.set_history_length.assert_called_with(1000)
                    mock_readline.set_completer_delims.assert_called_with(" \t\n")
                    mock_readline.parse_and_bind.assert_called_with("tab: complete")

    @patch('quimera.runtime.tasks.init_db')
    @patch('quimera.runtime.tasks.add_job')
    @patch('quimera.app.core.TerminalRenderer')
    @patch('quimera.app.core.ConfigManager')
    @patch('quimera.app.core.ContextManager')
    @patch('quimera.app.core.SessionStorage')
    @patch('quimera.app.core.AgentClient')
    @patch('quimera.app.core.SessionSummarizer')
    @patch('quimera.app.core.readline')
    def test_readline_completer_completes_slash_commands_without_duplication(self, mock_readline, mock_session_sum, mock_agent, mock_storage, mock_context, mock_config, mock_term, mock_add_job, mock_init_db):
        mock_add_job.return_value = 1
        self._setup_common_mocks(mock_storage, mock_context)

        with patch('quimera.app.core.Workspace') as mock_ws:
            mock_ws_instance = MagicMock()
            mock_ws_instance.history_file = self.history_file
            mock_ws_instance.root = Path("/tmp/quimera_test_workspace")
            mock_ws.return_value = mock_ws_instance

            with patch('quimera.app.core.create_executor'):
                from quimera.app import QuimeraApp
                QuimeraApp(self.tmp_cwd)

        completer = mock_readline.set_completer.call_args.args[0]

        c_matches = []
        state = 0
        while True:
            match = completer("/c", state)
            if match is None:
                break
            c_matches.append(match)
            state += 1

        self.assertIn("/clear", c_matches)
        self.assertEqual(len(c_matches), len(set(c_matches)))
        self.assertEqual(completer("/h", 0), "/help")
        self.assertIsNone(completer("/h", 1))
        self.assertIsNone(completer("h", 0))

    @patch('quimera.runtime.tasks.init_db')
    @patch('quimera.runtime.tasks.add_job')
    @patch('quimera.app.core.TerminalRenderer')
    @patch('quimera.app.core.ConfigManager')
    @patch('quimera.app.core.ContextManager')
    @patch('quimera.app.core.SessionStorage')
    @patch('quimera.app.core.AgentClient')
    @patch('quimera.app.core.SessionSummarizer')
    @patch('quimera.app.core.readline')
    def test_history_saving_on_shutdown(self, mock_readline, mock_session_sum, mock_agent, mock_storage, mock_context, mock_config, mock_term, mock_add_job, mock_init_db):
        mock_add_job.return_value = 1
        self._setup_common_mocks(mock_storage, mock_context)
        
        with patch('quimera.app.core.Workspace') as mock_ws:
            mock_ws_instance = MagicMock()
            mock_ws_instance.history_file = self.history_file
            mock_ws_instance.root = Path("/tmp/quimera_test_workspace")
            mock_ws.return_value = mock_ws_instance
            
            with patch('quimera.app.core.create_executor'):
                from quimera.app import QuimeraApp
                app = QuimeraApp(self.tmp_cwd)
                
                app.shutdown()
                
                mock_readline.write_history_file.assert_called_with(str(self.history_file))

    @patch('quimera.runtime.tasks.init_db')
    @patch('quimera.runtime.tasks.add_job')
    @patch('quimera.app.core.TerminalRenderer')
    @patch('quimera.app.core.ConfigManager')
    @patch('quimera.app.core.ContextManager')
    @patch('quimera.app.core.SessionStorage')
    @patch('quimera.app.core.AgentClient')
    @patch('quimera.app.core.SessionSummarizer')
    @patch('quimera.app.core.readline')
    @patch('builtins.input', return_value="test input")
    def test_read_user_input_uses_input_function(self, mock_input, mock_readline, mock_session_sum, mock_agent, mock_storage, mock_context, mock_config, mock_term, mock_add_job, mock_init_db):
        mock_add_job.return_value = 1
        self._setup_common_mocks(mock_storage, mock_context)
        
        with patch('quimera.app.core.Workspace') as mock_ws:
            mock_ws_instance = MagicMock()
            mock_ws_instance.history_file = self.history_file
            mock_ws_instance.root = Path("/tmp/quimera_test_workspace")
            mock_ws.return_value = mock_ws_instance

            with patch('quimera.app.core.create_executor'):
                from quimera.app import QuimeraApp
                app = QuimeraApp(self.tmp_cwd)
            app.user_name = "user"
            
            result = app.read_user_input(prompt="user: ", timeout=30)
            
            self.assertEqual(result, "test input")
            mock_input.assert_called_with("user: ")

if __name__ == '__main__':
    unittest.main()

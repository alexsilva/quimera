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

    @patch('quimera.app.TerminalRenderer')
    @patch('quimera.app.ConfigManager')
    @patch('quimera.app.ContextManager')
    @patch('quimera.app.SessionStorage')
    @patch('quimera.app.AgentClient')
    @patch('quimera.app.SessionSummarizer')
    @patch('quimera.app.readline')
    def test_history_loading_on_init(self, mock_readline, mock_session_sum, mock_agent, mock_storage, mock_context, mock_config, mock_term):
        self._setup_common_mocks(mock_storage, mock_context)
        
        with patch('quimera.app.Workspace') as mock_ws:
            mock_ws_instance = MagicMock()
            mock_ws_instance.history_file = self.history_file
            mock_ws.return_value = mock_ws_instance
            
            with patch.object(Path, 'exists', return_value=True):
                from quimera.app import QuimeraApp
                app = QuimeraApp(self.tmp_cwd)
                
                mock_readline.read_history_file.assert_called_with(str(self.history_file))
                mock_readline.set_history_length.assert_called_with(1000)

    @patch('quimera.app.TerminalRenderer')
    @patch('quimera.app.ConfigManager')
    @patch('quimera.app.ContextManager')
    @patch('quimera.app.SessionStorage')
    @patch('quimera.app.AgentClient')
    @patch('quimera.app.SessionSummarizer')
    @patch('quimera.app.readline')
    def test_history_saving_on_shutdown(self, mock_readline, mock_session_sum, mock_agent, mock_storage, mock_context, mock_config, mock_term):
        self._setup_common_mocks(mock_storage, mock_context)
        
        with patch('quimera.app.Workspace') as mock_ws:
            mock_ws_instance = MagicMock()
            mock_ws_instance.history_file = self.history_file
            mock_ws.return_value = mock_ws_instance
            
            from quimera.app import QuimeraApp
            app = QuimeraApp(self.tmp_cwd)
            
            app.shutdown()
            
            mock_readline.write_history_file.assert_called_with(str(self.history_file))

    @patch('quimera.app.TerminalRenderer')
    @patch('quimera.app.ConfigManager')
    @patch('quimera.app.ContextManager')
    @patch('quimera.app.SessionStorage')
    @patch('quimera.app.AgentClient')
    @patch('quimera.app.SessionSummarizer')
    @patch('quimera.app.readline')
    @patch('quimera.app.input', return_value="test input")
    def test_read_user_input_uses_input_function(self, mock_input, mock_readline, mock_session_sum, mock_agent, mock_storage, mock_context, mock_config, mock_term):
        self._setup_common_mocks(mock_storage, mock_context)
        
        with patch('quimera.app.Workspace') as mock_ws:
            mock_ws_instance = MagicMock()
            mock_ws_instance.history_file = self.history_file
            mock_ws.return_value = mock_ws_instance

            from quimera.app import QuimeraApp
            app = QuimeraApp(self.tmp_cwd)
            app.user_name = "user"
            
            result = app.read_user_input()
            
            self.assertEqual(result, "test input")
            mock_input.assert_called_with("user: ")

if __name__ == '__main__':
    unittest.main()

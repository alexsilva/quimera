import unittest
from unittest.mock import Mock, patch
import logging
from quimera.agents import AgentClient

class TestLoggingFix(unittest.TestCase):
    def test_agent_client_run_logs_when_silent(self):
        renderer = Mock()
        client = AgentClient(renderer)
        
        # Mock subprocess.Popen
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = mock_popen.return_value
            mock_proc.stdout = ["stdout line 1\n", "stdout line 2\n"]
            mock_proc.stderr = ["stderr line 1\n"]
            mock_proc.stdin = Mock()
            mock_proc.returncode = 0
            mock_proc.wait.return_value = 0
            
            with self.assertLogs("quimera.agents", level="DEBUG") as cm:
                result = client.run(["test_cmd"], silent=True)
                
            self.assertEqual(result, "stdout line 1\nstdout line 2")
            
            # Check if DEBUG and WARNING messages are in the logs
            self.assertTrue(any("stdout line 1\nstdout line 2\n" in output for output in cm.output))
            self.assertTrue(any("stderr line 1\n" in output for output in cm.output))

if __name__ == "__main__":
    unittest.main()

"""Tests for quimera/runtime/tool_preview.py."""
import unittest

from quimera.runtime.tool_preview import ToolPreview


class TestBuildDispatch(unittest.TestCase):
    def test_known_tool_dispatches(self):
        result = ToolPreview.build("read_file", {"path": "/tmp/x"}, context="approval")
        self.assertEqual("path: /tmp/x", result)

    def test_unknown_tool_fallback(self):
        result = ToolPreview.build("my_custom_tool", {"key": "val"}, context="approval")
        self.assertEqual("  key: val", result)


class TestApprovalPreview(unittest.TestCase):
    def test_write_file_with_content(self):
        result = ToolPreview.build("write_file", {"path": "/a/b.py", "content": "x = 1"}, context="approval")
        self.assertIn("/a/b.py", result)
        self.assertIn("x = 1", result)

    def test_write_file_without_content(self):
        result = ToolPreview.build("write_file", {"path": "/a/b.py"}, context="approval")
        self.assertIn("/a/b.py", result)

    def test_apply_patch(self):
        result = ToolPreview.build("apply_patch", {"patch": "--- a\n+++ b"}, context="approval")
        self.assertIn("patch textual", result)

    def test_remove_file_dry_run(self):
        result = ToolPreview.build("remove_file", {"path": "/tmp/f", "dry_run": True}, context="approval")
        self.assertIn("dry-run", result)

    def test_remove_file_real(self):
        result = ToolPreview.build("remove_file", {"path": "/tmp/f", "dry_run": False}, context="approval")
        self.assertIn("REMO", result)

    def test_exec_command_flags(self):
        result = ToolPreview.build(
            "exec_command",
            {"cmd": "bash", "login": True, "tty": True, "yield_time_ms": 100, "workdir": "/home/user"},
            context="approval",
        )
        self.assertIn("login", result)
        self.assertIn("tty", result)
        self.assertIn("yield=100ms", result)
        self.assertIn("/home/user", result)

    def test_exec_command_can_omit_command_line(self):
        result = ToolPreview.build(
            "exec_command",
            {"cmd": "pwd", "tty": True, "workdir": "/tmp"},
            context="approval",
            omit_fields={"command"},
        )
        self.assertNotIn("comando: pwd", result)
        self.assertIn("flags: tty", result)
        self.assertIn("workdir: /tmp", result)

    def test_run_shell_can_omit_command_line(self):
        result = ToolPreview.build(
            "run_shell",
            {"command": "pwd", "workdir": "/tmp"},
            context="approval",
            omit_fields={"command"},
        )
        self.assertNotIn("comando: pwd", result)
        self.assertIn("workdir: /tmp", result)

    def test_write_stdin(self):
        result = ToolPreview.build("write_stdin", {"session_id": "abc", "chars": "hello"}, context="approval")
        self.assertIn("abc", result)
        self.assertIn("hello", result)

    def test_close_command_session(self):
        result = ToolPreview.build("close_command_session", {"session_id": "x", "terminate": True}, context="approval")
        self.assertIn("x", result)
        self.assertIn("terminate", result)

    def test_read_tools(self):
        result = ToolPreview.build("grep_search", {"pattern": "foo", "path": "/src"}, context="approval")
        self.assertIn("foo", result)
        self.assertIn("/src", result)

    def test_web_search_defaults(self):
        result = ToolPreview.build("web_search", {"query": "test"}, context="approval")
        self.assertIn("5", result)

    def test_git_add_approval_preview_is_compact(self):
        result = ToolPreview.build("git_add", {"paths": "."}, context="approval")
        self.assertEqual("git add: .", result)

    def test_git_add_approval_preview_formats_multiple_paths(self):
        result = ToolPreview.build("git_add", {"paths": ["a.py", "b.py"]}, context="approval")
        self.assertEqual("git add: a.py, b.py", result)

    def test_unknown_truncates(self):
        long_val = "x" * 200
        result = ToolPreview.build("weird_tool", {"key": long_val}, context="approval")
        self.assertIn("…", result)
        self.assertLess(len(result), 300)

    def test_write_file_preview_truncates_after_six_lines(self):
        content = "\n".join(f"line{i}" for i in range(10))
        result = ToolPreview.build("write_file", {"path": "/f", "content": content}, context="approval")
        self.assertIn("truncado", result)

    def test_write_file_preview_keeps_six_lines(self):
        content = "\n".join(f"line{i}" for i in range(6))
        result = ToolPreview.build("write_file", {"path": "/f", "content": content}, context="approval")
        self.assertNotIn("truncado", result)


class TestExecutionPreview(unittest.TestCase):
    """O contexto execution é a forma canônica de ``build_tool_line``."""

    def test_execution_read_file(self):
        result = ToolPreview.build("read_file", {"path": "README.md"})
        self.assertEqual("⌕ ler README.md", result)

    def test_execution_exec_command(self):
        result = ToolPreview.build("exec_command", {"cmd": "ls", "tty": True})
        self.assertEqual("⌕ ls", result)

    def test_execution_exec_command_runs_without_double_dollar(self):
        result = ToolPreview.build("exec_command", {"cmd": "python3 script.py"})
        self.assertEqual("$ python3 script.py", result)

    def test_execution_write_stdin(self):
        result = ToolPreview.build("write_stdin", {"session_id": "abc", "chars": "hi\n", "close_stdin": True})
        self.assertEqual("$ session=abc 'hi\\n' [close]", result)

    def test_execution_close_command_session(self):
        result = ToolPreview.build("close_command_session", {"session_id": "s1", "terminate": True})
        self.assertEqual("$ session=s1 [terminate]", result)

    def test_execution_masks_sensitive_fields(self):
        result = ToolPreview.build(
            "custom_tool",
            {"token": "1234567890", "headers": {"authorization": "Bearer secret-token"}},
        )
        self.assertNotIn("1234567890", result)
        self.assertNotIn("secret-token", result)
        self.assertIn("12****90", result)

    def test_execution_unknown_truncates_pathological_values(self):
        result = ToolPreview.build("weird_tool", {"payload": "x" * 1000})
        self.assertTrue(result.endswith("…"))
        self.assertEqual(len("$ ") + 400 + 1, len(result))

    def test_execution_unknown_without_arguments_shows_tool(self):
        self.assertEqual("$ weird_tool", ToolPreview.build("weird_tool", {}))


class TestHelpers(unittest.TestCase):
    def test_truncate_short_string(self):
        result = ToolPreview._truncate("hi", 10)
        self.assertEqual(result, "hi")

    def test_truncate_long_string(self):
        result = ToolPreview._truncate("a" * 600, 500)
        self.assertTrue(result.endswith("…"))
        self.assertEqual(len(result), 501)


if __name__ == "__main__":
    unittest.main()

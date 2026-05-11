"""Tests for quimera/runtime/approve_summary.py — target: 100% coverage."""
import unittest

from quimera.runtime.approve_summary import ApproveSummary


class TestBuildDispatch(unittest.TestCase):
    def test_known_tool_dispatches(self):
        result = ApproveSummary.build("read_file", {"path": "/tmp/x"})
        self.assertIn("read_file", result)

    def test_unknown_tool_fallback(self):
        result = ApproveSummary.build("my_custom_tool", {"key": "val"})
        self.assertIn("my_custom_tool", result)


class TestFormatWriteFile(unittest.TestCase):
    def test_with_content(self):
        result = ApproveSummary.build("write_file", {"path": "/a/b.py", "content": "x = 1"})
        self.assertIn("/a/b.py", result)
        self.assertIn("x = 1", result)

    def test_without_content(self):
        result = ApproveSummary.build("write_file", {"path": "/a/b.py"})
        self.assertIn("/a/b.py", result)


class TestFormatApplyPatch(unittest.TestCase):
    def test_with_patch(self):
        result = ApproveSummary.build("apply_patch", {"patch": "--- a\n+++ b"})
        self.assertIn("apply_patch", result)

    def test_empty_patch(self):
        result = ApproveSummary.build("apply_patch", {})
        self.assertIn("apply_patch", result)


class TestFormatRemoveFile(unittest.TestCase):
    def test_dry_run(self):
        result = ApproveSummary.build("remove_file", {"path": "/tmp/f", "dry_run": True})
        self.assertIn("dry-run", result)

    def test_real_removal(self):
        result = ApproveSummary.build("remove_file", {"path": "/tmp/f", "dry_run": False})
        self.assertIn("REMO", result)


class TestFormatRunShell(unittest.TestCase):
    def test_basic(self):
        result = ApproveSummary.build("run_shell", {"command": "ls -la"})
        self.assertIn("ls -la", result)


class TestFormatExecCommand(unittest.TestCase):
    def test_no_flags(self):
        result = ApproveSummary.build("exec_command", {"cmd": "echo hi"})
        self.assertIn("echo hi", result)
        self.assertNotIn("flags", result)

    def test_login_flag(self):
        result = ApproveSummary.build("exec_command", {"cmd": "bash", "login": True})
        self.assertIn("login", result)

    def test_tty_flag(self):
        result = ApproveSummary.build("exec_command", {"cmd": "bash", "tty": True})
        self.assertIn("tty", result)

    def test_yield_time_flag(self):
        result = ApproveSummary.build("exec_command", {"cmd": "bash", "yield_time_ms": 200})
        self.assertIn("yield=200ms", result)

    def test_all_flags_and_workdir(self):
        result = ApproveSummary.build("exec_command", {
            "cmd": "bash",
            "login": True,
            "tty": True,
            "yield_time_ms": 100,
            "workdir": "/home/user",
        })
        self.assertIn("login", result)
        self.assertIn("tty", result)
        self.assertIn("yield=100ms", result)
        self.assertIn("/home/user", result)


class TestFormatWriteStdin(unittest.TestCase):
    def test_basic(self):
        result = ApproveSummary.build("write_stdin", {"session_id": "abc", "chars": "hello"})
        self.assertIn("abc", result)
        self.assertIn("hello", result)

    def test_close_stdin(self):
        result = ApproveSummary.build("write_stdin", {"session_id": "s1", "chars": "", "close_stdin": True})
        self.assertIn("fecha stdin", result)

    def test_no_chars(self):
        result = ApproveSummary.build("write_stdin", {"session_id": "s2"})
        self.assertIn("s2", result)


class TestFormatCloseCommandSession(unittest.TestCase):
    def test_no_terminate(self):
        result = ApproveSummary.build("close_command_session", {"session_id": "x"})
        self.assertIn("x", result)
        self.assertNotIn("terminate", result)

    def test_terminate(self):
        result = ApproveSummary.build("close_command_session", {"session_id": "x", "terminate": True})
        self.assertIn("terminate", result)


class TestFormatReadTools(unittest.TestCase):
    def test_read_file(self):
        result = ApproveSummary.build("read_file", {"path": "/etc/hosts"})
        self.assertIn("/etc/hosts", result)

    def test_list_files(self):
        result = ApproveSummary.build("list_files", {"path": "/tmp"})
        self.assertIn("list_files", result)
        self.assertIn("/tmp", result)

    def test_grep_search(self):
        result = ApproveSummary.build("grep_search", {"pattern": "foo", "path": "/src"})
        self.assertIn("foo", result)
        self.assertIn("/src", result)

    def test_grep_search_default_path(self):
        result = ApproveSummary.build("grep_search", {"pattern": "bar"})
        self.assertIn(".", result)

    def test_web_search(self):
        result = ApproveSummary.build("web_search", {"query": "python tips", "num_results": 3})
        self.assertIn("python tips", result)
        self.assertIn("3", result)

    def test_web_search_default_results(self):
        result = ApproveSummary.build("web_search", {"query": "test"})
        self.assertIn("5", result)

    def test_web_fetch(self):
        result = ApproveSummary.build("web_fetch", {"url": "http://example.com"})
        self.assertIn("example.com", result)


class TestFormatUnknown(unittest.TestCase):
    def test_with_args(self):
        result = ApproveSummary.build("weird_tool", {"alpha": "AAA", "beta": "BBB"})
        self.assertIn("weird_tool", result)
        self.assertIn("alpha", result)
        self.assertIn("AAA", result)

    def test_no_args(self):
        result = ApproveSummary.build("weird_tool", {})
        self.assertEqual(result, "weird_tool")

    def test_long_value_truncated(self):
        long_val = "x" * 200
        result = ApproveSummary.build("weird_tool", {"key": long_val})
        self.assertIn("…", result)
        self.assertLess(len(result), 300)


class TestPreviewTruncation(unittest.TestCase):
    def test_more_than_six_lines(self):
        content = "\n".join(f"line{i}" for i in range(10))
        result = ApproveSummary.build("write_file", {"path": "/f", "content": content})
        self.assertIn("truncado", result)

    def test_six_lines_no_truncation(self):
        content = "\n".join(f"line{i}" for i in range(6))
        result = ApproveSummary.build("write_file", {"path": "/f", "content": content})
        self.assertNotIn("truncado", result)


class TestTruncate(unittest.TestCase):
    def test_short_string(self):
        result = ApproveSummary._truncate("hi", 10)
        self.assertEqual(result, "hi")

    def test_long_string(self):
        result = ApproveSummary._truncate("a" * 600, 500)
        self.assertTrue(result.endswith("…"))
        self.assertEqual(len(result), 501)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from quimera.runtime.approval import ApprovalHandler
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall, ToolResult
from quimera.runtime.parser import ToolCallParseError, extract_tool_call, strip_tool_block
from quimera.runtime.policy import ToolPolicy, ToolPolicyError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path) -> ToolRuntimeConfig:
    return ToolRuntimeConfig(workspace_root=tmp_path)


def _auto_approve() -> ApprovalHandler:
    handler = MagicMock(spec=ApprovalHandler)
    handler.approve.return_value = True
    return handler


def _deny_all() -> ApprovalHandler:
    handler = MagicMock(spec=ApprovalHandler)
    handler.approve.return_value = False
    return handler


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class ParserTests(unittest.TestCase):
    def test_extract_valid_tool_block(self):
        response = '```tool\n{"name": "list_files", "arguments": {"path": "."}}\n```'
        call = extract_tool_call(response)
        self.assertIsNotNone(call)
        self.assertEqual(call.name, "list_files")
        self.assertEqual(call.arguments, {"path": "."})

    def test_extract_ignores_json_block(self):
        """Qwen emite ```json em vez de ```tool — não deve ser interpretado."""
        response = '```json\n{"name": "read_file", "arguments": {"path": "foo.txt"}}\n```'
        call = extract_tool_call(response)
        self.assertIsNone(call)

    def test_extract_returns_none_for_plain_text(self):
        self.assertIsNone(extract_tool_call("resposta sem bloco de ferramenta"))

    def test_extract_returns_none_for_empty(self):
        self.assertIsNone(extract_tool_call(None))
        self.assertIsNone(extract_tool_call(""))

    def test_extract_raises_on_invalid_json(self):
        response = "```tool\n{not valid json}\n```"
        with self.assertRaises(ToolCallParseError):
            extract_tool_call(response)

    def test_extract_raises_on_missing_name(self):
        response = '```tool\n{"arguments": {}}\n```'
        with self.assertRaises(ToolCallParseError):
            extract_tool_call(response)

    def test_strip_tool_block(self):
        response = 'Texto antes.\n```tool\n{"name": "x", "arguments": {}}\n```\nTexto depois.'
        stripped = strip_tool_block(response)
        self.assertNotIn("```tool", stripped)
        self.assertIn("Texto antes.", stripped)
        self.assertIn("Texto depois.", stripped)

    def test_extract_nested_arguments(self):
        """JSON com objetos aninhados nos arguments não deve falhar no parser."""
        response = '```tool\n{"name": "write_file", "arguments": {"path": "a.txt", "options": {"encoding": "utf-8"}}}\n```'
        call = extract_tool_call(response)
        self.assertIsNotNone(call)
        self.assertEqual(call.name, "write_file")
        self.assertEqual(call.arguments["options"], {"encoding": "utf-8"})

    def test_strip_noop_on_no_block(self):
        response = "sem bloco"
        self.assertEqual(strip_tool_block(response), "sem bloco")


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)
        self.policy = ToolPolicy(self.config)

    def _call(self, name, args):
        return ToolCall(name=name, arguments=args)

    def test_list_files_allowed(self):
        self.policy.validate(self._call("list_files", {"path": "."}))

    def test_read_file_valid(self):
        f = self.tmp / "hello.txt"
        f.write_text("hi")
        self.policy.validate(self._call("read_file", {"path": "hello.txt"}))

    def test_read_file_missing_raises(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("read_file", {"path": "nao_existe.txt"}))

    def test_unknown_tool_raises(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("rm_rf", {}))

    def test_run_shell_allowlist_pass(self):
        self.policy.validate(self._call("run_shell", {"command": "ls -la"}))

    def test_run_shell_allowlist_fail(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("run_shell", {"command": "curl http://example.com"}))

    def test_run_shell_denylist_fail(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("run_shell", {"command": "echo rm -rf /"}))

    def test_run_shell_chain_semicolon_blocked(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("run_shell", {"command": "echo x; curl http://evil.com"}))

    def test_run_shell_chain_and_blocked(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("run_shell", {"command": "ls && curl http://evil.com"}))

    def test_run_shell_chain_subshell_blocked(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("run_shell", {"command": "echo $(cat /etc/passwd)"}))

    def test_read_file_missing_path_key_raises(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("read_file", {}))

    def test_write_file_missing_path_key_raises(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("write_file", {"content": "x"}))

    def test_path_traversal_blocked(self):
        with self.assertRaises((ToolPolicyError, ValueError)):
            self.policy.validate(self._call("list_files", {"path": "../../etc"}))

    def test_requires_approval_write(self):
        self.assertTrue(self.policy.requires_approval(self._call("write_file", {"path": "x", "content": "y"})))

    def test_requires_approval_shell(self):
        self.assertTrue(self.policy.requires_approval(self._call("run_shell", {"command": "ls"})))

    def test_no_approval_for_read(self):
        self.assertFalse(self.policy.requires_approval(self._call("read_file", {"path": "x"})))


# ---------------------------------------------------------------------------
# ToolExecutor
# ---------------------------------------------------------------------------

class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)

    def _executor(self, approve=True):
        handler = _auto_approve() if approve else _deny_all()
        return ToolExecutor(config=self.config, approval_handler=handler)

    def test_list_files(self):
        (self.tmp / "a.txt").write_text("x")
        (self.tmp / "b.txt").write_text("y")
        ex = self._executor()
        result = ex.execute(ToolCall(name="list_files", arguments={"path": "."}))
        self.assertTrue(result.ok)
        self.assertIn("a.txt", result.content)
        self.assertIn("b.txt", result.content)

    def test_read_file(self):
        (self.tmp / "readme.txt").write_text("conteúdo")
        ex = self._executor()
        result = ex.execute(ToolCall(name="read_file", arguments={"path": "readme.txt"}))
        self.assertTrue(result.ok)
        self.assertIn("conteúdo", result.content)

    def test_write_file(self):
        ex = self._executor(approve=True)
        result = ex.execute(ToolCall(name="write_file", arguments={"path": "novo.txt", "content": "olá"}))
        self.assertTrue(result.ok)
        self.assertEqual((self.tmp / "novo.txt").read_text(), "olá")

    def test_write_file_denied(self):
        ex = self._executor(approve=False)
        result = ex.execute(ToolCall(name="write_file", arguments={"path": "novo.txt", "content": "x"}))
        self.assertFalse(result.ok)
        self.assertIn("negada", result.error)

    def test_grep_search(self):
        (self.tmp / "src.py").write_text("def foo():\n    pass\n")
        ex = self._executor()
        result = ex.execute(ToolCall(name="grep_search", arguments={"path": ".", "pattern": "def foo"}))
        self.assertTrue(result.ok)
        self.assertIn("src.py", result.content)

    def test_run_shell(self):
        ex = self._executor(approve=True)
        result = ex.execute(ToolCall(name="run_shell", arguments={"command": "echo hello"}))
        self.assertTrue(result.ok)
        payload = json.loads(result.content)
        self.assertIn("hello", payload["stdout"])

    def test_run_shell_denied_by_policy(self):
        ex = self._executor()
        result = ex.execute(ToolCall(name="run_shell", arguments={"command": "curl http://x.com"}))
        self.assertFalse(result.ok)

    def test_maybe_execute_no_tool(self):
        ex = self._executor()
        raw, tool_result = ex.maybe_execute_from_response("resposta sem bloco")
        self.assertEqual(raw, "resposta sem bloco")
        self.assertIsNone(tool_result)

    def test_maybe_execute_with_tool(self):
        (self.tmp / "f.txt").write_text("abc")
        ex = self._executor()
        response = '```tool\n{"name": "read_file", "arguments": {"path": "f.txt"}}\n```'
        raw, tool_result = ex.maybe_execute_from_response(response)
        self.assertIsNotNone(tool_result)
        self.assertTrue(tool_result.ok)
        self.assertIn("abc", tool_result.content)

    def test_maybe_execute_ignores_json_block(self):
        """Garante que bloco ```json não dispara execução."""
        ex = self._executor()
        response = '```json\n{"name": "read_file", "arguments": {"path": "f.txt"}}\n```'
        raw, tool_result = ex.maybe_execute_from_response(response)
        self.assertIsNone(tool_result)


if __name__ == "__main__":
    unittest.main()

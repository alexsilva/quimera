import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import quimera.plugins.mock  # noqa: F401
from quimera.agents import AgentClient, _strip_spinner
from quimera.constants import Visibility
from quimera.context import ContextManager
from quimera.plugins import get
from quimera.plugins.base import AgentPlugin
from quimera.runtime.approval import ApprovalHandler, ConsoleApprovalHandler
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall, ToolResult
from quimera.runtime.parser import ToolCallParseError, _parse_json_object, extract_tool_call, strip_tool_block
from quimera.runtime.policy import ToolPolicy, ToolPolicyError
from quimera.runtime.registry import ToolRegistry
from quimera.runtime.task_executor import TaskExecutor, create_executor
from quimera.runtime.task_planning import (
    TASK_TYPE_CODE_EDIT,
    TASK_TYPE_GENERAL,
    TASK_TYPE_TEST_EXECUTION,
    choose_best_agent,
    classify_task_type,
    score_plugin_for_task,
)
from quimera.runtime.tools.files import FileTools, set_staging_root
from quimera.runtime.tools.shell import ShellTool
from quimera.runtime.tools.tasks import TaskTools


class _DummyStatus:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def update(self, _message):
        return None


class AgentsCoverageTests(unittest.TestCase):
    def setUp(self):
        self.renderer = MagicMock()
        self.renderer.running_status.return_value = _DummyStatus()

    def test_strip_spinner(self):
        self.assertEqual(_strip_spinner("⠋Executing"), "Executing")
        self.assertEqual(_strip_spinner("Normal text"), "Normal text")

    def test_run_handles_os_error(self):
        client = AgentClient(self.renderer)
        with patch("subprocess.Popen", side_effect=OSError("missing")):
            self.assertIsNone(client.run(["missing"], silent=True))
        self.renderer.show_error.assert_called_once()

    def test_run_silent_success_and_logging(self):
        client = AgentClient(self.renderer)
        proc = MagicMock()
        proc.stdout = ["ok\n"]
        proc.stderr = ["warn\n"]
        proc.returncode = 0
        with patch("subprocess.Popen", return_value=proc), patch("quimera.agents.client._logger") as logger:
            self.assertEqual(client.run(["cmd"], silent=True), "ok")
        logger.debug.assert_called_once()
        logger.warning.assert_called_once()

    def test_run_handles_input_failure(self):
        client = AgentClient(self.renderer)
        proc = MagicMock()
        proc.stdin.write.side_effect = RuntimeError("broken pipe")
        with patch("subprocess.Popen", return_value=proc):
            self.assertIsNone(client.run(["cmd"], input_text="hello", silent=True))
        proc.kill.assert_called_once()
        self.renderer.show_error.assert_called_once()

    def test_run_handles_timeout(self):
        # Non-rate-limited agents use wall-clock safety (timeout * 5); idle timeout no longer applies.
        client = AgentClient(self.renderer, timeout=0.1)
        proc = MagicMock()
        proc.stdout = iter(())
        proc.stderr = iter(())
        proc.stdin = MagicMock()
        proc.returncode = 0
        stdout_thread = MagicMock()
        stderr_thread = MagicMock()
        stdout_thread.is_alive.side_effect = [True, False]
        stderr_thread.is_alive.return_value = False
        with patch("subprocess.Popen", return_value=proc), patch(
                "threading.Thread", side_effect=[stdout_thread, stderr_thread]
        ), patch("time.sleep"), patch(
            "time.time", side_effect=[100.0, 101.0, 101.0]
        ):
            self.assertIsNone(client.run(["slow"], silent=False))
        proc.terminate.assert_called_once()
        self.renderer.show_error.assert_called()

    def test_run_handles_reader_exception(self):
        client = AgentClient(self.renderer)
        proc = MagicMock()

        def boom():
            raise RuntimeError("read error")
            yield "never"

        proc.stdout = boom()
        proc.stderr = iter(())
        proc.returncode = 0
        with patch("subprocess.Popen", return_value=proc):
            self.assertIsNone(client.run(["cmd"], silent=True))
        error_message = self.renderer.show_error.call_args[0][0]
        self.assertIn("falha ao comunicar com cmd: read error", error_message)

    def test_run_streaming_shows_output_and_truncates_stderr(self):
        client = AgentClient(self.renderer, visibility=Visibility.SUMMARY)
        proc = MagicMock()
        proc.stdout = iter(["out\n"])
        proc.stderr = iter(["err1\n", "err2\n"])
        proc.stdin = MagicMock()
        proc.returncode = 0
        with patch("subprocess.Popen", return_value=proc), patch("time.sleep"):
            self.assertEqual(client.run(["cmd"], silent=False, agent="codex"), "out")
        self.renderer.show_plain.assert_any_call("err1", agent="codex")

    def test_run_returns_none_for_non_zero_exit(self):
        client = AgentClient(self.renderer)
        proc = MagicMock()
        proc.stdout = []
        proc.stderr = [f"line {idx}\n" for idx in range(1, 7)]
        proc.returncode = 1
        with patch("subprocess.Popen", return_value=proc):
            self.assertIsNone(client.run(["cmd"], silent=True))
        self.assertGreaterEqual(self.renderer.show_error.call_count, 2)

    def test_call_uses_plugin_and_prompt_mode(self):
        client = AgentClient(self.renderer)
        plugin = MagicMock()
        plugin.cmd = ["mock-agent"]
        plugin.prompt_as_arg = False
        with patch("quimera.plugins.get", return_value=plugin), patch.object(client, "run", return_value="ok") as run:
            self.assertEqual(client.call("mock", "hello"), "ok")
        run.assert_called_once_with(["mock-agent"], input_text="hello", silent=False, agent="mock", show_status=True)

    def test_call_uses_prompt_as_arg_and_unknown_agent_errors(self):
        client = AgentClient(self.renderer)
        plugin = MagicMock()
        plugin.cmd = ["mock-agent"]
        plugin.prompt_as_arg = True
        with patch("quimera.plugins.get", return_value=plugin), patch.object(client, "run", return_value="ok") as run:
            self.assertEqual(client.call("mock", "hello"), "ok")
        run.assert_called_once_with(["mock-agent", "hello"], input_text=None, silent=False, agent="mock",
                                    show_status=True)
        with patch("quimera.plugins.get", return_value=None):
            self.assertIsNone(client.call("missing", "hello"))
        self.renderer.show_error.assert_called()

    def test_log_prompt_metrics_persists_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            metrics_file = Path(tmpdir) / "metrics.jsonl"
            client = AgentClient(self.renderer, metrics_file=str(metrics_file))
            client.log_prompt_metrics("claude", {"total_chars": 10, "history_chars": 4})
            self.assertTrue(metrics_file.exists())
            payload = json.loads(metrics_file.read_text(encoding="utf-8").strip())
            self.assertEqual(payload["agent"], "claude")
            self.assertEqual(payload["largest_block"], "history")
            self.renderer.show_system.assert_called_once()


class ContextCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.base = tmp / "base.md"
        self.session = tmp / "session.md"
        self.base.write_text("Base", encoding="utf-8")
        self.session.write_text(
            "## Resumo da última sessão\n\n_Gerado em 2026-01-01 10:00_\n\nResumo",
            encoding="utf-8",
        )
        self.renderer = MagicMock()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_load_methods(self):
        manager = ContextManager(self.base, self.session, self.renderer)
        self.assertEqual(manager.load_base(), "Base")
        self.assertIn("Resumo", manager.load_session())
        self.assertEqual(manager.load_session_summary(), "Resumo")
        self.assertIn("Base", manager.load())
        invalid = ContextManager(self.base, self.session.with_name("invalid.md"), self.renderer)
        invalid.session_context_file.write_text("sem marcador", encoding="utf-8")
        self.assertEqual(invalid.load_session_summary(), "")
        only_base = ContextManager(self.base, self.session.with_name("missing.md"), self.renderer)
        self.assertEqual(only_base.load(), "Base")
        only_session = ContextManager(self.base.with_name("missing-base.md"), self.session, self.renderer)
        self.assertIn("Resumo", only_session.load())

    def test_show_empty_and_non_empty(self):
        manager = ContextManager(self.base, self.session, self.renderer)
        manager.show()
        self.renderer.show_plain.assert_called_once()
        empty = ContextManager(self.base.with_name("missing.md"), self.session.with_name("other.md"), self.renderer)
        empty.show()
        self.renderer.show_system.assert_called_with("\n[contexto vazio]\n")

    def test_edit_uses_editor_fallback_and_errors(self):
        manager = ContextManager(self.base, self.session, self.renderer)
        with patch("os.environ.get", return_value="code --wait"), patch("subprocess.run") as run:
            manager.edit()
        run.assert_called_once_with(["code", "--wait", str(self.base)], check=True)
        with patch("os.environ.get", return_value=None), patch("shutil.which",
                                                               side_effect=lambda name: name == "nano"), patch(
                "subprocess.run"
        ) as fallback_run:
            manager.edit()
        fallback_run.assert_called_once_with(["nano", str(self.base)], check=True)
        with patch("os.environ.get", return_value=None), patch("shutil.which", return_value=None):
            manager.edit()
        self.renderer.show_error.assert_called()
        with patch("os.environ.get", return_value="missing"), patch("subprocess.run", side_effect=FileNotFoundError):
            manager.edit()
        with patch("os.environ.get", return_value="vim"), patch(
                "subprocess.run", side_effect=subprocess.CalledProcessError(1, "vim")
        ):
            manager.edit()
        self.assertGreaterEqual(self.renderer.show_error.call_count, 3)

    def test_update_with_summary(self):
        manager = ContextManager(self.base, self.session, self.renderer)
        manager.update_with_summary("Novo resumo")
        content = self.session.read_text(encoding="utf-8")
        self.assertIn("## Resumo da última sessão", content)
        self.assertIn("Novo resumo", content)


class ApprovalCoverageTests(unittest.TestCase):
    def test_approval_handler_base_raises(self):
        class ConcreteApproval(ApprovalHandler):
            def approve(self, *, tool_name: str, summary: str) -> bool:
                return super().approve(tool_name=tool_name, summary=summary)

        with self.assertRaises(NotImplementedError):
            ConcreteApproval().approve(tool_name="shell", summary="ls")

    def test_console_approval_handler_variants(self):
        handler = ConsoleApprovalHandler()
        with patch("builtins.print"), patch("builtins.input", return_value="y"):
            self.assertTrue(handler.approve(tool_name="shell", summary="ls"))
        with patch("builtins.print"), patch("builtins.input", return_value="sim"):
            self.assertTrue(handler.approve(tool_name="shell", summary="ls"))
        with patch("builtins.print"), patch("builtins.input", return_value=""):
            self.assertFalse(handler.approve(tool_name="shell", summary="ls"))


class RuntimeConfigAndModelsCoverageTests(unittest.TestCase):
    def test_runtime_config_resolves_defaults(self):
        root = Path("/tmp").resolve()
        self.assertEqual(ToolRuntimeConfig(workspace_root=root).allowed_read_roots, [root])
        other = Path("/").resolve()
        config = ToolRuntimeConfig(workspace_root=root, allowed_read_roots=[other])
        self.assertEqual(config.allowed_read_roots, [other])

    def test_models_payload_and_defaults(self):
        result = ToolResult(ok=True, tool_name="test", content="done", data={"a": 1})
        self.assertEqual(result.to_model_payload()["data"], {"a": 1})
        call = ToolCall(name="list_files", arguments={"path": "."})
        self.assertEqual(call.metadata, {})


class ParserCoverageTests(unittest.TestCase):
    def test_extract_and_strip_tool_blocks(self):
        response = 'abc <tool function="test">{"x":1}</tool> def'
        call = extract_tool_call(response)
        self.assertEqual(call.name, "test")
        self.assertEqual(call.arguments, {"x": 1})
        self.assertEqual(strip_tool_block(response), "abc  def")

    def test_parser_error_paths(self):
        self.assertIsNone(extract_tool_call(None))
        self.assertIsNone(extract_tool_call("plain text"))
        with self.assertRaises(ToolCallParseError):
            extract_tool_call('<tool function="read_file" arguments="{invalid json}" />')
        with self.assertRaises(ToolCallParseError):
            _parse_json_object("no braces")
        with patch("json.JSONDecoder.raw_decode", return_value=(["bad"], 0)):
            with self.assertRaises(ToolCallParseError):
                extract_tool_call('<tool function="x">{}</tool>')
        with self.assertRaises(ToolCallParseError):
            extract_tool_call('<tool path="missing-function" />')


class RegistryAndExecutorCoverageTests(unittest.TestCase):
    def setUp(self):
        self.config = ToolRuntimeConfig(workspace_root=Path("/tmp"))
        self.approval = MagicMock()

    def test_registry_register_get_and_names(self):
        registry = ToolRegistry()

        def handler(call):
            return ToolResult(ok=True, tool_name=call.name, content="done")

        registry.register("b", handler)
        registry.register("a", handler)
        self.assertIs(registry.get("a"), handler)
        self.assertEqual(registry.names(), ["a", "b"])
        with self.assertRaisesRegex(KeyError, "Ferramenta não registrada: missing"):
            registry.get("missing")

    def test_executor_denied_and_unexpected_error_and_parse_error(self):
        executor = ToolExecutor(self.config, self.approval)
        self.approval.approve.return_value = False
        denied = executor.execute(ToolCall(name="write_file", arguments={"path": "a", "content": "b"}))
        self.assertFalse(denied.ok)
        self.assertIn("Execução negada", denied.error)
        with patch.object(executor.registry, "get", return_value=MagicMock(side_effect=RuntimeError("boom"))):
            result = executor.execute(ToolCall(name="list_files", arguments={"path": "."}))
        self.assertFalse(result.ok)
        self.assertIn("Falha inesperada: boom", result.error)
        _, parse_result = executor.maybe_execute_from_response('<tool function="read_file" arguments="{invalid}" />')
        self.assertEqual(parse_result.tool_name, "parse")
        _, no_result = executor.maybe_execute_from_response("no tool")
        self.assertIsNone(no_result)


class PolicyCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "file.txt").write_text("x", encoding="utf-8")
        self.policy = ToolPolicy(ToolRuntimeConfig(workspace_root=self.root))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_policy_validation_paths_and_disabled_tools(self):
        self.policy.validate(ToolCall(name="list_tasks", arguments={}))
        self.policy.validate(ToolCall(name="list_jobs", arguments={}))
        self.policy.validate(ToolCall(name="get_job", arguments={}))
        self.policy.validate(ToolCall(name="list_files", arguments={"path": "."}))
        self.policy.validate(ToolCall(name="read_file", arguments={"path": "file.txt"}))
        self.policy.validate(ToolCall(name="write_file", arguments={"path": "out.txt", "content": "ok"}))
        with self.assertRaisesRegex(ToolPolicyError, "Sem política"):
            self.policy.validate(ToolCall(name="missing", arguments={}))
        with self.assertRaisesRegex(ToolPolicyError, "read_file requer 'path'"):
            self.policy.validate(ToolCall(name="read_file", arguments={}))
        with self.assertRaisesRegex(ToolPolicyError, "write_file requer 'content'"):
            self.policy.validate(ToolCall(name="write_file", arguments={"path": "out.txt"}))
        with self.assertRaisesRegex(ToolPolicyError, "padrão não vazio"):
            self.policy.validate(ToolCall(name="grep_search", arguments={"pattern": ""}))
        with self.assertRaisesRegex(ToolPolicyError, "foi desativada"):
            self.policy.validate(ToolCall(name="propose_task", arguments={}))
        with self.assertRaisesRegex(ToolPolicyError, "Path fora da workspace"):
            self.policy.validate(ToolCall(name="read_file", arguments={"path": "../../etc/passwd"}))
        for disabled in ("approve_task", "complete_task", "fail_task"):
            with self.assertRaises(ToolPolicyError):
                self.policy.validate(ToolCall(name=disabled, arguments={}))
        self.assertTrue(self.policy.requires_approval(ToolCall(name="write_file", arguments={})))
        self.assertFalse(self.policy.requires_approval(ToolCall(name="read_file", arguments={})))

    def test_policy_shell_validation(self):
        self.policy.validate(ToolCall(name="run_shell", arguments={"command": "echo hello"}))
        with self.assertRaisesRegex(ToolPolicyError, "comando não vazio"):
            self.policy.validate(ToolCall(name="run_shell", arguments={"command": "  "}))
        with self.assertRaisesRegex(ToolPolicyError, "operador de encadeamento proibido"):
            self.policy.validate(ToolCall(name="run_shell", arguments={"command": "ls && pwd"}))
        with self.assertRaisesRegex(ToolPolicyError, "denylist"):
            self.policy.validate(ToolCall(name="run_shell", arguments={"command": "rm -rf /"}))
        with self.assertRaisesRegex(ToolPolicyError, "Comando inválido"):
            self.policy.validate(ToolCall(name="run_shell", arguments={"command": 'echo "bad'}))
        with self.assertRaisesRegex(ToolPolicyError, "fora da allowlist"):
            self.policy.validate(ToolCall(name="run_shell", arguments={"command": "nc -l 80"}))


class TaskPlanningCoverageTests(unittest.TestCase):
    class Plugin(AgentPlugin):
        @property
        def name(self):
            return self._name

        @property
        def cmd(self):
            return ["mock"]

        def __init__(self, name, tier=1, preferred=None, avoid=None, code=False, long=False, tools=False,
                     capabilities=None):
            self._name = name
            self.base_tier = tier
            self.preferred_task_types = preferred or []
            self.avoid_task_types = avoid or []
            self.supports_code_editing = code
            self.supports_long_context = long
            self.supports_tools = tools
            self.capabilities = capabilities or []

    def test_classification_and_scoring(self):
        self.assertEqual(classify_task_type(""), TASK_TYPE_GENERAL)
        self.assertEqual(classify_task_type("texto aleatório"), TASK_TYPE_GENERAL)
        self.assertEqual(classify_task_type("corrija o bug"), "code_edit")
        self.assertEqual(classify_task_type("execute os testes"), "test_execution")
        plugin = self.Plugin("p1", tier=3, preferred=[TASK_TYPE_CODE_EDIT], code=True, tools=True)
        self.assertEqual(score_plugin_for_task(plugin, TASK_TYPE_CODE_EDIT), 11)
        avoided = self.Plugin("p2", avoid=[TASK_TYPE_TEST_EXECUTION])
        self.assertEqual(score_plugin_for_task(avoided, TASK_TYPE_TEST_EXECUTION), -5)

    def test_choose_best_agent_paths(self):
        self.assertIsNone(choose_best_agent("anything", []))
        p1 = self.Plugin("p1", tier=1)
        p2 = self.Plugin("p2", tier=3)
        self.assertEqual(choose_best_agent("anything", [p1, p2]), "p2")
        long_context = self.Plugin("long", preferred=[TASK_TYPE_GENERAL], long=True)
        self.assertGreater(score_plugin_for_task(long_context, "documentation"), 0)
        p3 = self.Plugin("p3", avoid=[TASK_TYPE_CODE_EDIT])
        self.assertEqual(choose_best_agent(TASK_TYPE_CODE_EDIT, [p3]), "p3")
        p4 = self.Plugin("p4", avoid=[TASK_TYPE_CODE_EDIT])
        p5 = self.Plugin("p5")
        self.assertEqual(choose_best_agent(TASK_TYPE_CODE_EDIT, [p4, p5]), "p5")
        all_avoid = [self.Plugin("p6", avoid=[TASK_TYPE_CODE_EDIT]), self.Plugin("p7", avoid=[TASK_TYPE_CODE_EDIT])]
        self.assertEqual(choose_best_agent(TASK_TYPE_CODE_EDIT, all_avoid), "p6")


class TaskExecutorCoverageTests(unittest.TestCase):
    def test_constructor_requires_db_path(self):
        with self.assertRaisesRegex(ValueError, "db_path is required"):
            TaskExecutor("agent", None)

    def test_start_stop_and_create_executor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.db"
            executor = TaskExecutor("agent", db_path, poll_interval=0.01)
            with patch.object(executor, "_poll_loop") as poll_loop:
                executor.start()
                executor.start()
                executor.stop()
            poll_loop.assert_called_once()
            created = create_executor("agent", lambda task: True, db_path)
            self.assertEqual(created.agent_name, "agent")
            self.assertIsNotNone(created._handler)

    def test_process_pending_and_poll_loop_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.db"
            executor = TaskExecutor("agent", db_path, poll_interval=0.01)
            handler = MagicMock(return_value=True)
            review_handler = MagicMock(return_value=True)
            executor.set_handler(handler)
            executor.set_review_handler(review_handler)
            with patch("quimera.runtime.task_executor.claim_task", return_value=None):
                self.assertIsNone(executor.process_pending())
            with patch("quimera.runtime.task_executor.claim_task", return_value=1), patch(
                    "quimera.runtime.task_executor.list_tasks", return_value=[{"id": 1}]
            ):
                self.assertEqual(executor.process_pending(), 1)
            handler.assert_called_with({"id": 1})

            claim_task_effects = [1, None, Exception("stop")]

            def claim_task_side_effect(*_args, **_kwargs):
                value = claim_task_effects.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

            wait_calls = []

            def fake_wait(_seconds):
                wait_calls.append(_seconds)
                # Stop only after both task and review have been processed (2 waits)
                if len(wait_calls) >= 2:
                    executor._running = False
                    return True
                return False

            executor._running = True
            with patch("quimera.runtime.task_executor.claim_task", side_effect=claim_task_side_effect), patch(
                    "quimera.runtime.task_executor.claim_review_task", return_value=2
            ), patch("quimera.runtime.task_executor.list_tasks", side_effect=[[{"id": 1}], [{"id": 2}]]), patch.object(
                executor, "_wait_or_stop", side_effect=fake_wait
            ):
                executor._poll_loop()
            handler.assert_called_with({"id": 1})
            review_handler.assert_called_with({"id": 2})
            self.assertTrue(wait_calls)


class FileToolsCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name) / "workspace"
        self.root.mkdir()
        self.config = ToolRuntimeConfig(workspace_root=self.root, max_file_read_chars=4, max_search_results=2)
        self.tools = FileTools(self.config)

    def tearDown(self):
        set_staging_root(None)
        self.tmpdir.cleanup()

    def test_resolve_and_list_files_with_staging_overlay(self):
        (self.root / "local.txt").write_text("a", encoding="utf-8")
        (self.root / "subdir").mkdir()
        (self.root / "subdir" / "workspace.txt").write_text("workspace", encoding="utf-8")
        staging = self.root.parent / "staging"
        staging.mkdir()
        (staging / "staged.txt").write_text("b", encoding="utf-8")
        (staging / "subdir").mkdir()
        (staging / "subdir" / "nested.txt").write_text("c", encoding="utf-8")
        set_staging_root(staging)
        result = self.tools.list_files(ToolCall(name="list_files", arguments={"path": "."}))
        self.assertIn("staged.txt", result.content)
        nested = self.tools.list_files(ToolCall(name="list_files", arguments={"path": "subdir"}))
        self.assertIn("nested.txt", nested.content)
        with self.assertRaisesRegex(ValueError, "Path fora da workspace"):
            self.tools._resolve("../../etc/passwd")

    def test_read_write_and_grep(self):
        (self.root / "a.txt").write_text("abcdef", encoding="utf-8")
        read_result = self.tools.read_file(ToolCall(name="read_file", arguments={"path": "a.txt"}))
        self.assertEqual(read_result.content, "abcd")
        self.assertTrue(read_result.truncated)
        created = self.tools.write_file(ToolCall(name="write_file", arguments={"path": "file.txt", "content": "hi"}))
        self.assertTrue(created.ok)
        duplicate = self.tools.write_file(
            ToolCall(name="write_file", arguments={"path": "file.txt", "content": "x", "mode": "create"})
        )
        self.assertFalse(duplicate.ok)
        appended = self.tools.write_file(
            ToolCall(name="write_file", arguments={"path": "file.txt", "content": "!", "mode": "append"})
        )
        self.assertTrue(appended.ok)
        staging = self.root.parent / "staging"
        staging.mkdir()
        (staging / "other.txt").write_text("needle", encoding="utf-8")
        set_staging_root(staging)
        (self.root / "main.txt").write_text("needle", encoding="utf-8")
        staged_read = self.tools.read_file(ToolCall(name="read_file", arguments={"path": "other.txt"}))
        self.assertEqual(staged_read.content, "need")
        grep = self.tools.grep_search(ToolCall(name="grep_search", arguments={"pattern": "needle"}))
        self.assertTrue(grep.truncated)
        self.assertIn("main.txt", grep.content)
        empty = self.tools.grep_search(ToolCall(name="grep_search", arguments={"path": "missing", "pattern": "needle"}))
        self.assertEqual(empty.content, "")
        with patch("pathlib.Path.is_file", side_effect=lambda self_path: self_path.name != "skip.txt"), patch(
                "pathlib.Path.rglob",
                return_value=[Path("/tmp/skip.txt"), Path("/tmp/visible.txt")],
        ), patch("pathlib.Path.read_text", return_value="needle"):
            pass
        with patch("pathlib.Path.read_text", side_effect=RuntimeError("boom")):
            safe = self.tools.grep_search(ToolCall(name="grep_search", arguments={"pattern": "needle"}))
        self.assertTrue(safe.ok)


class ShellToolCoverageTests(unittest.TestCase):
    def test_shell_tool_success_and_warning(self):
        config = ToolRuntimeConfig(workspace_root=Path("/tmp"))
        tool = ShellTool(config)
        proc = MagicMock(stdout="hello\n", stderr="", returncode=0)
        with patch("subprocess.run", return_value=proc):
            result = tool.run_shell(ToolCall(name="run_shell", arguments={"command": "echo hello"}))
        self.assertTrue(result.ok)
        self.assertIn("hello", result.content)
        with patch("quimera.runtime.tools.files.get_staging_root", return_value=Path("/tmp/staging")), patch(
                "subprocess.run", return_value=proc
        ):
            with self.assertWarnsRegex(UserWarning, "Shell writes bypass staging isolation"):
                tool.run_shell(ToolCall(name="run_shell", arguments={"command": "ls"}))


class TaskToolsCoverageTests(unittest.TestCase):
    def setUp(self):
        self.config = ToolRuntimeConfig(workspace_root=Path("/tmp"), db_path=Path("/tmp/tasks.db"))
        self.tools = TaskTools(self.config)

    def test_resolve_job_id_and_duplicates(self):
        with patch.dict(os.environ, {"QUIMERA_CURRENT_JOB_ID": "123"}):
            self.assertEqual(self.tools._resolve_job_id(None), 123)
        with patch.dict(os.environ, {"QUIMERA_CURRENT_JOB_ID": "bad"}):
            self.assertIsNone(self.tools._resolve_job_id(None))
        with patch.dict(os.environ, {}, clear=True), patch(
                "quimera.runtime.tools.tasks._list_jobs", side_effect=[[{"id": 10}], []]
        ):
            self.assertEqual(self.tools._resolve_job_id(None, allow_recent_fallback=True), 10)
        with patch.dict(os.environ, {}, clear=True), patch(
                "quimera.runtime.tools.tasks._list_jobs", side_effect=[[], [{"id": 20}]]
        ):
            self.assertEqual(self.tools._resolve_job_id(None, allow_recent_fallback=True), 20)
        with patch.dict(os.environ, {}, clear=True), patch(
                "quimera.runtime.tools.tasks._list_jobs", side_effect=RuntimeError("db down")
        ):
            self.assertIsNone(self.tools._resolve_job_id(None, allow_recent_fallback=True))
        with patch(
                "quimera.runtime.tools.tasks._list_tasks",
                side_effect=[[{"description": " TEST task "}], [], [], []],
        ):
            self.assertIsNotNone(self.tools._find_duplicate_task(1, "test task"))
            self.assertIsNone(self.tools._find_duplicate_task(1, ""))
            self.assertIsNone(self.tools._find_duplicate_task(1, "unique task"))

    def test_list_jobs_tasks_and_get_job_paths(self):
        with patch("quimera.runtime.tools.tasks._list_tasks", return_value=[{"id": 1}]):
            tasks = self.tools.list_tasks(ToolCall(name="list_tasks", arguments={"status": "approved"}))
        self.assertEqual(json.loads(tasks.content), [{"id": 1}])
        with patch("quimera.runtime.tools.tasks._list_tasks", side_effect=RuntimeError("boom")):
            failed_tasks = self.tools.list_tasks(ToolCall(name="list_tasks", arguments={}))
        self.assertFalse(failed_tasks.ok)
        with patch("quimera.runtime.tools.tasks._list_jobs", return_value=[{"id": 2}]):
            jobs = self.tools.list_jobs(ToolCall(name="list_jobs", arguments={"status": "planning"}))
        self.assertEqual(json.loads(jobs.content), [{"id": 2}])
        with patch("quimera.runtime.tools.tasks._list_jobs", side_effect=RuntimeError("boom")):
            failed_jobs = self.tools.list_jobs(ToolCall(name="list_jobs", arguments={}))
        self.assertFalse(failed_jobs.ok)
        with patch.object(self.tools, "_resolve_job_id", return_value=None):
            missing_job = self.tools.get_job(ToolCall(name="get_job", arguments={}))
        self.assertFalse(missing_job.ok)
        with patch.object(self.tools, "_resolve_job_id", return_value=1), patch(
                "quimera.runtime.tools.tasks._get_job", return_value=None
        ):
            null_job = self.tools.get_job(ToolCall(name="get_job", arguments={}))
        self.assertEqual(null_job.content, "null")
        with patch.object(self.tools, "_resolve_job_id", return_value=1), patch(
                "quimera.runtime.tools.tasks._get_job", side_effect=RuntimeError("boom")
        ):
            failed_job = self.tools.get_job(ToolCall(name="get_job", arguments={}))
        self.assertFalse(failed_job.ok)


class MockPluginCoverageTests(unittest.TestCase):
    def test_mock_plugin_registered(self):
        plugin = get("mock")
        self.assertIsNotNone(plugin)
        self.assertEqual(plugin.name, "mock")
        self.assertEqual(plugin.prefix, "/mock")
        self.assertIn("echo", plugin.cmd)
        self.assertTrue(plugin.prompt_as_arg)

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import quimera.plugins as plugins
from quimera.runtime.approval import ApprovalHandler
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicy, ToolPolicyError
from quimera.runtime.task_executor import TaskExecutor
from quimera.runtime.task_planning import choose_best_agent, classify_task_type
from quimera.runtime.tasks import add_job, get_conn, init_db, list_tasks, propose_task


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

    def test_get_job_without_job_id_is_allowed(self):
        self.policy.validate(self._call("get_job", {}))

    def test_propose_task_is_blocked_in_chat(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("propose_task", {"description": "abrir tarefa"}))

    def test_approve_task_is_blocked_in_chat(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("approve_task", {"task_id": 1}))

    def test_complete_task_is_blocked_in_chat(self):
        with self.assertRaises(ToolPolicyError):
            self.policy.validate(self._call("complete_task", {"task_id": 1}))

    def test_classify_task_type_examples(self):
        self.assertEqual(classify_task_type("execute os testes"), "test_execution")
        self.assertEqual(classify_task_type("revise o arquivo quimera/app.py"), "code_review")
        self.assertEqual(classify_task_type("corrija o parser"), "code_edit")
        self.assertEqual(classify_task_type("investigue por que o handoff falha"), "bug_investigation")

    def test_choose_best_agent_uses_plugin_preferences(self):
        selected = choose_best_agent("test_execution",
                                     [plugins.get("claude"), plugins.get("codex"), plugins.get("ollama-granite4")])
        self.assertEqual(selected, "codex")

    def test_choose_best_agent_prefers_tooling_for_test_execution(self):
        selected = choose_best_agent("test_execution",
                                     [plugins.get("claude"), plugins.get("ollama-granite4"), plugins.get("opencode")])
        self.assertEqual(selected, "claude")

    def test_choose_best_agent_penalizes_low_reliability_tool_users_for_bug_investigation(self):
        selected = choose_best_agent("bug_investigation", [plugins.get("ollama-granite4"), plugins.get("gemini")])
        self.assertEqual(selected, "gemini")

    def test_choose_best_agent_does_not_route_tasks_to_qwen_without_explicit_execution_support(self):
        selected = choose_best_agent("code_review", [plugins.get("ollama-granite4"), plugins.get("claude")])
        self.assertEqual(selected, "claude")

    def test_choose_best_agent_assigns_ollama_when_it_is_the_only_available_agent(self):
        selected = choose_best_agent("code_review", [plugins.get("ollama-granite4")])
        self.assertEqual(selected, "ollama-granite4")

    def test_choose_best_agent_does_not_route_general_to_ollama_on_tie_order(self):
        selected = choose_best_agent("general",
                                     [plugins.get("ollama-granite4"), plugins.get("claude"), plugins.get("codex")])
        self.assertEqual(selected, "claude")

    def test_choose_best_agent_does_not_route_general_to_opencode_on_tie_order(self):
        selected = choose_best_agent("general",
                                     [plugins.get("opencode"), plugins.get("claude"), plugins.get("codex")])
        self.assertEqual(selected, "claude")

    def test_choose_best_agent_prefers_higher_tier_for_general_tasks(self):
        # opencode-omni-pro has general preferred (5) + tier 1 (0 boost) = 5
        # codex has general preferred (5) + tier 2 (2 boost) = 7
        # claude has general preferred (5) + tier 3 (4 boost) = 9
        selected = choose_best_agent("general",
                                     [plugins.get("opencode"), plugins.get("claude"), plugins.get("codex")])
        self.assertEqual(selected, "claude")

    def test_all_code_editing_agents_are_review_eligible(self):
        for plugin in plugins.all_plugins():
            if plugin.supports_code_editing:
                self.assertIn("code_review", plugin.preferred_task_types, plugin.name)


# ---------------------------------------------------------------------------
# ToolExecutor
# ---------------------------------------------------------------------------

class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "tasks.db"
        init_db(str(self.db_path))
        self.config = ToolRuntimeConfig(
            workspace_root=self.tmp,
            db_path=str(self.db_path),
            require_approval_for_mutations=True,
        )

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

    def test_executor_has_no_text_response_tool_parser(self):
        ex = self._executor()
        names = [name for name in dir(ex) if "response" in name and name.startswith("maybe")]
        self.assertEqual(names, [])

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
        self.assertIn("hello", result.content)
        self.assertEqual(result.data["stdout"], "hello\n")

    def test_run_shell_denied_by_policy(self):
        ex = self._executor()
        result = ex.execute(ToolCall(name="run_shell", arguments={"command": "curl http://x.com"}))
        self.assertFalse(result.ok)

    def test_list_tasks_accepts_top_level_filters(self):
        job_id = add_job("Job test", db_path=str(self.db_path))
        task_id = propose_task(job_id, "Task A", db_path=str(self.db_path))
        ex = self._executor()

        result = ex.execute(ToolCall(name="list_tasks", arguments={"job_id": job_id, "status": "proposed"}))

        self.assertTrue(result.ok)
        payload = json.loads(result.content)
        self.assertEqual([task["id"] for task in payload], [task_id])

    def test_propose_task_via_executor_is_blocked(self):
        job_id = add_job("Job test", db_path=str(self.db_path))
        ex = self._executor()
        result = ex.execute(
            ToolCall(
                name="propose_task",
                arguments={"job_id": job_id, "description": "Validar schema"},
            )
        )

        self.assertFalse(result.ok)
        self.assertIn("desativada", result.error)

    def test_propose_task_fails_for_unknown_job_id(self):
        ex = self._executor()

        result = ex.execute(
            ToolCall(
                name="propose_task",
                arguments={"job_id": 999, "description": "Task órfã"},
            )
        )

        self.assertFalse(result.ok)
        self.assertIn("desativada", result.error)

    def test_propose_task_denied_without_approval(self):
        job_id = add_job("Job test", db_path=str(self.db_path))
        ex = self._executor(approve=False)

        result = ex.execute(
            ToolCall(
                name="propose_task",
                arguments={"job_id": job_id, "description": "Abrir tarefa aprovada pelo humano"},
            )
        )

        self.assertFalse(result.ok)
        self.assertIn("desativada", result.error)

    def test_approve_task_via_executor_is_blocked(self):
        job_id = add_job("Job test", db_path=str(self.db_path))
        task_id = propose_task(job_id, "Task A", db_path=str(self.db_path))
        ex = self._executor()

        result = ex.execute(ToolCall(name="approve_task", arguments={"task_id": task_id, "approved_by": "alex"}))

        self.assertFalse(result.ok)
        self.assertIn("desativada", result.error)
        tasks = list_tasks({"id": task_id}, db_path=str(self.db_path))
        self.assertEqual(tasks[0]["status"], "proposed")

    def test_get_job_uses_current_job_env_fallback(self):
        job_id = add_job("Job env", db_path=str(self.db_path))
        ex = self._executor()
        old_env = os.environ.get("QUIMERA_CURRENT_JOB_ID")
        os.environ["QUIMERA_CURRENT_JOB_ID"] = str(job_id)
        try:
            result = ex.execute(ToolCall(name="get_job", arguments={}))
        finally:
            if old_env is None:
                os.environ.pop("QUIMERA_CURRENT_JOB_ID", None)
            else:
                os.environ["QUIMERA_CURRENT_JOB_ID"] = old_env

        self.assertTrue(result.ok)
        payload = json.loads(result.content)
        self.assertEqual(payload["id"], job_id)

    def test_list_jobs_accepts_top_level_filters(self):
        add_job("Planejamento", created_by="alex", db_path=str(self.db_path))
        add_job("Execução", created_by="bia", db_path=str(self.db_path))
        ex = self._executor()

        result = ex.execute(ToolCall(name="list_jobs", arguments={"created_by": "alex"}))

        self.assertTrue(result.ok)
        payload = json.loads(result.content)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["description"], "Planejamento")


class TasksDbPathGuardTests(unittest.TestCase):
    def test_get_conn_raises_without_db_path(self):
        with self.assertRaises(ValueError):
            get_conn(None)

    def test_task_executor_raises_without_repository(self):
        with self.assertRaises(ValueError):
            TaskExecutor(agent_name="codex", db_path=None)


if __name__ == "__main__":
    unittest.main()

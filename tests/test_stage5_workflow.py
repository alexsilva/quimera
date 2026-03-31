import os
import tempfile
import threading
import unittest

from quimera.runtime.tasks import (
    init_db, add_job, propose_task, approve_task, reject_task,
    list_tasks, list_jobs, claim_task, update_task, complete_task,
    fail_task, drop_db, get_conn,
)


class TestStage5Workflow(unittest.TestCase):
    """Ciclo completo: job → propose → approve → claim → complete."""

    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".db")
        init_db(self.tmp)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def test_full_lifecycle(self):
        job_id = add_job("Refactor auth module", created_by="alex", db_path=self.tmp)
        self.assertIsInstance(job_id, int)

        t1 = propose_task(job_id, "Add JWT validation", created_by="agent-1", db_path=self.tmp)
        t2 = propose_task(job_id, "Update login endpoint", created_by="agent-1", db_path=self.tmp)

        self.assertTrue(approve_task(t1, approved_by="alex", db_path=self.tmp))
        self.assertTrue(approve_task(t2, approved_by="alex", db_path=self.tmp))

        claimed = claim_task("agent-2", db_path=self.tmp)
        self.assertEqual(claimed, t1)

        tasks = list_tasks({"status": "in_progress"}, db_path=self.tmp)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["assigned_to"], "agent-2")

        complete_task(t1, result="JWT validation implemented", db_path=self.tmp)

        tasks_completed = list_tasks({"status": "completed"}, db_path=self.tmp)
        self.assertEqual(len(tasks_completed), 1)
        self.assertEqual(tasks_completed[0]["result"], "JWT validation implemented")

    def test_approve_validates_state(self):
        job_id = add_job("Test job", db_path=self.tmp)
        tid = propose_task(job_id, "Some task", db_path=self.tmp)

        self.assertTrue(approve_task(tid, approved_by="alex", db_path=self.tmp))
        self.assertFalse(approve_task(tid, approved_by="alex", db_path=self.tmp))

    def test_reject_validates_state(self):
        job_id = add_job("Test job", db_path=self.tmp)
        tid = propose_task(job_id, "Some task", db_path=self.tmp)

        self.assertTrue(reject_task(tid, rejected_by="alex", reason="Not needed", db_path=self.tmp))
        self.assertFalse(reject_task(tid, rejected_by="alex", db_path=self.tmp))

    def test_cannot_approve_rejected(self):
        job_id = add_job("Test job", db_path=self.tmp)
        tid = propose_task(job_id, "Some task", db_path=self.tmp)
        reject_task(tid, rejected_by="alex", db_path=self.tmp)
        self.assertFalse(approve_task(tid, approved_by="alex", db_path=self.tmp))

    def test_claim_returns_none_when_no_approved_tasks(self):
        job_id = add_job("Test job", db_path=self.tmp)
        propose_task(job_id, "Task not approved", db_path=self.tmp)
        self.assertIsNone(claim_task("agent-1", db_path=self.tmp))

    def test_fail_task(self):
        job_id = add_job("Test job", db_path=self.tmp)
        tid = propose_task(job_id, "Some task", db_path=self.tmp)
        approve_task(tid, approved_by="alex", db_path=self.tmp)
        claim_task("agent-1", db_path=self.tmp)

        fail_task(tid, reason="Dependency missing", db_path=self.tmp)
        tasks = list_tasks({"status": "failed"}, db_path=self.tmp)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["result"], "Dependency missing")

    def test_list_jobs_filter(self):
        add_job("Job A", created_by="alex", db_path=self.tmp)
        add_job("Job B", created_by="bob", db_path=self.tmp)

        all_jobs = list_jobs(db_path=self.tmp)
        self.assertEqual(len(all_jobs), 2)

        alex_jobs = list_jobs({"created_by": "alex"}, db_path=self.tmp)
        self.assertEqual(len(alex_jobs), 1)
        self.assertEqual(alex_jobs[0]["description"], "Job A")

    def test_list_tasks_filter_by_job(self):
        j1 = add_job("Job 1", db_path=self.tmp)
        j2 = add_job("Job 2", db_path=self.tmp)
        propose_task(j1, "Task for job 1", db_path=self.tmp)
        propose_task(j2, "Task for job 2", db_path=self.tmp)

        tasks_j1 = list_tasks({"job_id": j1}, db_path=self.tmp)
        self.assertEqual(len(tasks_j1), 1)

    def test_update_task(self):
        job_id = add_job("Test job", db_path=self.tmp)
        tid = propose_task(job_id, "Some task", db_path=self.tmp)
        approve_task(tid, approved_by="alex", db_path=self.tmp)
        claim_task("agent-1", db_path=self.tmp)

        update_task(tid, status="completed", result="done", notes="all good", db_path=self.tmp)
        tasks = list_tasks({"status": "completed"}, db_path=self.tmp)
        self.assertEqual(tasks[0]["notes"], "all good")


class TestStage5Concurrency(unittest.TestCase):
    """Testes de concorrência para claim atômico."""

    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".db")
        init_db(self.tmp)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def test_claim_atomic_no_double_claim(self):
        """Dois agentes não podem claim a mesma task."""
        job_id = add_job("Concurrency test", db_path=self.tmp)
        tid = propose_task(job_id, "Atomic task", db_path=self.tmp)
        approve_task(tid, approved_by="alex", db_path=self.tmp)

        claimed_by = []
        lock = threading.Lock()

        def try_claim(agent_name):
            result = claim_task(agent_name, db_path=self.tmp)
            with lock:
                if result is not None:
                    claimed_by.append((agent_name, result))

        threads = [
            threading.Thread(target=try_claim, args=(f"agent-{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(claimed_by), 1)
        self.assertEqual(claimed_by[0][1], tid)


if __name__ == "__main__":
    unittest.main()

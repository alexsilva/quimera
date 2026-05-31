import unittest
from quimera.runtime.tools.todo import TodoRegistry


class TestTodoRegistry(unittest.TestCase):
    def setUp(self):
        self.job_id = 9999
        # Clean up any existing state for this job_id
        TodoRegistry.cleanup(self.job_id)

    def tearDown(self):
        TodoRegistry.cleanup(self.job_id)

    def test_unique_in_progress_invariant(self):
        """ chamar `TodoRegistry.write` com 2 itens `in_progress` — o anterior deve mover para `pending` ao setar o segundo. """
        # First item with in_progress
        TodoRegistry.write(self.job_id, "agent1", [{"content": "first", "status": "in_progress"}])
        active = TodoRegistry.get_active(self.job_id)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].content, "first")
        self.assertEqual(active[0].status, "in_progress")

        # Second item with in_progress should move the first to pending
        TodoRegistry.write(self.job_id, "agent2", [{"content": "second", "status": "in_progress"}])
        active = TodoRegistry.get_active(self.job_id)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].content, "second")
        self.assertEqual(active[0].status, "in_progress")

        # Check that the first item is now pending
        all_todos = TodoRegistry.list(self.job_id)
        self.assertEqual(len(all_todos), 2)
        # Find the first todo by content
        first_todo = next(t for t in all_todos if t.content == "first")
        self.assertEqual(first_todo.status, "pending")

    def test_get_active_filters_correctly(self):
        """ `get_active` filtra corretamente: apenas itens com `status == "in_progress"` aparecem em `get_active` e em `get_active_as_dicts`. """
        # Create a mix of statuses
        TodoRegistry.write(self.job_id, "agent1", [
            {"content": "pending1", "status": "pending"},
            {"content": "in_progress1", "status": "in_progress"},
            {"content": "completed1", "status": "completed"},
        ])
        active_items = TodoRegistry.get_active(self.job_id)
        active_dicts = TodoRegistry.get_active_as_dicts(self.job_id)

        # Only one item should be active
        self.assertEqual(len(active_items), 1)
        self.assertEqual(active_items[0].content, "in_progress1")
        self.assertEqual(active_items[0].status, "in_progress")

        self.assertEqual(len(active_dicts), 1)
        self.assertEqual(active_dicts[0]["content"], "in_progress1")
        self.assertEqual(active_dicts[0]["status"], "in_progress")

        # Ensure the dicts are deep copies (not mutable references)
        # Modify the dict and check that the original item is unchanged
        active_dicts[0]["content"] = "modified"
        # The original item in the registry should still be "in_progress1"
        active_items_after = TodoRegistry.get_active(self.job_id)
        self.assertEqual(active_items_after[0].content, "in_progress1")

    def test_batch_update_and_create(self):
        """ Batch com atualização + criação: num mesmo `write`, atualizar item existente (por `id`) e criar item novo — ambos devem aparecer na lista. """
        # Create an initial item
        initial = TodoRegistry.write(self.job_id, "agent1", [{"content": "initial", "status": "pending"}])
        initial_id = initial[0].id

        # Now update the initial item and create a new one in the same write call
        results = TodoRegistry.write(self.job_id, "agent2", [
            {"id": initial_id, "content": "updated", "status": "in_progress"},
            {"content": "new", "status": "pending"}
        ])

        # We should have two results: the updated item and the new item
        self.assertEqual(len(results), 2)
        # Check that the updated item has the new content and status
        updated = next(r for r in results if r.id == initial_id)
        self.assertEqual(updated.content, "updated")
        self.assertEqual(updated.status, "in_progress")
        # Check that the new item exists
        new_item = next(r for r in results if r.content == "new")
        self.assertEqual(new_item.status, "pending")

        # Verify via list
        all_todos = TodoRegistry.list(self.job_id)
        self.assertEqual(len(all_todos), 2)
        contents = {t.content for t in all_todos}
        self.assertIn("updated", contents)
        self.assertIn("new", contents)

    def test_batch_in_progress_only_last_remains(self):
        """ dentre múltiplos itens `in_progress` no mesmo `write`, apenas o último permanece ativo. """
        TodoRegistry.write(self.job_id, "agent1", [
            {"content": "first", "status": "in_progress"},
            {"content": "second", "status": "in_progress"},
            {"content": "third", "status": "in_progress"},
        ])
        active = TodoRegistry.get_active(self.job_id)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].content, "third")

        all_todos = TodoRegistry.list(self.job_id)
        first = next(t for t in all_todos if t.content == "first")
        second = next(t for t in all_todos if t.content == "second")
        third = next(t for t in all_todos if t.content == "third")
        self.assertEqual(first.status, "pending")
        self.assertEqual(second.status, "pending")
        self.assertEqual(third.status, "in_progress")

    def test_cleanup_removes_job(self):
        """ `cleanup` remove o job: após `cleanup(job_id)`, `TodoRegistry.list(job_id)` deve retornar `[]`. """
        # Add some items
        TodoRegistry.write(self.job_id, "agent1", [{"content": "test1", "status": "pending"}])
        TodoRegistry.write(self.job_id, "agent2", [{"content": "test2", "status": "in_progress"}])

        # Verify they exist
        self.assertEqual(len(TodoRegistry.list(self.job_id)), 2)

        # Cleanup
        TodoRegistry.cleanup(self.job_id)

        # Verify the job is cleaned
        self.assertEqual(len(TodoRegistry.list(self.job_id)), 0)
        self.assertEqual(len(TodoRegistry.get_active(self.job_id)), 0)
        self.assertEqual(len(TodoRegistry.get_active_as_dicts(self.job_id)), 0)

    def test_get_active_as_dicts_returns_correct_fields(self):
        """ `get_active_as_dicts` dentro do lock: verificar que retorna dicts com os campos corretos (não referências mutáveis). """
        TodoRegistry.write(self.job_id, "agent1", [
            {"content": "task1", "status": "in_progress", "priority": "high"},
            {"content": "task2", "status": "pending", "priority": "low"},
        ])

        active_dicts = TodoRegistry.get_active_as_dicts(self.job_id)
        self.assertEqual(len(active_dicts), 1)
        d = active_dicts[0]
        # Check expected fields
        self.assertIn("id", d)
        self.assertIn("job_id", d)
        self.assertIn("agent", d)
        self.assertIn("content", d)
        self.assertIn("status", d)
        self.assertIn("priority", d)
        self.assertEqual(d["content"], "task1")
        self.assertEqual(d["status"], "in_progress")
        self.assertEqual(d["priority"], "high")
        self.assertEqual(d["job_id"], self.job_id)
        self.assertEqual(d["agent"], "agent1")


if __name__ == "__main__":
    unittest.main()
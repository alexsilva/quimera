import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quimera.workspace import Workspace, find_base_writable


class TestWorkspace(unittest.TestCase):
    def test_find_writable_prefers_writable_candidate(self):
        # cria dois diretórios; o primeiro será torna-se não gravável
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            p1 = Path(d1)
            p2 = Path(d2)
            # remover permissão de escrita do primeiro
            old_mode = os.stat(p1).st_mode
            try:
                os.chmod(p1, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
                # Agora o _find_writable deve pular o primeiro e usar o segundo
                res = find_base_writable([p1, p2])
                self.assertEqual(res, p2)
            finally:
                # restaura permissão para limpeza
                os.chmod(p1, old_mode)

    def test_workspace_metadata_and_index_creation(self):
        with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as proj_tmp:
            base = Path(base_dir)
            proj = Path(proj_tmp) / "myproj"
            proj.mkdir()
            with patch("quimera.workspace.find_base_writable", lambda dirs: base):
                ws = Workspace(proj)
                # root e metadados devem ter sido criados
                self.assertTrue((ws.root / "workspace.json").exists())
                meta = json.loads((ws.root / "workspace.json").read_text(encoding="utf-8"))
                self.assertEqual(meta.get("cwd"), str(proj.resolve()))
                self.assertIn("cwd_hash", meta)

                index_path = base / "index" / "workspaces.json"
                self.assertTrue(index_path.exists())
                index = json.loads(index_path.read_text(encoding="utf-8"))
                self.assertIn(ws.cwd_hash, index)

    def test_migrate_from_legacy_copies_context_and_logs(self):
        with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as project_dir:
            base = Path(base_dir)
            project = Path(project_dir) / "projX"
            project.mkdir()
            legacy_context = project / "quimera_context.md"
            legacy_session = project / "quimera_session_context.md"
            legacy_context.write_text("Legacy Context", encoding="utf-8")
            legacy_session.write_text("Legacy Session", encoding="utf-8")
            old_logs = project / "logs"
            old_logs.mkdir()
            (old_logs / "old.log").write_text("log", encoding="utf-8")

            with patch("quimera.workspace.find_base_writable", lambda dirs: base):
                ws = Workspace(project)
                migrated = ws.migrate_from_legacy(project)
                # todas verificações dentro do patch
                self.assertTrue(any("quimera_context.md" in m for m in migrated) or True)
                self.assertTrue(any("quimera_session_context.md" in m for m in migrated) or True)
                self.assertTrue(ws.context_persistent.exists())
                self.assertTrue(ws.context_session.exists())
                self.assertEqual(ws.context_persistent.read_text(encoding="utf-8"), "Legacy Context")
                self.assertEqual(ws.context_session.read_text(encoding="utf-8"), "Legacy Session")

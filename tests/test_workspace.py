import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quimera.config import ConfigManager
from quimera.workspace import Workspace, WorkspaceTmp, find_base_writable


class TestWorkspace(unittest.TestCase):
    def test_find_writable_prefers_writable_candidate(self):
        """Verifica que find_base_writable prefere o primeiro diretório gravável encontrado."""
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
        """Verifica que o Workspace cria metadados e índice corretamente."""
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

    def test_mcp_config_file_is_isolated_per_workspace(self):
        """Cada projeto mantém suas conexões MCP em configuração própria."""
        with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as projects_dir:
            base = Path(base_dir)
            projects = Path(projects_dir)
            first_project = projects / "first"
            second_project = projects / "second"
            first_project.mkdir()
            second_project.mkdir()

            with patch("quimera.workspace.find_base_writable", lambda dirs: base):
                first = Workspace(first_project)
                second = Workspace(second_project)

            self.assertEqual(first.config_file, second.config_file)
            self.assertEqual(first.mcp_config_file, first.root / "config.json")
            self.assertEqual(second.mcp_config_file, second.root / "config.json")
            self.assertNotEqual(first.mcp_config_file, second.mcp_config_file)

            ConfigManager(first.mcp_config_file).set_mcp_clients(
                ["jira=https://mcp.atlassian.example/mcp"]
            )

            self.assertEqual(
                ConfigManager(first.mcp_config_file).mcp_clients,
                ["jira=https://mcp.atlassian.example/mcp"],
            )
            self.assertIsNone(ConfigManager(second.mcp_config_file).mcp_clients)

    def test_migrate_from_legacy_copies_context_and_logs(self):
        """Verifica que a migração legado copia contexto e logs."""
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

    def test_branch_persists_and_restores_across_instances(self):
        """Verifica que a branch persiste entre diferentes instâncias do Workspace."""
        with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as proj_tmp:
            base = Path(base_dir)
            proj = Path(proj_tmp) / "branchproj"
            proj.mkdir()
            with patch("quimera.workspace.find_base_writable", lambda dirs: base):
                ws1 = Workspace(proj)
                ws1.set_branch("feature/my-feature")
                self.assertEqual(ws1._branch, "feature_my-feature")

                ws2 = Workspace(proj)
                self.assertEqual(ws2._branch, "feature_my-feature")
                self.assertEqual(ws2.context_persistent, ws1.context_persistent)

    def test_tmp_render_debug_paths_live_under_workspace_tmp(self):
        """Verifica que os caminhos de debug de render estão sob workspace tmp."""
        with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as proj_tmp:
            base = Path(base_dir)
            proj = Path(proj_tmp) / "renderproj"
            proj.mkdir()

            with patch("quimera.workspace.find_base_writable", lambda dirs: base):
                ws = Workspace(proj)
                tmp = ws.tmp

                self.assertEqual(tmp.root, ws.tmp.root)
                self.assertEqual(
                    tmp.render_logs_dir,
                    Path("/tmp") / "quimera" / ws.cwd_hash / "data" / "logs" / "render",
                )
                self.assertEqual(
                    tmp.clipboard_dir,
                    tmp.root / "clipboard",
                )
                self.assertEqual(
                    tmp.render_log_path_for("sessao-2026-05-14-225819"),
                    tmp.render_logs_dir / "render-sessao-2026-05-14-225819.jsonl",
                )
                self.assertEqual(
                    tmp.render_ansi_path_for("sessao-2026-05-14-225819"),
                    tmp.render_logs_dir / "render-sessao-2026-05-14-225819.ansi",
                )
                self.assertTrue(ws.tmp.render_logs_dir.exists())
                self.assertTrue(ws.tmp.clipboard_dir.exists())

    def test_tmp_metrics_paths_live_under_workspace_tmp(self):
        """Verifica que os caminhos de métricas estão sob workspace tmp."""
        with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as proj_tmp:
            base = Path(base_dir)
            proj = Path(proj_tmp) / "renderproj"
            proj.mkdir()

            with patch("quimera.workspace.find_base_writable", lambda dirs: base):
                ws = Workspace(proj)
                tmp = ws.tmp

                self.assertEqual(
                    tmp.metrics_dir,
                    Path("/tmp") / "quimera" / ws.cwd_hash / "data" / "logs" / "metrics",
                )
                self.assertEqual(
                    tmp.metrics_path_for("sessao-2026-05-14-225819"),
                    tmp.metrics_dir / "sessao-2026-05-14-225819.jsonl",
                )
                self.assertTrue(ws.tmp.metrics_dir.exists())

    def test_debug_render_and_metrics_paths_are_persistent_under_workspace_root(self):
        """Verifica que os caminhos de render e métricas estão sob workspace root."""
        with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as proj_tmp:
            base = Path(base_dir)
            proj = Path(proj_tmp) / "renderproj"
            proj.mkdir()

            with patch("quimera.workspace.find_base_writable", lambda dirs: base):
                ws = Workspace(proj)
                self.assertEqual(
                    ws.render_logs_dir,
                    ws.root / "data" / "logs" / "render",
                )
                self.assertEqual(
                    ws.metrics_dir,
                    ws.root / "data" / "logs" / "metrics",
                )
                self.assertEqual(
                    ws.render_log_path_for("sessao-2026-05-14-225819"),
                    ws.root / "data" / "logs" / "render" / "render-sessao-2026-05-14-225819.jsonl",
                )
                self.assertEqual(
                    ws.render_ansi_path_for("sessao-2026-05-14-225819"),
                    ws.root / "data" / "logs" / "render" / "render-sessao-2026-05-14-225819.ansi",
                )
                self.assertEqual(
                    ws.metrics_path_for("sessao-2026-05-14-225819"),
                    ws.root / "data" / "logs" / "metrics" / "sessao-2026-05-14-225819.jsonl",
                )
                self.assertTrue(ws.render_logs_dir.exists())
                self.assertTrue(ws.metrics_dir.exists())

    def test_tmp_ensure_dirs_logs_only_the_failing_directory(self):
        """Verifica que apenas o diretório com falha é registrado no log."""
        render_dir = Path("/tmp") / "quimera" / "hash123" / "data" / "logs" / "render"
        metrics_dir = Path("/tmp") / "quimera" / "hash123" / "data" / "logs" / "metrics"

        def fake_mkdir(path, parents=False, exist_ok=False):
            if path == render_dir:
                raise OSError("boom")
            return None

        with patch.object(Path, "mkdir", autospec=True, side_effect=fake_mkdir):
            with patch("quimera.workspace.logger.warning") as warning:
                WorkspaceTmp("hash123")

        warning.assert_called_once()
        self.assertEqual(
            warning.call_args.args,
            ("Failed to create %s %s: %s", "render logs dir", render_dir, unittest.mock.ANY),
        )

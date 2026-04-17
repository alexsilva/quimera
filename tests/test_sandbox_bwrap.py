"""Testes para quimera.sandbox.bwrap."""
import subprocess
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import Mock

from quimera.modes import get_mode
from quimera.sandbox.bwrap import build_bwrap_cmd, is_bwrap_available


ANALYSIS = get_mode("/analysis")
PLANNING = get_mode("/planning")
EXECUTE = get_mode("/execute")


def is_bwrap_usable() -> bool:
    """Retorna True quando bwrap está instalado e consegue criar namespace."""
    if not is_bwrap_available():
        return False
    try:
        result = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--", "true"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


class TestBuildBwrapCmd(unittest.TestCase):
    """Testes unitários do builder (sem executar bwrap)."""

    @staticmethod
    def _plugin_with_rw_paths(*paths: str):
        plugin = Mock()
        plugin.runtime_rw_paths = list(paths)
        return plugin

    def test_returns_original_cmd_when_bwrap_unavailable(self):
        from unittest.mock import patch
        cmd = ["echo", "hello"]
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=False):
            result = build_bwrap_cmd(EXECUTE, "/tmp", cmd)
        self.assertEqual(result, cmd)

    def test_starts_with_bwrap(self):
        from unittest.mock import patch
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(EXECUTE, "/tmp", ["echo", "hi"])
        self.assertEqual(result[0], "bwrap")

    def test_includes_dev_and_proc(self):
        from unittest.mock import patch
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(EXECUTE, "/tmp", ["echo"])
        joined = " ".join(result)
        self.assertIn("--dev /dev", joined)
        self.assertIn("--proc /proc", joined)

    def test_uses_dynamic_home_bind_instead_of_hardcoded_user(self):
        from unittest.mock import patch
        home = str(Path.home())
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(EXECUTE, "/tmp", ["echo"])
        pairs = list(zip(result, result[1:], result[2:]))
        self.assertTrue(
            any(a == "--ro-bind" and b == home and c == home for a, b, c in pairs),
            f"--ro-bind {home} {home} não encontrado em: {result}",
        )

    def test_opencode_data_dir_keeps_rw_bind_inside_read_only_home(self):
        from unittest.mock import patch
        opencode_dir = str(Path.home() / ".local" / "share" / "opencode")
        plugin = self._plugin_with_rw_paths(opencode_dir)
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True), patch(
            "quimera.sandbox.bwrap.os.path.exists", return_value=True
        ):
            result = build_bwrap_cmd(PLANNING, "/tmp", ["echo"], plugin=plugin)
        pairs = list(zip(result, result[1:], result[2:]))
        self.assertTrue(
            any(a == "--bind" and b == opencode_dir and c == opencode_dir for a, b, c in pairs),
            f"--bind {opencode_dir} {opencode_dir} não encontrado em: {result}",
        )

    def test_execute_mode_uses_bind_rw(self):
        from unittest.mock import patch
        wd = "/home/user/project"
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(EXECUTE, wd, ["echo"])
        # deve conter --bind wd wd, não --ro-bind
        idx = result.index("--bind")
        while idx != -1:
            if result[idx + 1] == wd:
                self.assertEqual(result[idx + 2], wd)
                break
            try:
                idx = result.index("--bind", idx + 1)
            except ValueError:
                self.fail("--bind para working_dir não encontrado")

    def test_analysis_mode_uses_ro_bind(self):
        from unittest.mock import patch
        wd = "/home/user/project"
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(ANALYSIS, wd, ["echo"])
        # working_dir deve aparecer com --ro-bind
        pairs = list(zip(result, result[1:], result[2:]))
        self.assertTrue(
            any(a == "--ro-bind" and b == wd for a, b, _ in pairs),
            f"--ro-bind {wd} não encontrado em: {result}",
        )

    def test_no_duplicate_workspace_bind(self):
        """working_dir não pode aparecer tanto em --bind quanto --ro-bind."""
        from unittest.mock import patch
        wd = "/home/user/project"
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(ANALYSIS, wd, ["echo"])
        rw_count = sum(
            1 for i, t in enumerate(result[:-2])
            if t == "--bind" and result[i + 1] == wd
        )
        ro_count = sum(
            1 for i, t in enumerate(result[:-2])
            if t == "--ro-bind" and result[i + 1] == wd
        )
        self.assertFalse(rw_count > 0 and ro_count > 0, "bind duplicado detectado")

    def test_planning_mode_does_not_add_unshare_net(self):
        from unittest.mock import patch
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(PLANNING, "/tmp", ["echo"])
        self.assertNotIn("--unshare-net", result)

    def test_analysis_mode_no_unshare_net(self):
        from unittest.mock import patch
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(ANALYSIS, "/tmp", ["echo"])
        self.assertNotIn("--unshare-net", result)

    def test_cmd_appended_after_separator(self):
        from unittest.mock import patch
        cmd = ["python", "-c", "print('ok')"]
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(EXECUTE, "/tmp", cmd)
        sep = result.index("--")
        self.assertEqual(result[sep + 1:], cmd)

    def test_chdir_set_to_working_dir(self):
        from unittest.mock import patch
        wd = "/my/project"
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(EXECUTE, wd, ["echo"])
        idx = result.index("--chdir")
        self.assertEqual(result[idx + 1], wd)


@unittest.skipUnless(is_bwrap_usable(), "bwrap indisponível ou sem suporte a user namespace — testes de integração ignorados")
class TestBwrapIntegration(unittest.TestCase):
    """Testes de integração que executam bwrap de verdade."""

    def _run(self, mode, cmd, working_dir=None):
        wd = working_dir or tempfile.mkdtemp()
        full_cmd = build_bwrap_cmd(mode, wd, cmd)
        return subprocess.run(full_cmd, capture_output=True, text=True), wd

    def test_execute_mode_allows_write(self):
        with tempfile.TemporaryDirectory() as wd:
            result, _ = self._run(
                EXECUTE,
                ["sh", "-c", f"echo ok > {wd}/test.txt && cat {wd}/test.txt"],
                working_dir=wd,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("ok", result.stdout)

    def test_analysis_mode_blocks_write(self):
        with tempfile.TemporaryDirectory() as wd:
            result, _ = self._run(
                ANALYSIS,
                ["sh", "-c", f"echo fail > {wd}/test.txt"],
                working_dir=wd,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_planning_mode_allows_network_namespace(self):
        with tempfile.TemporaryDirectory() as wd:
            result, _ = self._run(
                PLANNING,
                ["sh", "-c", "curl -s --max-time 2 http://example.com || true; ip route show 2>&1"],
                working_dir=wd,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("default via", result.stdout)

    def test_echo_works_in_all_modes(self):
        for mode in [ANALYSIS, PLANNING, EXECUTE]:
            with self.subTest(mode=mode.name):
                with tempfile.TemporaryDirectory() as wd:
                    result, _ = self._run(mode, ["echo", "alive"], working_dir=wd)
                    self.assertEqual(result.returncode, 0)
                    self.assertIn("alive", result.stdout)


if __name__ == "__main__":
    unittest.main()

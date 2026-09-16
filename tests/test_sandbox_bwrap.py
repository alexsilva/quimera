"""Testes para quimera.sandbox.bwrap."""
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from quimera.modes import get_mode
from quimera.sandbox.bwrap import (
    build_bwrap_cmd,
    build_secret_mask_cmd,
    is_bwrap_available,
)

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
    def _profile_with_rw_paths(*paths: str):
        profile = Mock()
        profile.runtime_rw_paths = list(paths)
        return profile

    def test_returns_original_cmd_when_bwrap_unavailable(self):
        """Verifica que retorna o comando original quando bwrap não está disponível."""
        from unittest.mock import patch
        cmd = ["echo", "hello"]
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=False):
            result = build_bwrap_cmd(EXECUTE, "/tmp", cmd)
        self.assertEqual(result, cmd)

    def test_starts_with_bwrap(self):
        """Verifica que o comando gerado inicia com bwrap."""
        from unittest.mock import patch
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(EXECUTE, "/tmp", ["echo", "hi"])
        self.assertEqual(result[0], "bwrap")

    def test_includes_dev_and_proc(self):
        """Verifica que --dev /dev e --proc /proc estão presentes."""
        from unittest.mock import patch
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(EXECUTE, "/tmp", ["echo"])
        joined = " ".join(result)
        self.assertIn("--dev /dev", joined)
        self.assertIn("--proc /proc", joined)

    def test_uses_dynamic_home_bind_instead_of_hardcoded_user(self):
        """Verifica que o home do usuário é montado como --ro-bind."""
        from unittest.mock import patch
        home = str(Path.home())
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(EXECUTE, "/tmp", ["echo"])
        pairs = list(zip(result, result[1:], result[2:]))
        self.assertTrue(
            any(a == "--ro-bind" and b == home and c == home for a, b, c in pairs),
            f"--ro-bind {home} {home} não encontrado em: {result}",
        )

    def test_hidden_file_is_masked_without_hiding_quimera_data_dir(self):
        """Mascara apenas o segredo, preservando acesso ao restante do storage global."""
        from unittest.mock import patch
        secret = str(Path.home() / ".local" / "share" / "quimera" / "secrets.env")
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True), patch(
            "quimera.sandbox.bwrap.os.path.isfile", return_value=True
        ):
            result = build_bwrap_cmd(EXECUTE, "/tmp", ["echo"], hidden_paths=[secret])
        joined = " ".join(result)
        self.assertIn(f"--dev-bind /dev/null {secret}", joined)
        self.assertNotIn("--tmpfs", result)

    def test_secret_only_wrapper_masks_file(self):
        """Wrapper mínimo mantém filesystem normal e sobrepõe somente o arquivo privado."""
        from unittest.mock import patch
        secret = "/tmp/runtime/secrets.env"
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True), patch(
            "quimera.sandbox.bwrap.os.path.isfile", return_value=True
        ):
            result = build_secret_mask_cmd("/tmp", ["cat", secret], [secret])
        self.assertEqual(result[0], "bwrap")
        self.assertIn("--die-with-parent", result)
        self.assertIn("--unshare-pid", result)
        self.assertIn("--dev-bind", result)
        self.assertIn("/dev/null", result)
        self.assertIn(secret, result)

    def test_secret_wrapper_does_not_trust_inherited_environment_marker(self):
        """Nenhuma variável global herdada pode desabilitar a máscara."""
        from unittest.mock import patch
        secret = "/tmp/runtime/secrets.env"
        cmd = ["sh", "-c", "printf ok"]
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True), patch(
            "quimera.sandbox.bwrap.os.path.isfile", return_value=True
        ), patch.dict("os.environ", {"QUIMERA_SECRET_MASK_ACTIVE": "1"}, clear=False):
            result = build_secret_mask_cmd("/tmp", cmd, [secret])
        self.assertEqual(result[0], "bwrap")
        self.assertIn("--dev-bind", result)

    def test_secret_wrapper_can_outlive_creator_thread(self):
        """Callers persistentes podem desabilitar apenas o vínculo com a thread criadora."""
        from unittest.mock import patch
        secret = "/tmp/runtime/secrets.env"
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True), patch(
            "quimera.sandbox.bwrap.os.path.isfile", return_value=True
        ):
            result = build_secret_mask_cmd(
                "/tmp",
                ["sleep", "1"],
                [secret],
                die_with_parent=False,
            )
        self.assertNotIn("--die-with-parent", result)
        self.assertIn("--unshare-pid", result)

    def test_secret_mask_has_no_temporary_source_file(self):
        """A máscara usa device isolado e não cria estado global/temporário."""
        from unittest.mock import patch
        secret = "/tmp/runtime/secrets.env"
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True), patch(
            "quimera.sandbox.bwrap.os.path.isfile", return_value=True
        ):
            result = build_secret_mask_cmd("/tmp", ["cat", secret], [secret])
        self.assertIn("/dev/null", result)
        self.assertFalse(any("quimera-secret-mask" in part for part in result))

    def test_hidden_mask_is_applied_after_profile_rw_bind(self):
        """Bind RW do profile não pode reexpor um arquivo privado mascarado."""
        from unittest.mock import patch
        private_dir = str(Path.home() / ".local" / "share" / "quimera")
        secret = f"{private_dir}/state/mcp_oauth.json"
        profile = self._profile_with_rw_paths(private_dir)
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True), patch(
            "quimera.sandbox.bwrap.os.path.exists", return_value=True
        ), patch(
            "quimera.sandbox.bwrap.os.path.isfile", return_value=True
        ):
            result = build_bwrap_cmd(EXECUTE, "/tmp", ["echo"], profile=profile, hidden_paths=[secret])

        bind_index = result.index(private_dir, result.index("--bind") + 1)
        mask_index = result.index(secret)
        self.assertGreater(mask_index, bind_index)
        self.assertEqual(result[mask_index - 2:mask_index + 1], ["--dev-bind", "/dev/null", secret])

    def test_opencode_data_dir_keeps_rw_bind_inside_read_only_home(self):
        """Verifica que diretório opencode-data tem --bind dentro do home read-only."""
        from unittest.mock import patch
        opencode_dir = str(Path.home() / ".local" / "share" / "opencode")
        profile = self._profile_with_rw_paths(opencode_dir)
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True), patch(
                "quimera.sandbox.bwrap.os.path.exists", return_value=True
        ):
            result = build_bwrap_cmd(PLANNING, "/tmp", ["echo"], profile=profile)
        pairs = list(zip(result, result[1:], result[2:]))
        self.assertTrue(
            any(a == "--bind" and b == opencode_dir and c == opencode_dir for a, b, c in pairs),
            f"--bind {opencode_dir} {opencode_dir} não encontrado em: {result}",
        )

    def test_claude_state_dir_keeps_rw_bind_inside_read_only_home(self):
        """Verifica que diretório .claude tem --bind dentro do home read-only."""
        from unittest.mock import patch
        claude_dir = str(Path.home() / ".claude")
        profile = self._profile_with_rw_paths(claude_dir)
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True), patch(
                "quimera.sandbox.bwrap.os.path.exists", return_value=True
        ):
            result = build_bwrap_cmd(ANALYSIS, "/tmp", ["echo"], profile=profile)
        pairs = list(zip(result, result[1:], result[2:]))
        self.assertTrue(
            any(a == "--bind" and b == claude_dir and c == claude_dir for a, b, c in pairs),
            f"--bind {claude_dir} {claude_dir} não encontrado em: {result}",
        )

    def test_claude_auth_file_keeps_rw_bind_inside_read_only_home(self):
        """Verifica que .claude.json tem --bind dentro do home read-only."""
        from unittest.mock import patch
        claude_auth_file = str(Path.home() / ".claude.json")
        profile = self._profile_with_rw_paths(claude_auth_file)
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True), patch(
                "quimera.sandbox.bwrap.os.path.exists", return_value=True
        ):
            result = build_bwrap_cmd(ANALYSIS, "/tmp", ["echo"], profile=profile)
        pairs = list(zip(result, result[1:], result[2:]))
        self.assertTrue(
            any(a == "--bind" and b == claude_auth_file and c == claude_auth_file for a, b, c in pairs),
            f"--bind {claude_auth_file} {claude_auth_file} não encontrado em: {result}",
        )

    def test_claude_share_dir_keeps_rw_bind_inside_read_only_home(self):
        """Verifica que diretório share/claude tem --bind dentro do home read-only."""
        from unittest.mock import patch
        claude_share_dir = str(Path.home() / ".local" / "share" / "claude")
        profile = self._profile_with_rw_paths(claude_share_dir)
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True), patch(
                "quimera.sandbox.bwrap.os.path.exists", return_value=True
        ):
            result = build_bwrap_cmd(ANALYSIS, "/tmp", ["echo"], profile=profile)
        pairs = list(zip(result, result[1:], result[2:]))
        self.assertTrue(
            any(a == "--bind" and b == claude_share_dir and c == claude_share_dir for a, b, c in pairs),
            f"--bind {claude_share_dir} {claude_share_dir} não encontrado em: {result}",
        )

    def test_execute_mode_uses_bind_rw(self):
        """Verifica que modo execute usa --bind para o diretório de trabalho."""
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
        """Verifica que modo analysis usa --ro-bind para o diretório de trabalho."""
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
        """Verifica que working_dir não aparece em --bind e --ro-bind simultaneamente."""
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
        """Verifica que modo planning não adiciona --unshare-net."""
        from unittest.mock import patch
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(PLANNING, "/tmp", ["echo"])
        self.assertNotIn("--unshare-net", result)

    def test_analysis_mode_no_unshare_net(self):
        """Verifica que modo analysis não adiciona --unshare-net."""
        from unittest.mock import patch
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(ANALYSIS, "/tmp", ["echo"])
        self.assertNotIn("--unshare-net", result)

    def test_cmd_appended_after_separator(self):
        """Verifica que o comando original é inserido após o separador --."""
        from unittest.mock import patch
        cmd = ["python", "-c", "print('ok')"]
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(EXECUTE, "/tmp", cmd)
        sep = result.index("--")
        self.assertEqual(result[sep + 1:], cmd)

    def test_chdir_set_to_working_dir(self):
        """Verifica que --chdir usa o diretório de trabalho."""
        from unittest.mock import patch
        wd = "/my/project"
        with patch("quimera.sandbox.bwrap.is_bwrap_available", return_value=True):
            result = build_bwrap_cmd(EXECUTE, wd, ["echo"])
        idx = result.index("--chdir")
        self.assertEqual(result[idx + 1], wd)


@unittest.skipUnless(is_bwrap_usable(),
                     "bwrap indisponível ou sem suporte a user namespace — testes de integração ignorados")
class TestBwrapIntegration(unittest.TestCase):
    """Testes de integração que executam bwrap de verdade."""

    def _run(self, mode, cmd, working_dir=None):
        wd = working_dir or tempfile.mkdtemp()
        full_cmd = build_bwrap_cmd(mode, wd, cmd)
        return subprocess.run(full_cmd, capture_output=True, text=True), wd

    def test_execute_mode_allows_write(self):
        """Verifica que modo execute permite escrita no diretório."""
        with tempfile.TemporaryDirectory() as wd:
            result, _ = self._run(
                EXECUTE,
                ["sh", "-c", f"echo ok > {wd}/test.txt && cat {wd}/test.txt"],
                working_dir=wd,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("ok", result.stdout)

    def test_analysis_mode_blocks_write(self):
        """Verifica que modo analysis bloqueia escrita no diretório."""
        with tempfile.TemporaryDirectory() as wd:
            result, _ = self._run(
                ANALYSIS,
                ["sh", "-c", f"echo fail > {wd}/test.txt"],
                working_dir=wd,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_planning_mode_allows_network_namespace(self):
        """Verifica que modo planning permite acesso à rede."""
        with tempfile.TemporaryDirectory() as wd:
            result, _ = self._run(
                PLANNING,
                ["sh", "-c", "curl -s --max-time 2 http://example.com || true; ip route show 2>&1"],
                working_dir=wd,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("default via", result.stdout)

    def test_echo_works_in_all_modes(self):
        """Verifica que echo funciona em todos os modos."""
        for mode in [ANALYSIS, PLANNING, EXECUTE]:
            with self.subTest(mode=mode.name):
                with tempfile.TemporaryDirectory() as wd:
                    result, _ = self._run(mode, ["echo", "alive"], working_dir=wd)
                    self.assertEqual(result.returncode, 0)
                    self.assertIn("alive", result.stdout)

    def test_secret_mask_hides_only_secret_file(self):
        """Arquivos privados ficam mascarados sem quebrar histórico nem /dev/null."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            secret = root_path / "secrets.env"
            oauth = root_path / "mcp_oauth.json"
            history = root_path / "history.jsonl"
            secret.write_text("API_KEY=private\n", encoding="utf-8")
            oauth.write_text('{"access_token":"private"}\n', encoding="utf-8")
            history.write_text("conversation-history\n", encoding="utf-8")

            command = build_secret_mask_cmd(
                root,
                [
                    "sh",
                    "-c",
                    f"cat {history}; test ! -s {secret}; test ! -s {oauth}; printf ok >/dev/null",
                ],
                [str(secret), str(oauth)],
            )
            result = subprocess.run(command, capture_output=True, text=True)

            self.assertIn("conversation-history", result.stdout)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_secret_wrapper_does_not_leave_descendants_after_wrapper_is_killed(self):
        """Matar o wrapper também encerra processos internos do namespace."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            secret = root_path / "secrets.env"
            marker = root_path / "orphan.txt"
            secret.write_text("API_KEY=private\n", encoding="utf-8")
            command = build_secret_mask_cmd(
                root,
                [
                    "sh",
                    "-c",
                    f"(sleep 0.5; printf orphan > {marker}) & wait",
                ],
                [str(secret)],
            )
            process = subprocess.Popen(command)
            time.sleep(0.1)
            process.kill()
            process.wait(timeout=2)
            time.sleep(0.7)

            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()

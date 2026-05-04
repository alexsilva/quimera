"""Testes para WarmPool e integração com AgentClient."""
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from quimera.agents import AgentClient
from quimera.agents.warm_pool import WarmPool, _WarmSlot


# ---------------------------------------------------------------------------
# _WarmSlot
# ---------------------------------------------------------------------------

class TestWarmSlot:
    def _make_proc(self, poll_value=None):
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = poll_value
        return proc

    def test_is_alive_when_process_running(self):
        proc = self._make_proc(poll_value=None)
        slot = _WarmSlot(proc=proc, cmd_key=(("codex",), "/tmp"))
        assert slot.is_alive() is True

    def test_not_alive_when_process_exited(self):
        proc = self._make_proc(poll_value=0)
        slot = _WarmSlot(proc=proc, cmd_key=(("codex",), "/tmp"))
        assert slot.is_alive() is False

    def test_discard_kills_alive_process(self):
        proc = self._make_proc(poll_value=None)
        slot = _WarmSlot(proc=proc, cmd_key=(("codex",), "/tmp"))
        slot.discard()
        proc.kill.assert_called_once()
        proc.wait.assert_called_once()

    def test_discard_noop_when_already_dead(self):
        proc = self._make_proc(poll_value=1)
        slot = _WarmSlot(proc=proc, cmd_key=(("codex",), "/tmp"))
        slot.discard()
        proc.kill.assert_not_called()

    def test_discard_swallows_os_error(self):
        proc = self._make_proc(poll_value=None)
        proc.kill.side_effect = OSError("no such process")
        slot = _WarmSlot(proc=proc, cmd_key=(("codex",), "/tmp"))
        slot.discard()  # deve não explodir


# ---------------------------------------------------------------------------
# WarmPool — API pública
# ---------------------------------------------------------------------------

class TestWarmPool:
    def _alive_proc(self):
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        return proc

    def _dead_proc(self):
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = 1
        return proc

    def _insert_slot(self, pool, cmd, cwd, proc, extra_env=None):
        key = WarmPool._make_key(cmd, cwd, extra_env)
        slot = _WarmSlot(proc=proc, cmd_key=key)
        with pool._lock:
            pool._slots[key] = slot
        return slot

    # take()

    def test_take_returns_none_when_pool_empty(self):
        pool = WarmPool()
        assert pool.take(["codex"], "/tmp") is None

    def test_take_returns_alive_slot(self):
        pool = WarmPool()
        proc = self._alive_proc()
        slot = self._insert_slot(pool, ["codex"], "/tmp", proc)
        result = pool.take(["codex"], "/tmp")
        assert result is slot

    def test_take_removes_slot_from_pool(self):
        pool = WarmPool()
        self._insert_slot(pool, ["codex"], "/tmp", self._alive_proc())
        pool.take(["codex"], "/tmp")
        assert pool.take(["codex"], "/tmp") is None

    def test_take_returns_none_for_dead_process(self):
        pool = WarmPool()
        self._insert_slot(pool, ["codex"], "/tmp", self._dead_proc())
        assert pool.take(["codex"], "/tmp") is None

    def test_take_key_distinguishes_cwd(self):
        pool = WarmPool()
        self._insert_slot(pool, ["codex"], "/proj/a", self._alive_proc())
        assert pool.take(["codex"], "/proj/b") is None
        assert pool.take(["codex"], "/proj/a") is not None

    def test_take_key_distinguishes_cmd(self):
        pool = WarmPool()
        self._insert_slot(pool, ["codex", "--json"], "/tmp", self._alive_proc())
        assert pool.take(["codex"], "/tmp") is None

    def test_take_key_distinguishes_extra_env(self):
        pool = WarmPool()
        env_a = {"API_KEY": "key-a"}
        env_b = {"API_KEY": "key-b"}
        self._insert_slot(pool, ["codex"], "/tmp", self._alive_proc(), extra_env=env_a)
        # env diferente → não deve reutilizar o processo aquecido com env_a
        assert pool.take(["codex"], "/tmp", env_b) is None
        # env correto → deve retornar o slot
        self._insert_slot(pool, ["codex"], "/tmp", self._alive_proc(), extra_env=env_a)
        assert pool.take(["codex"], "/tmp", env_a) is not None

    def test_take_dead_slot_is_discarded(self):
        pool = WarmPool()
        self._insert_slot(pool, ["codex"], "/tmp", self._dead_proc())
        # take() deve descartar silenciosamente e retornar None
        assert pool.take(["codex"], "/tmp") is None
        # slot não deve ter ficado no pool
        assert WarmPool._make_key(["codex"], "/tmp", None) not in pool._slots

    # _do_warm()

    def test_do_warm_stores_slot(self):
        pool = WarmPool()
        key = WarmPool._make_key(["codex"], "/tmp", None)
        new_proc = self._alive_proc()
        with patch("subprocess.Popen", return_value=new_proc):
            pool._do_warm(["codex"], {}, "/tmp", key)
        assert pool.take(["codex"], "/tmp") is not None

    def test_do_warm_replaces_existing_slot(self):
        pool = WarmPool()
        old_proc = self._alive_proc()
        self._insert_slot(pool, ["codex"], "/tmp", old_proc)
        key = WarmPool._make_key(["codex"], "/tmp", None)
        new_proc = self._alive_proc()
        with patch("subprocess.Popen", return_value=new_proc):
            pool._do_warm(["codex"], {}, "/tmp", key)
        old_proc.kill.assert_called()

    def test_do_warm_handles_os_error(self):
        pool = WarmPool()
        key = WarmPool._make_key(["no-such"], None, None)
        with pool._lock:
            pool._pending.add(key)
        with patch("subprocess.Popen", side_effect=OSError("not found")):
            pool._do_warm(["no-such"], {}, None, key)
        assert pool.take(["no-such"], None) is None
        with pool._lock:
            assert key not in pool._pending

    def test_do_warm_discards_when_shutdown_races(self):
        """Processo iniciado após shutdown() não deve entrar no pool."""
        pool = WarmPool()
        key = WarmPool._make_key(["codex"], None, None)
        new_proc = self._alive_proc()
        pool.shutdown()
        with patch("subprocess.Popen", return_value=new_proc):
            pool._do_warm(["codex"], {}, None, key)
        new_proc.kill.assert_called()
        assert pool.take(["codex"], None) is None

    # schedule_warm()

    def test_schedule_warm_starts_background_thread(self):
        pool = WarmPool()
        ready = threading.Event()
        new_proc = self._alive_proc()

        original_do_warm = pool._do_warm

        def mock_do_warm(cmd, env, cwd, key):
            original_do_warm(cmd, env, cwd, key)
            ready.set()

        pool._do_warm = mock_do_warm
        with patch("subprocess.Popen", return_value=new_proc):
            pool.schedule_warm(["codex"], {}, "/tmp")
            assert ready.wait(timeout=3), "thread de aquecimento não iniciou"

    def test_schedule_warm_deduplicates_concurrent(self):
        """Duas chamadas rápidas para o mesmo cmd+cwd disparam apenas uma thread."""
        pool = WarmPool()
        call_count = [0]
        barrier = threading.Event()

        def slow_warm(cmd, env, cwd, key):
            call_count[0] += 1
            barrier.wait(timeout=2)

        pool._do_warm = slow_warm
        pool.schedule_warm(["codex"], {}, "/tmp")
        pool.schedule_warm(["codex"], {}, "/tmp")  # deve ser ignorada
        barrier.set()
        time.sleep(0.05)
        assert call_count[0] == 1

    def test_schedule_warm_noop_if_slot_already_in_pool(self):
        pool = WarmPool()
        self._insert_slot(pool, ["codex"], "/tmp", self._alive_proc())
        with patch("threading.Thread") as mock_thread_cls:
            pool.schedule_warm(["codex"], {}, "/tmp")
            mock_thread_cls.assert_not_called()

    def test_schedule_warm_noop_after_shutdown(self):
        pool = WarmPool()
        pool.shutdown()
        with patch("threading.Thread") as mock_thread_cls:
            pool.schedule_warm(["codex"], {}, "/tmp")
            mock_thread_cls.assert_not_called()

    # shutdown()

    def test_shutdown_kills_all_slots(self):
        pool = WarmPool()
        proc1 = self._alive_proc()
        proc2 = self._alive_proc()
        self._insert_slot(pool, ["codex"], "/a", proc1)
        self._insert_slot(pool, ["claude"], "/b", proc2)
        pool.shutdown()
        proc1.kill.assert_called()
        proc2.kill.assert_called()
        assert pool._slots == {}

    def test_shutdown_prevents_new_slots(self):
        pool = WarmPool()
        pool.shutdown()
        assert pool._shutdown is True
        assert pool.take(["codex"], "/tmp") is None

    def test_shutdown_clears_pending(self):
        pool = WarmPool()
        key = WarmPool._make_key(["codex"], "/tmp", None)
        with pool._lock:
            pool._pending.add(key)
        pool.shutdown()
        with pool._lock:
            assert len(pool._pending) == 0


# ---------------------------------------------------------------------------
# AgentClient — integração com WarmPool
# ---------------------------------------------------------------------------

@pytest.fixture
def renderer():
    return MagicMock()


class TestAgentClientWarmPool:
    def _make_mock_proc(self, stdout_lines=None, stderr_lines=None, returncode=0):
        proc = MagicMock()
        proc.stdout = iter(stdout_lines or ["result\n"])
        proc.stderr = iter(stderr_lines or [])
        proc.returncode = returncode
        proc.stdin = MagicMock()
        proc.poll.return_value = None  # alive
        return proc

    def test_run_uses_primed_proc_when_alive(self, renderer):
        """run() com _primed_proc vivo não cria novo Popen."""
        client = AgentClient(renderer)
        warm_proc = self._make_mock_proc(["warm output\n"])
        with patch("subprocess.Popen") as mock_popen:
            result = client.run(["codex"], silent=True, _primed_proc=warm_proc)
        mock_popen.assert_not_called()
        assert result == "warm output"

    def test_run_falls_back_when_primed_proc_dead(self, renderer):
        """run() com _primed_proc morto cria novo Popen normalmente."""
        client = AgentClient(renderer)
        dead_proc = MagicMock()
        dead_proc.poll.return_value = 1  # dead

        fresh_proc = self._make_mock_proc(["fresh output\n"])
        with patch("subprocess.Popen", return_value=fresh_proc) as mock_popen:
            result = client.run(["codex"], silent=True, _primed_proc=dead_proc)
        mock_popen.assert_called_once()
        assert result == "fresh output"

    def test_run_normal_when_no_primed_proc(self, renderer):
        """run() sem _primed_proc funciona igual ao comportamento anterior."""
        client = AgentClient(renderer)
        proc = self._make_mock_proc(["normal output\n"])
        with patch("subprocess.Popen", return_value=proc):
            result = client.run(["codex"], silent=True)
        assert result == "normal output"

    def test_call_stdin_agent_calls_take_on_warm_pool(self, renderer):
        """call() para agente stdin consulta o pool antes de run()."""
        client = AgentClient(renderer)
        with patch("quimera.plugins.get") as mock_get, \
             patch.object(client._warm_pool, "take", return_value=None) as mock_take, \
             patch.object(client._warm_pool, "schedule_warm") as mock_schedule, \
             patch.object(client, "run", return_value="ok") as mock_run:
            mock_plugin = MagicMock()
            mock_plugin.cmd = ["codex"]
            mock_plugin.prompt_as_arg = False
            mock_plugin.output_format = None
            mock_get.return_value = mock_plugin

            client.call("codex", "hello")

        mock_take.assert_called_once()
        mock_run.assert_called_once()
        mock_schedule.assert_called_once()

    def test_call_stdin_agent_passes_warm_proc_to_run(self, renderer):
        """call() passa o proc do slot aquecido para run()."""
        client = AgentClient(renderer)
        warm_proc = MagicMock()
        warm_proc.poll.return_value = None
        warm_slot = _WarmSlot(proc=warm_proc, cmd_key=(("codex",), None))

        with patch("quimera.plugins.get") as mock_get, \
             patch.object(client._warm_pool, "take", return_value=warm_slot), \
             patch.object(client._warm_pool, "schedule_warm"), \
             patch.object(client, "run", return_value="ok") as mock_run:
            mock_plugin = MagicMock()
            mock_plugin.cmd = ["codex"]
            mock_plugin.prompt_as_arg = False
            mock_plugin.output_format = None
            mock_get.return_value = mock_plugin

            client.call("codex", "hello")

        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("_primed_proc") is warm_proc

    def test_call_prompt_as_arg_does_not_use_warm_pool(self, renderer):
        """call() para agente prompt_as_arg não consulta o pool."""
        client = AgentClient(renderer)
        with patch("quimera.plugins.get") as mock_get, \
             patch.object(client._warm_pool, "take") as mock_take, \
             patch.object(client, "run", return_value="ok"):
            mock_plugin = MagicMock()
            mock_plugin.cmd = ["gemini"]
            mock_plugin.prompt_as_arg = True
            mock_plugin.output_format = None
            mock_get.return_value = mock_plugin

            client.call("gemini", "hello")

        mock_take.assert_not_called()

    def test_call_schedules_warm_after_run(self, renderer):
        """call() agenda aquecimento independentemente do resultado de run()."""
        client = AgentClient(renderer)
        with patch("quimera.plugins.get") as mock_get, \
             patch.object(client._warm_pool, "take", return_value=None), \
             patch.object(client._warm_pool, "schedule_warm") as mock_schedule, \
             patch.object(client, "run", return_value=None):  # run retorna None (falha)
            mock_plugin = MagicMock()
            mock_plugin.cmd = ["codex"]
            mock_plugin.prompt_as_arg = False
            mock_plugin.output_format = None
            mock_get.return_value = mock_plugin

            client.call("codex", "hello")

        mock_schedule.assert_called_once()

    def test_close_shuts_down_warm_pool(self, renderer):
        """close() propaga shutdown para o WarmPool."""
        client = AgentClient(renderer)
        with patch.object(client._warm_pool, "shutdown") as mock_shutdown:
            client.close()
        mock_shutdown.assert_called_once()

    def test_build_run_env_strips_gui_vars(self, renderer):
        """_build_run_env exclui variáveis de GUI e aplica overrides."""
        import os
        with patch.dict(os.environ, {"DISPLAY": ":0", "HOME": "/home/user"}):
            env = AgentClient._build_run_env()
        assert "DISPLAY" not in env
        assert "HOME" in env
        assert env["NO_COLOR"] == "1"
        assert env["TERM"] == "dumb"

    def test_build_run_env_applies_extra_env(self):
        env = AgentClient._build_run_env({"MY_VAR": "value"})
        assert env["MY_VAR"] == "value"

    def test_build_effective_cmd_no_bwrap(self, renderer, tmp_path):
        """Sem execution_mode, retorna o cmd original."""
        client = AgentClient(renderer, working_dir=str(tmp_path))
        cmd, cwd = client._build_effective_cmd(["codex", "--json"], "codex", None)
        assert cmd == ["codex", "--json"]
        assert cwd == str(tmp_path)

    def test_build_effective_cmd_uses_cwd_over_working_dir(self, renderer, tmp_path):
        """cwd explícito tem precedência sobre working_dir."""
        override = str(tmp_path / "sub")
        client = AgentClient(renderer, working_dir=str(tmp_path))
        _, effective_cwd = client._build_effective_cmd(["codex"], "codex", override)
        assert effective_cwd == override

"""Testes para TTL por turno e limpeza de sessão do shared_state."""

import unittest

from quimera.shared_state import (
    AGENT_STATE_KEYS,
    STATE_KEY_MAX_AGE_TURNS,
    bootstrap_state_key_stamps,
    stamp_state_keys,
    expire_stale_keys,
    build_prompt_state_payload,
    build_task_reference_payload,
)


class TestExpireStaleKeysRemovesOld(unittest.TestCase):
    """Verifica que keys não reafirmadas além do max_age são removidas."""

    def test_expire_stale_keys_removes_old(self):
        """Verifica que chaves não reafirmadas além do max_age são removidas."""
        current_turn = 20
        old_turn = current_turn - STATE_KEY_MAX_AGE_TURNS - 1  # expired
        state = {
            "goal_canonical": "finish feature X",
            "current_step": "step 3",
        }
        turn_stamps = {
            "goal_canonical": old_turn,
            "current_step": old_turn,
        }
        expired = expire_stale_keys(state, turn_stamps, current_turn)
        self.assertIn("goal_canonical", expired)
        self.assertIn("current_step", expired)
        self.assertNotIn("goal_canonical", state)
        self.assertNotIn("current_step", state)


class TestExpireStaleKeysKeepsRecent(unittest.TestCase):
    """Verifica que keys reafirmadas recentemente sobrevivem."""

    def test_expire_stale_keys_keeps_recent(self):
        """Verifica que chaves reafirmadas recentemente não são removidas."""
        current_turn = 20
        recent_turn = current_turn - 2  # well within max_age
        state = {
            "goal_canonical": "finish feature X",
            "current_step": "step 3",
        }
        turn_stamps = {
            "goal_canonical": recent_turn,
            "current_step": recent_turn,
        }
        expired = expire_stale_keys(state, turn_stamps, current_turn)
        self.assertEqual(expired, [])
        self.assertIn("goal_canonical", state)
        self.assertIn("current_step", state)


class TestStampStateKeysRecordsTurn(unittest.TestCase):
    """Verifica que stamp_state_keys registra o turno correto."""

    def test_stamp_state_keys_records_turn(self):
        """Verifica que stamp_state_keys registra o turno correto."""
        turn_stamps = {}
        stamp_state_keys(turn_stamps, {"goal_canonical"}, current_turn=5)
        self.assertEqual(turn_stamps["goal_canonical"], 5)

    def test_stamp_ignores_non_agent_keys(self):
        """Verifica que stamp_state_keys ignora chaves que não são de agente."""
        turn_stamps = {}
        stamp_state_keys(turn_stamps, {"task_overview"}, current_turn=5)
        self.assertNotIn("task_overview", turn_stamps)


class TestBootstrapStateKeyStamps(unittest.TestCase):
    """Verifica bootstrap de stamps para sessão restaurada."""

    def test_bootstrap_sets_stamps_for_existing_agent_keys(self):
        """Verifica que bootstrap define stamps para chaves de agente existentes."""
        shared_state = {"goal_canonical": "active", "current_step": "step 2", "task_overview": "sys"}
        turn_stamps = {}
        bootstrap_state_key_stamps(shared_state, turn_stamps, current_turn=7)

        self.assertEqual(turn_stamps["goal_canonical"], 7)
        self.assertEqual(turn_stamps["current_step"], 7)
        self.assertNotIn("task_overview", turn_stamps)


class TestSessionLoadClearsAgentKeys(unittest.TestCase):
    """Simula cenário sem history_restored e verifica limpeza de agent keys."""

    def test_clears_agent_keys_when_no_history(self):
        """Verifica que chaves de agente são limpas quando não há histórico restaurado."""
        # Simula shared_state carregado de sessão anterior
        shared_state = {
            "goal_canonical": "old goal",
            "current_step": "old step",
            "evidence": ["item1"],
            "task_overview": "system data",
            "working_dir": "/some/path",
        }
        history_restored = False

        # Reproduz a lógica de core.py
        if not history_restored:
            for key in AGENT_STATE_KEYS:
                shared_state.pop(key, None)

        # Agent keys removidas
        self.assertNotIn("goal_canonical", shared_state)
        self.assertNotIn("current_step", shared_state)
        self.assertNotIn("evidence", shared_state)

        # System keys preservadas
        self.assertIn("task_overview", shared_state)
        self.assertIn("working_dir", shared_state)

    def test_preserves_agent_keys_when_history_restored(self):
        """Verifica que chaves de agente são preservadas quando o histórico é restaurado."""
        shared_state = {
            "goal_canonical": "active goal",
            "current_step": "step 2",
            "task_overview": "system data",
        }
        history_restored = True

        if not history_restored:
            for key in AGENT_STATE_KEYS:
                shared_state.pop(key, None)

        # Agent keys preservadas
        self.assertIn("goal_canonical", shared_state)
        self.assertIn("current_step", shared_state)


class TestInternalKeysNotInPrompt(unittest.TestCase):
    """Verifica que _current_turn não aparece nos payloads de prompt."""

    def test_internal_keys_excluded_from_prompt_payload(self):
        """Verifica que chaves internas não aparecem no payload do prompt."""
        state = {
            "goal_canonical": "some goal",
            "task_overview": "overview",
            "working_dir": "/path",
            "_current_turn": 15,
        }
        prompt_json, completed = build_prompt_state_payload(state)
        self.assertNotIn("_current_turn", prompt_json)

    def test_internal_keys_excluded_from_task_reference_payload(self):
        """Verifica que chaves internas não aparecem no payload de referência de tarefa."""
        state = {
            "goal_canonical": "some goal",
            "task_overview": "overview",
            "_current_turn": 15,
        }
        payload = build_task_reference_payload(state)
        self.assertNotIn("_current_turn", payload)


if __name__ == "__main__":
    unittest.main()

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from quimera.storage import SessionStorage


class DummyRenderer:
    def __init__(self):
        self.system_messages = []

    def show_system(self, message):
        self.system_messages.append(message)


class SessionStorageTests(unittest.TestCase):
    def test_load_last_session_discards_shared_state_for_legacy_snapshot_without_saved_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            day_dir = logs_dir / "2026-04-17"
            day_dir.mkdir()
            snapshot = day_dir / "sessao-2026-04-17-101010.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "session_id": "sessao-2026-04-17-101010",
                        "messages": [{"role": "human", "content": "oi"}],
                        "shared_state": {"next_step": "stale"},
                    }
                ),
                encoding="utf-8",
            )

            storage = SessionStorage(logs_dir, DummyRenderer())

            restored = storage.load_last_session()

            self.assertEqual(restored["messages"], [{"role": "human", "content": "oi"}])
            self.assertEqual(restored["shared_state"], {})

    def test_load_last_session_discards_shared_state_when_saved_at_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            day_dir = logs_dir / "2026-04-17"
            day_dir.mkdir()
            snapshot = day_dir / "sessao-2026-04-17-111111.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "session_id": "sessao-2026-04-17-111111",
                        "saved_at": "not-a-date",
                        "messages": [{"role": "human", "content": "oi"}],
                        "shared_state": {"next_step": "stale"},
                    }
                ),
                encoding="utf-8",
            )

            storage = SessionStorage(logs_dir, DummyRenderer())

            restored = storage.load_last_session()

            self.assertEqual(restored["shared_state"], {})

    def test_load_last_session_keeps_recent_shared_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            day_dir = logs_dir / "2026-04-17"
            day_dir.mkdir()
            snapshot = day_dir / "sessao-2026-04-17-121212.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "session_id": "sessao-2026-04-17-121212",
                        "saved_at": (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),
                        "messages": [{"role": "human", "content": "oi"}],
                        "shared_state": {"next_step": "continuar"},
                    }
                ),
                encoding="utf-8",
            )

            storage = SessionStorage(logs_dir, DummyRenderer())

            restored = storage.load_last_session()

            self.assertEqual(restored["shared_state"], {"next_step": "continuar"})


if __name__ == "__main__":
    unittest.main()

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from quimera.storage import SessionStorage


class SessionStorageTests(unittest.TestCase):
    def test_load_last_session_defers_restore_notice_until_consumed(self):
        """Verifica que o aviso de restauro é adiado até ser consumido pelo caller."""
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            day_dir = logs_dir / date_str
            day_dir.mkdir()
            snapshot = day_dir / f"sessao-{now.strftime('%Y-%m-%d-%H%M%S')}.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "session_id": snapshot.stem,
                        "saved_at": (now - timedelta(minutes=10)).isoformat(timespec="seconds"),
                        "messages": [{"role": "human", "content": "oi"}],
                        "shared_state": {},
                    }
                ),
                encoding="utf-8",
            )

            storage = SessionStorage(logs_dir)

            restored = storage.load_last_session()

            self.assertEqual(restored["messages"], [{"role": "human", "content": "oi"}])
            notice = storage.pop_restore_notice()
            self.assertIn("[memória] histórico restaurado de", notice)
            self.assertIsNone(storage.pop_restore_notice())

    def test_load_last_session_discards_dict_snapshot_without_saved_at(self):
        """Verifica que snapshots sem saved_at são descartados."""
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            day_dir = logs_dir / date_str
            day_dir.mkdir()
            snapshot = day_dir / f"sessao-{now.strftime('%Y-%m-%d-%H%M%S')}.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "session_id": snapshot.stem,
                        "messages": [{"role": "human", "content": "oi"}],
                        "shared_state": {"next_step": "stale"},
                    }
                ),
                encoding="utf-8",
            )

            storage = SessionStorage(logs_dir)

            restored = storage.load_last_session()

            self.assertEqual(restored["messages"], [])
            self.assertEqual(restored["shared_state"], {})

    def test_load_last_session_discards_shared_state_when_saved_at_is_invalid(self):
        """Verifica que shared_state é descartado quando saved_at é inválido."""
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            day_dir = logs_dir / date_str
            day_dir.mkdir()
            snapshot = day_dir / f"sessao-{now.strftime('%Y-%m-%d-%H%M%S')}.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "session_id": snapshot.stem,
                        "saved_at": "not-a-date",
                        "messages": [{"role": "human", "content": "oi"}],
                        "shared_state": {"next_step": "stale"},
                    }
                ),
                encoding="utf-8",
            )

            storage = SessionStorage(logs_dir)

            restored = storage.load_last_session()

            self.assertEqual(restored["messages"], [])
            self.assertEqual(restored["shared_state"], {})

    def test_load_last_session_keeps_recent_shared_state(self):
        """Verifica que shared_state recente é preservado durante o restauro."""
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            day_dir = logs_dir / date_str
            day_dir.mkdir()
            snapshot = day_dir / f"sessao-{now.strftime('%Y-%m-%d-%H%M%S')}.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "session_id": snapshot.stem,
                        "saved_at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                        "messages": [{"role": "human", "content": "oi"}],
                        "shared_state": {"next_step": "continuar"},
                    }
                ),
                encoding="utf-8",
            )

            storage = SessionStorage(logs_dir)

            restored = storage.load_last_session()

            self.assertEqual(restored["shared_state"], {"next_step": "continuar"})

    def test_load_last_session_discards_old_history_by_ttl(self):
        """Verifica que histórico antigo além do TTL é descartado."""
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            day_dir = logs_dir / date_str
            day_dir.mkdir()
            snapshot = day_dir / f"sessao-{now.strftime('%Y-%m-%d-%H%M%S')}.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "session_id": snapshot.stem,
                        "saved_at": (now - timedelta(hours=120)).isoformat(timespec="seconds"),
                        "messages": [{"role": "human", "content": "não deveria restaurar"}],
                        "shared_state": {"next_step": "stale"},
                    }
                ),
                encoding="utf-8",
            )

            storage = SessionStorage(logs_dir)
            restored = storage.load_last_session()

            self.assertEqual(restored["messages"], [])
            self.assertEqual(restored["shared_state"], {})


if __name__ == "__main__":
    unittest.main()

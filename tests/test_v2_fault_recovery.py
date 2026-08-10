from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from etoro_agent.runtime_store_v2 import RuntimeStoreV2


class V2FaultRecoveryTests(unittest.TestCase):
    def test_audit_tamper_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            from etoro_agent.domain_v2 import DomainEvent
            now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
            store.append_event(DomainEvent("e1","Test",2,now,now,"idem-1","","corr",{"x":1}))
            self.assertTrue(store.verify_event_chain())
            store.db.execute("UPDATE v2_events SET payload_json='{}' WHERE event_id='e1'")
            store.db.commit()
            self.assertFalse(store.verify_event_chain())
            store.close()

    def test_decision_lease_is_recovered_after_worker_crash(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
            store.queue_decision("d1", "a"*64, {"action":"HOLD"}, expires_at=now+timedelta(minutes=10), created_at=now)
            first = store.claim_decision("worker-a", now=now, lease_seconds=30)
            self.assertIsNotNone(first)
            self.assertIsNone(store.claim_decision("worker-b", now=now+timedelta(seconds=10), lease_seconds=30))
            second = store.claim_decision("worker-b", now=now+timedelta(seconds=31), lease_seconds=30)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertNotEqual(first["claim_token"], second["claim_token"])
            store.close()

    def test_expired_decision_cannot_be_recovered_as_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
            store.queue_decision("d1", "b"*64, {"action":"OPEN"}, expires_at=now+timedelta(seconds=20), created_at=now)
            self.assertIsNone(store.claim_decision("worker", now=now+timedelta(seconds=21), lease_seconds=30))
            state = store.db.execute("SELECT state FROM v2_decisions WHERE decision_id='d1'").fetchone()[0]
            self.assertEqual(state, "EXPIRED")
            store.close()


if __name__ == "__main__":
    unittest.main()

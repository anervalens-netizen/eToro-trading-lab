from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from etoro_agent.ai_decision import AIDecisionStore
from etoro_agent.audit import AuditLog


class AIDecisionStoreTests(unittest.TestCase):
    def test_hash_bound_open_decision_is_one_time_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            store = AIDecisionStore(audit)
            expires = int(datetime.now(timezone.utc).timestamp()) + 600
            packet_id, packet_hash, created = store.queue(
                {"schema_version": 1, "candidates": [{"candidate_id": "c1"}]},
                expires,
            )
            self.assertTrue(created)
            with self.assertRaises(PermissionError):
                store.decide(packet_id, "0" * 64, "OPEN", "c1", Decimal("0.8"), ("trend",), "bounded review", "gpt-5.6-sol")
            store.decide(packet_id, packet_hash, "OPEN", "c1", Decimal("0.8"), ("trend",), "bounded review", "gpt-5.6-sol")
            ready = store.consume_ready()
            self.assertEqual(len(ready), 1)
            self.assertEqual((ready[0].action, ready[0].candidate_id), ("OPEN", "c1"))
            self.assertEqual(store.consume_ready(), ())
            self.assertTrue(audit.verify_chain())

    def test_open_requires_exact_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            store = AIDecisionStore(audit)
            expires = int(datetime.now(timezone.utc).timestamp()) + 600
            packet_id, packet_hash, _ = store.queue({"candidates": []}, expires)
            with self.assertRaises(ValueError):
                store.decide(packet_id, packet_hash, "OPEN", "", Decimal("0.8"), ("missing",), "no candidate", "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()

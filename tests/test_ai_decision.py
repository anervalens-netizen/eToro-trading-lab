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

    def test_direct_open_intent_is_persisted_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            store = AIDecisionStore(audit)
            expires = int(datetime.now(timezone.utc).timestamp()) + 600
            packet_id, packet_hash, _ = store.queue({"allowed_symbols": ["AAPL"]}, expires)
            direct = {
                "symbol": "AAPL", "side": "buy", "amount_usd": 250,
                "stop_loss_fraction": 0.04, "take_profit_fraction": 0.08,
                "max_holding_seconds": 21600,
            }
            store.decide(
                packet_id, packet_hash, "OPEN", "", Decimal("0.8"), ("direct",),
                "bounded direct intent", "gpt-5.6-sol", intent=direct,
            )
            decision = store.consume_ready()[0]
            self.assertEqual(decision.intent, direct)

    def test_policy_change_invalidates_pending_and_decided_packets(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            store = AIDecisionStore(audit)
            expires = int(datetime.now(timezone.utc).timestamp()) + 600
            first_id, first_hash, _ = store.queue(
                {"candidates": [{"candidate_id": "c1"}]}, expires
            )
            store.decide(
                first_id,
                first_hash,
                "OPEN",
                "c1",
                Decimal("0.8"),
                ("trend",),
                "bounded review",
                "gpt-5.6-sol",
            )
            store.queue({"candidates": []}, expires)
            self.assertEqual(store.invalidate_active("policy changed"), 2)
            self.assertEqual(store.pending(), ())
            self.assertEqual(store.consume_ready(), ())


if __name__ == "__main__":
    unittest.main()

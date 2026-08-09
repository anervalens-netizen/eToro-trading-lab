from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from etoro_agent.audit import AuditLog


class AuditTests(unittest.TestCase):
    def test_hash_chain_and_one_time_approval(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.append("decision", {"value": 1})
            audit.register_proposal("p1", {"account": "DEMO"})
            audit.approve_once("p1")
            audit.consume_approval("p1")
            self.assertTrue(audit.verify_chain())
            with self.assertRaises(PermissionError):
                audit.consume_approval("p1")

    def test_proposal_id_cannot_be_rebound_before_or_after_approval(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            first_hash = audit.register_proposal("p1", {"amount": "10"})
            self.assertEqual(
                audit.register_proposal("p1", {"amount": "10"}), first_hash
            )
            with self.assertRaisesRegex(ValueError, "immutable"):
                audit.register_proposal("p1", {"amount": "20"})
            audit.approve_once("p1", first_hash, "owner")
            with self.assertRaisesRegex(ValueError, "immutable"):
                audit.register_proposal("p1", {"amount": "30"})
            row = audit.proposal("p1")
            self.assertEqual(row["envelope_hash"], first_hash)
            self.assertEqual(row["state"], "APPROVED")


if __name__ == "__main__":
    unittest.main()

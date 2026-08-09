from __future__ import annotations

import tempfile
import threading
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

    def test_proposal_source_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            audit.register_proposal("p1", {}, source="sol_master_open")
            with self.assertRaisesRegex(ValueError, "immutable"):
                audit.register_proposal("p1", {}, source="manual")
            self.assertEqual(audit.proposal("p1")["source"], "sol_master_open")

    def test_preflight_failure_is_terminal_without_consuming_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            envelope_hash = audit.register_proposal("p-preflight", {"demo": True})
            audit.approve_once(
                "p-preflight", envelope_hash, "standing-demo-policy"
            )
            self.assertTrue(
                audit.reject_approved_before_send(
                    "p-preflight", "PermissionError"
                )
            )
            proposal = audit.proposal("p-preflight")
            assert proposal is not None
            self.assertEqual(proposal["state"], "REJECTED")
            self.assertIsNone(proposal["consumed_at"])
            self.assertFalse(
                audit.reject_approved_before_send(
                    "p-preflight", "RuntimeError"
                )
            )

    def test_concurrent_startup_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "audit.sqlite3"
            failures: list[Exception] = []

            def open_store() -> None:
                try:
                    AuditLog(database)
                except Exception as exc:  # pragma: no cover - asserted below
                    failures.append(exc)

            workers = [threading.Thread(target=open_store) for _ in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(failures, [])
            audit = AuditLog(database)
            columns = {
                str(row[1])
                for row in audit.db.execute("PRAGMA table_info(approvals)")
            }
            self.assertIn("source", columns)


if __name__ == "__main__":
    unittest.main()

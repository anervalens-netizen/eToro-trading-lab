from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from etoro_agent.audit_anchor_v2 import AuditAnchorWriter
from etoro_agent.dashboard_v2 import DashboardServiceV2, _health_payload
from etoro_agent.domain_v2 import DomainEvent
from etoro_agent.risk import generate_signing_keypair
from etoro_agent.runtime_store_v2 import RuntimeStoreV2


class V2DashboardHealthTests(unittest.TestCase):
    def test_present_execution_gate_requires_every_execution_worker_heartbeat(self) -> None:
        now = datetime.now(UTC)
        heartbeats = {
            service: ("healthy", now, {})
            for service in (
                "v2-market",
                "v2-coordinator",
                "v2-reconciliation",
                "v2-demo-executor",
                "v2-exit-manager",
            )
        }
        with patch("etoro_agent.dashboard_v2.execution_gate_present", return_value=True):
            health = _health_payload(
                trading_state="ACTIVE",
                heartbeats=heartbeats,
                oldest_outbox_at=None,
                oldest_unknown_at=None,
                oldest_reconciliation_at=None,
                dead_letters=0,
                chain_valid=True,
                anchor_at=now,
            )
        self.assertIn("stale_heartbeats:v2-decision-apply", health["failures"])

    def test_signed_checkpoint_incremental_health_is_bounded_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime.sqlite3"
            backup = root / "backups"
            anchors = root / "anchors"
            offhost = root / "LAST_OFFHOST_OK"
            backup.mkdir()
            anchors.mkdir()
            now = datetime.now(UTC)
            store = RuntimeStoreV2(runtime)
            for service in ("v2-market", "v2-coordinator", "v2-reconciliation"):
                store.heartbeat(
                    service,
                    "halted",
                    {"economic_drift": [], "real_money": False},
                    at=now,
                )
            store.append_event(
                DomainEvent(
                    "health-anchor-base",
                    "HealthTest",
                    4,
                    now,
                    now,
                    "health-anchor-base",
                    "",
                    "health",
                    {"safe": True},
                )
            )
            row = store.db.execute(
                "SELECT sequence,event_hash FROM v2_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence, head_hash = int(row[0]), str(row[1])

            private_key = root / "anchor.key"
            public_key = root / "anchor.pub"
            generate_signing_keypair(private_key, public_key)
            anchor = AuditAnchorWriter(private_key, anchors).anchor(head_hash, at=now)
            (anchors / "LATEST.json").write_text(
                json.dumps({**anchor.__dict__, "sequence": sequence}), encoding="utf-8"
            )
            (backup / "LAST_BACKUP_OK").touch()
            (backup / "LAST_RESTORE_DRILL_OK").touch()
            offhost.touch()

            store.append_event(
                DomainEvent(
                    "health-after-anchor",
                    "HealthTest",
                    4,
                    now,
                    now,
                    "health-after-anchor",
                    "",
                    "health",
                    {"safe": True},
                )
            )
            store.close()
            gate = root / "gate-absent"
            environment = {
                "ETORO_V2_ANCHOR_LATEST": str(anchors / "LATEST.json"),
                "ETORO_V2_ANCHOR_PUBLIC_KEY_FILE": str(public_key),
                "ETORO_V2_BACKUP_ROOT": str(backup),
                "ETORO_V2_OFFHOST_MARKER": str(offhost),
                "ETORO_V2_EXECUTION_GATE_FILE": str(gate),
            }
            service = DashboardServiceV2(runtime, "config/v2-demo.json")
            with patch.dict(os.environ, environment):
                health = service.health()
                self.assertEqual(health["status"], "locked")
                self.assertTrue(health["audit"]["incremental_chain_valid"])

                tampered = RuntimeStoreV2(runtime)
                tampered.db.execute(
                    "UPDATE v2_events SET canonical_body='{}' WHERE event_id='health-after-anchor'"
                )
                tampered.db.commit()
                tampered.close()
                health = service.health()
                self.assertEqual(health["status"], "error")
                self.assertIn("audit_chain_or_checkpoint_invalid", health["failures"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from etoro_agent.ai_queue_v2 import AIPacketQueueV2
from etoro_agent.ai_v2 import AIAction, DecisionPacketV2


class AIPacketQueueV2Tests(unittest.TestCase):
    def test_existing_queue_schema_migrates_to_dead_letter_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ai.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(
                """CREATE TABLE ai_packets_v2(
                  packet_id TEXT PRIMARY KEY,packet_hash TEXT NOT NULL UNIQUE,
                  packet_json TEXT NOT NULL,role TEXT NOT NULL,lane TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN ('PENDING','CLAIMED','DECIDED','ERROR','EXPIRED')),
                  created_at TEXT NOT NULL,expires_at TEXT NOT NULL,claimed_by TEXT,
                  claim_token TEXT,lease_expires_at TEXT,attempt_count INTEGER NOT NULL DEFAULT 0,
                  decision_json TEXT,model TEXT,prompt_hash TEXT,decided_at TEXT,updated_at TEXT NOT NULL
                );
                INSERT INTO ai_packets_v2 VALUES(
                  'old','hash','{}','role','lane','ERROR','2026-08-10T12:00:00+00:00',
                  '2026-08-10T13:00:00+00:00','stale-worker','stale-token',
                  '2026-08-10T12:30:00+00:00',2,NULL,NULL,NULL,NULL,
                  '2026-08-10T12:00:00+00:00'
                );
                CREATE TABLE ai_runs_v2(
                  run_id TEXT PRIMARY KEY,packet_id TEXT NOT NULL REFERENCES ai_packets_v2(packet_id),
                  role TEXT NOT NULL,lane TEXT NOT NULL,model TEXT NOT NULL,prompt_hash TEXT NOT NULL,
                  output_hash TEXT,status TEXT NOT NULL,input_tokens INTEGER,output_tokens INTEGER,
                  reasoning_tokens INTEGER,latency_ms INTEGER NOT NULL,error_type TEXT,created_at TEXT NOT NULL
                );
                CREATE TABLE ai_daily_budget_v2(
                  day TEXT NOT NULL,role TEXT NOT NULL,lane TEXT NOT NULL,claim_key TEXT NOT NULL,
                  created_at TEXT NOT NULL,PRIMARY KEY(day,role,lane,claim_key)
                );"""
            )
            db.close()
            queue = AIPacketQueueV2(path)
            columns = {str(row[1]) for row in queue.db.execute("PRAGMA table_info(ai_packets_v2)")}
            self.assertIn("dead_lettered_at", columns)
            self.assertEqual(
                tuple(
                    queue.db.execute(
                        """SELECT packet_id,attempt_count,claimed_by,claim_token,lease_expires_at
                           FROM ai_packets_v2"""
                    ).fetchone()
                ),
                ("old", 2, None, None, None),
            )

    def test_claim_and_submit_is_strict_and_leased(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            now = datetime(2026, 8, 10, 12, tzinfo=UTC)
            packet = DecisionPacketV2(
                "q1",
                now.isoformat(),
                (now + timedelta(minutes=5)).isoformat(),
                "C_sol_direct",
                "ENTRY_REVIEW",
                ("m1",),
                "f1",
                "b" * 64,
                "r" * 64,
                {},
                (),
                None,
                ("e1",),
            )
            queue = AIPacketQueueV2(Path(folder) / "ai.sqlite3")
            self.assertTrue(queue.queue(packet, "portfolio_decider_sol"))
            claim = queue.claim("worker-1", now=now, lease_seconds=30, daily_cap=3)
            self.assertIsNotNone(claim)
            assert claim is not None
            self.assertIsNone(
                queue.claim("worker-2", now=now + timedelta(seconds=10), lease_seconds=30)
            )
            decision = queue.submit(
                "q1",
                str(claim["claim_token"]),
                {
                    "action": "HOLD",
                    "self_reported_confidence": "0.6",
                    "self_reported_uncertainty": "0.4",
                    "reason_codes": ["no_edge"],
                    "rationale": "No sufficient edge",
                    "evidence_refs": ["e1"],
                    "hypothesis_id": "h1",
                    "lane_id": "C_sol_direct",
                },
                model="gpt-test",
                prompt_hash="p" * 64,
                run={
                    "run_id": "run1",
                    "status": "COMPLETED",
                    "latency_ms": 10,
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "reasoning_tokens": 0,
                    "error_type": None,
                },
                now=now + timedelta(seconds=1),
            )
            self.assertEqual(decision.action, AIAction.HOLD)

    def test_expired_lease_is_reclaimed_with_new_token(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            now = datetime(2026, 8, 10, 12, tzinfo=UTC)
            packet = DecisionPacketV2(
                "q2",
                now.isoformat(),
                (now + timedelta(minutes=5)).isoformat(),
                "B_sol_ranker_veto",
                "ENTRY_REVIEW",
                ("m1",),
                "f1",
                "b" * 64,
                "r" * 64,
                {},
                (),
                None,
                ("e1",),
            )
            queue = AIPacketQueueV2(Path(folder) / "ai.sqlite3")
            queue.queue(packet, "adversarial_critic")
            first = queue.claim("w1", now=now, lease_seconds=30)
            self.assertIsNotNone(first)
            second = queue.claim("w2", now=now + timedelta(seconds=31), lease_seconds=30)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertNotEqual(first["claim_token"], second["claim_token"])

    def test_poison_packet_is_dead_lettered_and_does_not_block_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            now = datetime(2026, 8, 10, 12, tzinfo=UTC)
            queue = AIPacketQueueV2(Path(folder) / "ai.sqlite3")
            for packet_id, offset in (("bad", 0), ("good", 1)):
                created = now + timedelta(seconds=offset)
                queue.queue(
                    DecisionPacketV2(
                        packet_id,
                        created.isoformat(),
                        (now + timedelta(minutes=30)).isoformat(),
                        "C_sol_direct",
                        "ENTRY_REVIEW",
                        ("m1",),
                        "f1",
                        "b" * 64,
                        "r" * 64,
                        {},
                        (),
                        None,
                        ("e1",),
                    ),
                    "portfolio_decider_sol",
                )

            for attempt in range(1, 4):
                claim = queue.claim("worker", now=now + timedelta(seconds=attempt), max_attempts=3)
                self.assertIsNotNone(claim)
                assert claim is not None
                self.assertEqual(claim["packet_id"], "bad")
                queue.fail(
                    "bad",
                    str(claim["claim_token"]),
                    retryable=True,
                    model="test",
                    prompt_hash="p" * 64,
                    run={
                        "run_id": f"failed-{attempt}",
                        "status": "ERROR",
                        "latency_ms": 1,
                        "input_tokens": 1,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "error_type": "InvalidOutput",
                    },
                    now=now + timedelta(seconds=attempt),
                    max_attempts=3,
                )

            state = queue.db.execute(
                "SELECT state,terminal_reason FROM ai_packets_v2 WHERE packet_id='bad'"
            ).fetchone()
            self.assertEqual(tuple(state), ("DEAD_LETTER", "inference_retry_exhausted"))
            next_claim = queue.claim("worker", now=now + timedelta(seconds=10), max_attempts=3)
            self.assertIsNotNone(next_claim)
            assert next_claim is not None
            self.assertEqual(next_claim["packet_id"], "good")


if __name__ == "__main__":
    unittest.main()

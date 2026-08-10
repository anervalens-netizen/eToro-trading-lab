from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from etoro_agent.ai_queue_v2 import AIPacketQueueV2
from etoro_agent.ai_v2 import AIAction, DecisionPacketV2


class AIPacketQueueV2Tests(unittest.TestCase):
    def test_claim_and_submit_is_strict_and_leased(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
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
                    "confidence": "0.6",
                    "uncertainty": "0.4",
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
            now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
            packet = DecisionPacketV2(
                "q2", now.isoformat(), (now + timedelta(minutes=5)).isoformat(),
                "B_sol_ranker_veto", "ENTRY_REVIEW", ("m1",), "f1", "b" * 64,
                "r" * 64, {}, (), None, ("e1",),
            )
            queue = AIPacketQueueV2(Path(folder) / "ai.sqlite3")
            queue.queue(packet, "adversarial_critic")
            first = queue.claim("w1", now=now, lease_seconds=30)
            self.assertIsNotNone(first)
            second = queue.claim("w2", now=now + timedelta(seconds=31), lease_seconds=30)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertNotEqual(first["claim_token"], second["claim_token"])


if __name__ == "__main__":
    unittest.main()

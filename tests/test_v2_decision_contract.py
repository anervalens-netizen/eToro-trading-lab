from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from etoro_agent.ai_v2 import AIAction, AIIntentOutputV2, DecisionPacketV2, Lane
from etoro_agent.decision_v2 import DecisionApplierV2
from etoro_agent.domain_v2 import QuoteProvenance


class V2DecisionContractTests(unittest.TestCase):
    def test_open_output_becomes_exact_bounded_intent(self) -> None:
        now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
        packet = DecisionPacketV2(
            "packet-1",
            now.isoformat(),
            (now + timedelta(minutes=5)).isoformat(),
            Lane.SOL_DIRECT.value,
            "ENTRY_REVIEW",
            ("market-1",),
            "feature-1",
            "b" * 64,
            "r" * 64,
            {},
            (),
            None,
            ("feature-1",),
        )
        output = AIIntentOutputV2(
            action=AIAction.OPEN,
            confidence=Decimal("0.75"),
            uncertainty=Decimal("0.25"),
            reason_codes=("edge_present",),
            rationale="fresh bounded setup",
            evidence_refs=("feature-1",),
            hypothesis_id="sol_direct_v2",
            lane_id=Lane.SOL_DIRECT.value,
            symbol="AAPL",
            side="buy",
            amount_usd=Decimal("100"),
            stop_loss_fraction=Decimal("0.02"),
            take_profit_fraction=Decimal("0.04"),
            max_holding_seconds=3600,
            max_slippage_bps=Decimal("25"),
            partial_close_fraction=None,
            invalidation_conditions=("feature regime breaks",),
        )
        quote = QuoteProvenance(
            "AAPL",
            Decimal("99.9"),
            Decimal("100"),
            now,
            now,
            "test",
            "1",
            "m" * 64,
            "b" * 64,
        )
        intent = DecisionApplierV2._intent(packet, output, quote, now=now)
        self.assertEqual(intent.symbol, "AAPL")
        self.assertEqual(intent.amount_usd, Decimal("100"))
        self.assertEqual(intent.raw_confidence, Decimal("0.75"))
        self.assertEqual(intent.confidence_threshold, Decimal("0"))
        self.assertEqual(intent.stop_loss_fraction, Decimal("0.02"))
        self.assertEqual(intent.snapshot_hash, "m" * 64)
        self.assertEqual(intent.correlation_id, "packet-1")
        self.assertIn("uncertainty=0.25", intent.rationale)
        self.assertEqual(intent.invalidation_conditions, ("feature regime breaks",))


if __name__ == "__main__":
    unittest.main()

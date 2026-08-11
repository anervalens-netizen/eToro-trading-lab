from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from etoro_agent.ai_v2 import AIAction, AIIntentOutputV2, DecisionPacketV2, Lane
from etoro_agent.decision_v2 import DecisionApplierV2
from etoro_agent.domain_v2 import QuoteProvenance


class V2DecisionContractTests(unittest.TestCase):
    def test_open_output_becomes_exact_bounded_intent(self) -> None:
        now = datetime(2026, 8, 10, 12, tzinfo=UTC)
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
            (
                {
                    "candidate_id": "candidate-1",
                    "strategy_id": "trend_breakout_multi_horizon",
                    "strategy_version": "trend-v2.1",
                    "symbol": "AAPL",
                    "side": "buy",
                    "raw_confidence": "0.68",
                    "threshold": "0.60",
                    "stop_loss_fraction": "0.02",
                    "take_profit_fraction": "0.04",
                    "max_holding_seconds": 3600,
                    "executable": True,
                    "execution_plan": {
                        "amount_usd": "100",
                        "max_slippage_bps": "25",
                    },
                },
            ),
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
            hypothesis_id="trend_breakout_multi_horizon",
            lane_id=Lane.SOL_DIRECT.value,
            partial_close_fraction=None,
            invalidation_conditions=("feature regime breaks",),
            candidate_id="candidate-1",
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
        self.assertEqual(intent.raw_confidence, Decimal("0.68"))
        self.assertEqual(intent.confidence_threshold, Decimal("0.60"))
        self.assertEqual(intent.stop_loss_fraction, Decimal("0.02"))
        self.assertEqual(intent.snapshot_hash, "m" * 64)
        self.assertEqual(intent.correlation_id, "packet-1")
        self.assertIn("uncertainty=0.25", intent.rationale)
        self.assertEqual(intent.invalidation_conditions, ("feature regime breaks",))
        retry = DecisionApplierV2._intent(packet, output, quote, now=now + timedelta(seconds=1))
        self.assertEqual(retry.intent_id, intent.intent_id)

        with self.assertRaisesRegex(ValueError, "lane attribution"):
            replace(output, lane_id=Lane.SOL_CRITIC.value).validate(packet)

        with self.assertRaisesRegex(ValueError, "deterministic candidate plan"):
            replace(output, amount_usd=Decimal("100")).validate(packet)

        position_packet = replace(
            packet,
            mode="POSITION_REVIEW",
            position={"position_id": "position-1", "symbol": "AAPL"},
        )
        with self.assertRaisesRegex(ValueError, "entry-review"):
            output.validate(position_packet)


if __name__ == "__main__":
    unittest.main()

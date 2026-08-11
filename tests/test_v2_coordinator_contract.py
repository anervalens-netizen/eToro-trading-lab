from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from etoro_agent.ai_v2 import AIAction, AIIntentOutputV2, Lane
from etoro_agent.coordinator_v2 import coordinator_cycle_allowed, validate_snapshot_batch
from etoro_agent.decision_apply_service_v2 import _shadow_effect
from etoro_agent.decision_v2 import DecisionPacketBuilderV2, DecisionPacketContextV2
from etoro_agent.domain_v2 import Side
from etoro_agent.features_v2 import build_feature_snapshot
from etoro_agent.market import CandleSnapshot, MarketSnapshot
from etoro_agent.strategy_v2 import FamilySignal, StrategyFamily

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


def snapshot(symbol: str, instrument_id: int, *, bar_offset: int = 0) -> MarketSnapshot:
    candle = CandleSnapshot(
        NOW - timedelta(minutes=15) + timedelta(minutes=bar_offset),
        Decimal("99"),
        Decimal("101"),
        Decimal("98"),
        Decimal("100"),
    )
    return MarketSnapshot(
        symbol,
        instrument_id,
        Decimal("99.9"),
        Decimal("100"),
        (Decimal("100"),),
        (candle,),
        "FifteenMinutes",
        NOW,
        market_open=True,
        quote_observed_at=NOW,
    )


class CoordinatorContractV2Tests(unittest.TestCase):
    def test_locked_state_runs_shadow_only_without_execution_gate(self) -> None:
        self.assertTrue(coordinator_cycle_allowed("LOCKED", execution_gate=False))
        self.assertFalse(coordinator_cycle_allowed("LOCKED", execution_gate=True))
        self.assertTrue(coordinator_cycle_allowed("ACTIVE", execution_gate=True))

    def test_batch_rejects_missing_symbol_and_correlated_bar_skew(self) -> None:
        expected = frozenset(("SPX500", "NSDQ100"))
        valid = {
            "SPX500": snapshot("SPX500", 27),
            "NSDQ100": snapshot("NSDQ100", 28),
        }
        self.assertEqual(validate_snapshot_batch(valid, expected), (True, "aligned"))
        self.assertEqual(
            validate_snapshot_batch({"SPX500": valid["SPX500"]}, expected)[1],
            "incomplete_symbol_batch",
        )
        misaligned = {**valid, "NSDQ100": snapshot("NSDQ100", 28, bar_offset=15)}
        self.assertEqual(
            validate_snapshot_batch(misaligned, expected)[1],
            "correlated_closed_bar_misaligned",
        )

    def test_open_can_only_select_a_deterministic_execution_plan(self) -> None:
        signal = FamilySignal(
            StrategyFamily.TREND_BREAKOUT,
            "trend-v2.1",
            "AAPL",
            Side.BUY,
            Decimal("0.70"),
            Decimal("0.60"),
            Decimal("0.02"),
            Decimal("0.04"),
            3600,
            "deterministic breakout",
            ("market-1",),
        )
        feature = build_feature_snapshot(
            "AAPL",
            NOW,
            {"return_1": Decimal("0.01")},
            ("market-1",),
            feature_version="test",
            data_quality_ok=True,
        )
        builder = DecisionPacketBuilderV2()
        packet = builder.build(
            lane=Lane.SOL_CRITIC,
            mode="ENTRY_REVIEW",
            feature=feature,
            market_snapshot_ids=("market-1",),
            signals=(signal,),
            context=DecisionPacketContextV2("b" * 64, "r" * 64, {}),
            position=None,
            created_at=NOW,
            execution_plans={
                builder.signal_key(signal): {
                    "amount_usd": "50",
                    "max_slippage_bps": "15",
                }
            },
        )
        candidate_id = str(packet.candidates[0]["candidate_id"])
        output = AIIntentOutputV2(
            AIAction.OPEN,
            Decimal("0.8"),
            Decimal("0.2"),
            ("selected",),
            "Selected supplied plan",
            ("market-1",),
            StrategyFamily.TREND_BREAKOUT.value,
            Lane.SOL_CRITIC.value,
            candidate_id=candidate_id,
        )
        output.validate(packet)
        effect = _shadow_effect(packet, output)
        self.assertEqual(effect["status"], "shadow_only")
        self.assertFalse(effect["broker_write"])
        self.assertFalse(effect["order_command_created"])
        with self.assertRaisesRegex(ValueError, "candidate"):
            AIIntentOutputV2(
                AIAction.OPEN,
                Decimal("0.8"),
                Decimal("0.2"),
                ("invented",),
                "Invented plan",
                ("market-1",),
                StrategyFamily.TREND_BREAKOUT.value,
                Lane.SOL_CRITIC.value,
                candidate_id="not-in-packet",
            ).validate(packet)


if __name__ == "__main__":
    unittest.main()

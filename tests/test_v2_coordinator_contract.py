from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from etoro_agent.ai_v2 import AIAction, AIIntentOutputV2, Lane
from etoro_agent.candidates_v2 import generate_core_signals
from etoro_agent.config_v2 import load_config_v2
from etoro_agent.coordinator_v2 import (
    AutonomousCoordinatorV2,
    coordinator_cycle_allowed,
    validate_snapshot_batch,
)
from etoro_agent.decision_apply_service_v2 import _shadow_effect
from etoro_agent.decision_v2 import DecisionPacketBuilderV2, DecisionPacketContextV2
from etoro_agent.domain_v2 import Side
from etoro_agent.execution_gate_v2 import authority_for_state
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
    def test_executable_statistical_baseline_reaches_coordinator_candidates(self) -> None:
        closes = tuple(Decimal("1") + Decimal(index) / Decimal("10000") for index in range(33))
        highs = tuple(value + Decimal("0.01") for value in closes)
        lows = tuple(value - Decimal("0.01") for value in closes)

        signals = generate_core_signals("EURUSD", closes, highs, lows)
        baseline = [
            signal for signal in signals if signal.family == StrategyFamily.STATISTICAL_BASELINE
        ]

        self.assertEqual(len(baseline), 1)
        self.assertEqual(baseline[0].symbol, "EURUSD")
        self.assertEqual(baseline[0].side, Side.BUY)
        self.assertTrue(baseline[0].actionable)
        self.assertEqual(baseline[0].stop_fraction, Decimal("0.02"))
        self.assertEqual(baseline[0].take_fraction, Decimal("0.04"))
        self.assertIn("lookback_return_bps=", baseline[0].rationale)

        coordinator = object.__new__(AutonomousCoordinatorV2)
        coordinator.config = load_config_v2("config/v2-demo-execution.json")
        coordinator.compatibility = coordinator.config.compatibility()
        coordinator.builder = DecisionPacketBuilderV2()
        coordinator.broker = Mock()
        coordinator.broker.cash_truth.return_value = SimpleNamespace(
            available_cash_usd=Decimal("1000"),
            snapshot_hash="b" * 64,
        )
        coordinator.broker.demo_pnl.return_value = SimpleNamespace(
            ok=True,
            body={"clientPortfolio": {"positions": []}},
        )
        coordinator.store = Mock()
        coordinator.store.positions.return_value = []
        coordinator.store.state_get.return_value = "ACTIVE"
        broker_hash, portfolio = coordinator._portfolio_context()
        plan = coordinator._execution_plan(baseline[0])
        self.assertIsNotNone(plan)
        assert plan is not None

        feature = build_feature_snapshot(
            "EURUSD",
            NOW,
            {"return_1": Decimal("0.001")},
            ("market-eurusd",),
            feature_version="test",
            data_quality_ok=True,
        )
        packet = coordinator.builder.build(
            lane=Lane.SOL_CRITIC,
            mode="ENTRY_REVIEW",
            feature=feature,
            market_snapshot_ids=("market-eurusd",),
            signals=(baseline[0],),
            context=DecisionPacketContextV2(broker_hash, "r" * 64, portfolio),
            position=None,
            created_at=NOW,
            execution_plans={coordinator.builder.signal_key(baseline[0]): plan},
        )
        self.assertEqual(len(packet.candidates), 1)
        candidate = packet.candidates[0]
        self.assertEqual(packet.model_context["packet_version"], "decision-packet-v2.2")
        self.assertTrue(candidate["executable"])
        self.assertEqual(candidate["strategy_id"], StrategyFamily.STATISTICAL_BASELINE.value)
        self.assertEqual(candidate["execution_plan"]["amount_usd"], "50")
        self.assertEqual(candidate["execution_plan"]["max_slippage_bps"], "15")
        self.assertEqual(candidate["evidence_refs"], ["market-eurusd"])
        proxy = candidate["execution_plan"]["tradability_proxy"]
        self.assertEqual(proxy["basis_points_definition"], "1_bp_equals_0.0001_return")
        self.assertIs(proxy["raw_score_is_calibrated_probability"], False)
        self.assertEqual(
            proxy["interpretation"],
            "heuristic_filter_not_expected_value_or_alpha_evidence",
        )
        self.assertGreater(
            Decimal(proxy["payoff_proxy_bps"]),
            Decimal(proxy["minimum_required_proxy_bps"]),
        )
        self.assertEqual(packet.model_context["portfolio"]["broker_available_cash_usd"], "1000")
        self.assertNotIn("initial_cash_usd", packet.model_context["portfolio"])
        risk_limits = packet.model_context["portfolio"]["risk_limits"]
        self.assertEqual(
            risk_limits["allowed_symbols"],
            sorted(coordinator.config.mandate.allowed_symbols),
        )
        self.assertEqual(
            risk_limits["max_order_usd"],
            str(coordinator.config.mandate.max_order_usd),
        )

    def test_locked_state_runs_shadow_only_without_execution_gate(self) -> None:
        self.assertTrue(coordinator_cycle_allowed("LOCKED", execution_gate=False))
        self.assertFalse(coordinator_cycle_allowed("LOCKED", execution_gate=True))
        self.assertTrue(coordinator_cycle_allowed("ACTIVE", execution_gate=True))
        self.assertFalse(coordinator_cycle_allowed("ACTIVE", execution_gate=False))
        self.assertFalse(coordinator_cycle_allowed("HALT_NEW", execution_gate=True))
        self.assertEqual(
            authority_for_state("LOCKED", 3, execution_gate=False),
            ("SHADOW", None),
        )
        self.assertEqual(
            authority_for_state("ACTIVE", 4, execution_gate=True),
            ("EXECUTION", 4),
        )
        self.assertIsNone(authority_for_state("LOCKED", 3, execution_gate=True))

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

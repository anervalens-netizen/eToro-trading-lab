from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from etoro_agent.compatibility_v2 import BrokerInstrumentRules, CompatibilityValidator, StrategyExecutionProfile
from etoro_agent.domain_v2 import (
    CompatibilityStatus,
    ExitReason,
    Fill,
    IntentEnvelope,
    OrderStatus,
    PositionState,
    QuoteProvenance,
    Side,
)
from etoro_agent.exits_v2 import BarObservation, ExitContext, ExitEvaluator
from etoro_agent.kernel_v2 import UnifiedTradingKernel
from etoro_agent.risk_v2 import BrokerTruth, CapitalMandate, GlobalRiskKernel
from etoro_agent.runtime_store_v2 import RuntimeStoreV2


NOW = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)


def mandate() -> CapitalMandate:
    return CapitalMandate(
        frozenset({"AAPL", "OIL"}),
        Decimal("1000"), Decimal("20"), Decimal("1000"), Decimal("1000"), 1,
        Decimal("25"), Decimal("40"), Decimal("50"), Decimal("0.06"), Decimal("0.10"),
        30, Decimal("50"), Decimal("50"), 0, 1,
    )


def quote(symbol: str = "AAPL", bid: str = "99.4", ask: str = "99.6", broker_hash: str = "broker") -> QuoteProvenance:
    return QuoteProvenance(
        symbol, Decimal(bid), Decimal(ask), NOW, NOW, "test", "q1", "market", broker_hash
    )


def intent(symbol: str = "AAPL", amount: str = "100", confidence: str = "0.7") -> IntentEnvelope:
    return IntentEnvelope(
        "intent-1", "master", "A", "trend", "2", symbol, Side.BUY,
        Decimal(amount), Decimal(confidence), Decimal("0.6"), Decimal("0.02"), Decimal("0.04"),
        3600, NOW, NOW, NOW + timedelta(minutes=10), Decimal("99"), Decimal("100"),
        Decimal("200"), Decimal("20"), "market", correlation_id="corr-1",
    )


def broker(hash_: str = "broker", available: str = "1000") -> BrokerTruth:
    return BrokerTruth(
        Decimal("1000"), Decimal("1000"), Decimal(available), Decimal("0"), Decimal("0"),
        0, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), hash_, NOW,
    )


class V2KernelTests(unittest.TestCase):
    def test_subthreshold_signal_cannot_be_promoted_by_flooring(self) -> None:
        with self.assertRaisesRegex(ValueError, "sub-threshold"):
            intent(confidence="0.31")

    def test_quote_provenance_is_mandatory_and_future_rejected(self) -> None:
        with self.assertRaises(ValueError):
            QuoteProvenance("AAPL", Decimal("99"), Decimal("100"), NOW, NOW, "", "q", "m", "b")
        with self.assertRaisesRegex(ValueError, "future"):
            QuoteProvenance(
                "AAPL", Decimal("99"), Decimal("100"), NOW + timedelta(seconds=10), NOW,
                "test", "q", "m", "b",
            )

    def test_exit_evaluator_gap_stop_and_stop_priority(self) -> None:
        position = PositionState(
            "p", "master", "trend", "A", "2", "i", "AAPL", Side.BUY, Decimal("1"),
            Decimal("100"), NOW, NOW, Decimal("95"), Decimal("105"), Decimal("0.05"),
            Decimal("0.05"), 3600, NOW + timedelta(hours=1),
        )
        gap = ExitEvaluator().evaluate(
            position,
            ExitContext(
                NOW + timedelta(minutes=15),
                quote(bid="89", ask="90"),
                BarObservation(NOW + timedelta(minutes=15), Decimal("90"), Decimal("106"), Decimal("89"), Decimal("100")),
            ),
        )
        self.assertEqual(gap.reason, ExitReason.GAP_STOP)
        both = ExitEvaluator().evaluate(
            position,
            ExitContext(
                NOW + timedelta(minutes=15),
                quote(bid="99", ask="100"),
                BarObservation(NOW + timedelta(minutes=15), Decimal("100"), Decimal("106"), Decimal("94"), Decimal("100")),
            ),
        )
        self.assertEqual(both.reason, ExitReason.STOP_LOSS)
        self.assertTrue(both.conservative_intrabar)

    def test_commodity_incompatibility_is_detected_before_preflight(self) -> None:
        validator = CompatibilityValidator(
            max_order_usd=Decimal("1000"),
            max_trade_risk_usd=Decimal("20"),
            max_gross_exposure_usd=Decimal("1000"),
        )
        profile = StrategyExecutionProfile(
            "oil_balanced", "OIL", Decimal("100"), Decimal("100"),
            Decimal("0.03"), Decimal("0.03"),
        )
        rules = BrokerInstrumentRules(
            "OIL", Decimal("1000"), None, Decimal("0.01"), Decimal("0.10")
        )
        result = validator.validate(profile, rules)
        self.assertEqual(result.status, CompatibilityStatus.INVALID)
        self.assertIn("amount_range_empty", result.reasons)

    def test_decision_claim_is_crash_safe_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            self.assertTrue(
                store.enqueue_decision(
                    "d1", "h" * 64, {"action": "HOLD"},
                    created_at=NOW, expires_at=NOW + timedelta(minutes=10),
                )
            )
            first = store.claim_decision("w1", now=NOW, lease_seconds=10)
            self.assertIsNotNone(first)
            self.assertIsNone(store.claim_decision("w2", now=NOW + timedelta(seconds=5), lease_seconds=10))
            reclaimed = store.claim_decision("w2", now=NOW + timedelta(seconds=11), lease_seconds=10)
            self.assertIsNotNone(reclaimed)
            self.assertNotEqual(first["claim_token"], reclaimed["claim_token"])
            store.close()

    def test_ack_is_not_fill_and_partial_fill_updates_position_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(mandate()))
            risk, command = kernel.submit_open_intent(intent(), quote(), broker(), now=NOW)
            self.assertTrue(risk.approved)
            assert command is not None
            kernel.begin_submit(command.order_command_id, NOW)
            ack = kernel.acknowledge(
                command.order_command_id, at=NOW, broker_order_id="bo-1", broker_position_id="bp-1"
            )
            self.assertEqual(ack.status, OrderStatus.ACKNOWLEDGED)
            self.assertEqual(store.positions(open_only=True), ())
            position = kernel.apply_fill(
                Fill(
                    "f1", command.order_command_id, command.client_order_id, "bo-1", "bp-1",
                    "AAPL", Side.BUY, Decimal("0.5"), Decimal("100"), Decimal("0.1"), Decimal("0"),
                    NOW, NOW, "fill-1",
                ),
                final=False,
            )
            self.assertEqual(position.quantity, Decimal("0.5"))
            self.assertEqual(store.broker_order(command.order_command_id).status, OrderStatus.PARTIALLY_FILLED)
            duplicate = kernel.apply_fill(
                Fill(
                    "f1", command.order_command_id, command.client_order_id, "bo-1", "bp-1",
                    "AAPL", Side.BUY, Decimal("0.5"), Decimal("100"), Decimal("0.1"), Decimal("0"),
                    NOW, NOW, "fill-1",
                ),
                final=False,
            )
            self.assertEqual(duplicate.quantity, Decimal("0.5"))
            self.assertTrue(store.verify_event_chain())
            store.close()

    def test_stale_intent_and_broker_hash_mismatch_fail_closed(self) -> None:
        risk = GlobalRiskKernel(mandate())
        stale = risk.evaluate_open(intent(), quote(), broker(), NOW + timedelta(minutes=11))
        self.assertFalse(stale.approved)
        self.assertIn("intent_expired_or_not_yet_valid", stale.reasons)
        mismatch = risk.evaluate_open(intent(), quote(broker_hash="x"), broker(hash_="y"), NOW)
        self.assertFalse(mismatch.approved)
        self.assertIn("broker_snapshot_hash_mismatch", mismatch.reasons)


if __name__ == "__main__":
    unittest.main()

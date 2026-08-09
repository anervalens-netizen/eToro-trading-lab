from __future__ import annotations

import dataclasses
import unittest
from decimal import Decimal

from etoro_agent.config import RiskLimits
from etoro_agent.models import RiskContext, Side, TradeIntent
from etoro_agent.risk import DEMO_ORDER_ROUTE, DeterministicRiskEngine


def limits() -> RiskLimits:
    return RiskLimits(frozenset({"BTC"}), Decimal("100"), Decimal("250"), Decimal("500"), 5, Decimal("50"), Decimal("0.05"), 1, Decimal("0.01"), Decimal("0.05"), 300)


def intent() -> TradeIntent:
    return TradeIntent("BTC", Side.BUY, Decimal("50"), Decimal("0.8"), "test", Decimal("0.03"), Decimal("0.06"), 1)


def context(**overrides: object) -> RiskContext:
    values = dict(equity_usd=Decimal("10000"), peak_equity_usd=Decimal("10000"), daily_pnl_usd=Decimal("0"), gross_exposure_usd=Decimal("0"), symbol_exposure_usd=Decimal("0"), trades_today=0, bid=Decimal("99"), ask=Decimal("100"), kill_switch_active=False)
    values.update(overrides)
    return RiskContext(**values)


class RiskTests(unittest.TestCase):
    def test_valid_intent_mints_demo_only_sealed_order(self) -> None:
        engine = DeterministicRiskEngine(limits(), b"x" * 32)
        result = engine.evaluate(intent(), context())
        self.assertTrue(result.approved)
        self.assertEqual(result.order.route, DEMO_ORDER_ROUTE)
        self.assertTrue(engine.verify(result.order))

    def test_limits_fail_closed(self) -> None:
        cases = [
            (dataclasses.replace(intent(), amount_usd=Decimal("101")), context(), "order_notional_limit"),
            (dataclasses.replace(intent(), leverage=2), context(), "leverage_limit"),
            (intent(), context(kill_switch_active=True), "kill_switch_active"),
            (intent(), context(daily_pnl_usd=Decimal("-50")), "daily_loss_limit"),
            (intent(), context(gross_exposure_usd=Decimal("460")), "gross_exposure_limit"),
            (intent(), context(trades_today=5), "trade_count_limit"),
        ]
        engine = DeterministicRiskEngine(limits(), b"x" * 32)
        for trade, state, reason in cases:
            with self.subTest(reason=reason):
                result = engine.evaluate(trade, state)
                self.assertFalse(result.approved)
                self.assertIn(reason, result.reasons)

    def test_tampering_cannot_bypass_risk_seal(self) -> None:
        engine = DeterministicRiskEngine(limits(), b"x" * 32)
        order = engine.evaluate(intent(), context()).order
        tampered = dataclasses.replace(order, route="/api/v2/trading/execution/orders")
        self.assertFalse(engine.verify(tampered))

    def test_executor_public_key_verifies_but_cannot_mint(self) -> None:
        engine = DeterministicRiskEngine(limits(), b"x" * 32)
        order = engine.evaluate(intent(), context()).order
        verifier = engine.verifier()
        self.assertTrue(verifier.verify(order))
        self.assertFalse(hasattr(verifier, "evaluate"))
        self.assertFalse(hasattr(verifier, "_private_key"))

    def test_operational_failures_and_monthly_cap_fail_closed(self) -> None:
        cases = [
            (context(data_quality_ok=False), "data_quality_failure"),
            (context(audit_writable=False), "audit_unavailable"),
            (context(reconciliation_ok=False), "reconciliation_drift"),
            (context(monthly_pnl_usd=Decimal("-50")), "monthly_loss_limit"),
            (context(open_positions=1), "open_position_limit"),
        ]
        engine = DeterministicRiskEngine(limits(), b"x" * 32)
        for state, reason in cases:
            with self.subTest(reason=reason):
                result = engine.evaluate(intent(), state)
                self.assertFalse(result.approved)
                self.assertIn(reason, result.reasons)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from etoro_agent.ablation_v2 import LaneMetricsV2, compare_lanes
from etoro_agent.commodity_v2 import (
    CommodityReleaseV2,
    CommodityResearchEngineV2,
    TermStructureSnapshotV2,
)
from etoro_agent.soak_v2 import SoakDayV2, evaluate_soak


class V2ResearchExtensionsTests(unittest.TestCase):
    def test_quantitative_commodity_event_and_carry_are_separate_hypotheses(self):
        now = datetime(2026, 8, 10, 12, tzinfo=UTC)
        release = CommodityReleaseV2(
            "eia-1",
            "OIL",
            "EIA_INVENTORY",
            "EIA",
            now,
            Decimal("110"),
            Decimal("100"),
            Decimal("98"),
            Decimal("5"),
        )
        signal = CommodityResearchEngineV2().event_signal(
            release, price_confirmation_return=Decimal("0.01")
        )
        self.assertIsNotNone(signal)
        carry = TermStructureSnapshotV2("OIL", Decimal("80"), Decimal("78"), 30, now)
        self.assertGreater(carry.annualized_carry_fraction, Decimal("0"))
        self.assertIsNotNone(CommodityResearchEngineV2().carry_signal(carry))

    def test_ablation_requires_value_after_model_cost_and_no_worse_drawdown(self):
        base = LaneMetricsV2(
            "A", 100, Decimal("20"), Decimal("0.08"), Decimal("1.2"), Decimal("0.2")
        )
        candidate = LaneMetricsV2(
            "D",
            100,
            Decimal("30"),
            Decimal("0.07"),
            Decimal("1.3"),
            Decimal("0.3"),
            20,
            Decimal("2"),
        )
        result = compare_lanes(base, candidate)
        self.assertTrue(result.improves_baseline)
        self.assertEqual(result.value_add_after_model_cost_usd, Decimal("8"))

    def test_soak_gate_is_operational_not_profit_only(self):
        from datetime import date, timedelta

        start = date(2026, 1, 1)
        days = [
            SoakDayV2(start + timedelta(days=i), 1, Decimal("1"), 0, 0, Decimal("0"), 0)
            for i in range(30)
        ]
        self.assertTrue(evaluate_soak(days).operational_gate_passed)
        broken = list(days)
        broken[-1] = SoakDayV2(broken[-1].day, 1, Decimal("100"), 0, 1, Decimal("0"), 0)
        self.assertFalse(evaluate_soak(broken).operational_gate_passed)


if __name__ == "__main__":
    unittest.main()

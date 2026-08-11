from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from etoro_agent.decision_apply_service_v2 import _period_loss_metrics


class V2PeriodPnLTests(unittest.TestCase):
    def test_period_gates_do_not_reuse_lifetime_pnl(self) -> None:
        now = datetime(2026, 8, 11, 12, tzinfo=UTC)
        events = (
            (now - timedelta(days=8), Decimal("100")),
            (now - timedelta(days=1), Decimal("50")),
            (now - timedelta(hours=1), Decimal("-25")),
        )
        daily, weekly, monthly = _period_loss_metrics(
            events,
            unrealized_usd=Decimal("0"),
            now=now,
        )
        self.assertEqual(daily, Decimal("-25"))
        self.assertEqual(weekly, Decimal("25"))
        self.assertEqual(monthly, Decimal("125"))

    def test_open_unrealized_losses_are_conservatively_applied_to_each_period(self) -> None:
        now = datetime(2026, 8, 11, 12, tzinfo=UTC)
        self.assertEqual(
            _period_loss_metrics((), unrealized_usd=Decimal("-5"), now=now),
            (Decimal("-5"), Decimal("-5"), Decimal("-5")),
        )
        self.assertEqual(
            _period_loss_metrics((), unrealized_usd=Decimal("5"), now=now),
            (Decimal("0"), Decimal("0"), Decimal("0")),
        )


if __name__ == "__main__":
    unittest.main()

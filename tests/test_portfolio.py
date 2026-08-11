from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from etoro_agent.audit import AuditLog
from etoro_agent.portfolio import SHADOW_PORTFOLIO_IDS, ShadowPortfolioLedger


class ShadowPortfolioTests(unittest.TestCase):
    def test_catalog_ledgers_start_with_independent_thousand_dollar_navs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            ledger = ShadowPortfolioLedger(audit)
            states = ledger.snapshot_all(as_of=datetime.now(UTC))
            self.assertEqual(tuple(state.portfolio_id for state in states), SHADOW_PORTFOLIO_IDS)
            self.assertEqual(len(states), 42)
            self.assertTrue(all(state.equity_usd == Decimal("1000") for state in states))

    def test_realized_unrealized_fees_financing_daily_pnl_and_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            ledger = ShadowPortfolioLedger(audit)
            as_of = datetime.now(UTC)

            ledger.record_fill(
                "strategy_01",
                "AAPL",
                "buy",
                Decimal("2"),
                Decimal("100"),
                fee_usd=Decimal("1"),
                executed_at=as_of,
            )
            open_state = ledger.snapshot("strategy_01", {"AAPL": Decimal("110")}, as_of=as_of)
            self.assertEqual(open_state.cash_usd, Decimal("799"))
            self.assertEqual(open_state.unrealized_pnl_usd, Decimal("20"))
            self.assertEqual(open_state.equity_usd, Decimal("1019"))
            self.assertEqual(open_state.daily_pnl_usd, Decimal("19"))

            ledger.accrue_financing("strategy_01", Decimal("0.5"))
            realized = ledger.record_fill(
                "strategy_01",
                "AAPL",
                "sell",
                Decimal("2"),
                Decimal("110"),
                fee_usd=Decimal("0.5"),
                executed_at=as_of,
            )
            closed = ledger.snapshot("strategy_01", as_of=as_of)
            untouched = ledger.snapshot("strategy_02", as_of=as_of)

            self.assertEqual(realized, Decimal("20"))
            self.assertEqual(closed.realized_pnl_usd, Decimal("20"))
            self.assertEqual(closed.unrealized_pnl_usd, Decimal("0"))
            self.assertEqual(closed.fees_usd, Decimal("1.5"))
            self.assertEqual(closed.financing_usd, Decimal("0.5"))
            self.assertEqual(closed.equity_usd, Decimal("1018.0"))
            self.assertEqual(closed.daily_pnl_usd, Decimal("18.0"))
            self.assertEqual(untouched.equity_usd, Decimal("1000"))
            self.assertEqual(untouched.trades_today, 0)

            reopened = ShadowPortfolioLedger(audit).snapshot("strategy_01", as_of=as_of)
            self.assertEqual(reopened.equity_usd, Decimal("1018.0"))
            self.assertEqual(reopened.realized_pnl_usd, Decimal("20"))

    def test_short_position_and_crossing_zero_use_correct_cost_basis(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            ledger = ShadowPortfolioLedger(audit)
            ledger.record_fill("strategy_03", "BTC", "sell", Decimal("2"), Decimal("100"))
            realized = ledger.record_fill("strategy_03", "BTC", "buy", Decimal("3"), Decimal("90"))
            state = ledger.snapshot("strategy_03", {"BTC": Decimal("95")})
            self.assertEqual(realized, Decimal("20"))
            self.assertEqual(state.unrealized_pnl_usd, Decimal("5"))
            self.assertEqual(state.equity_usd, Decimal("1025"))


if __name__ == "__main__":
    unittest.main()

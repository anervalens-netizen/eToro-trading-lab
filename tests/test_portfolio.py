from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from etoro_agent.audit import AuditLog
from etoro_agent.portfolio import (
    MASTER_PORTFOLIO_ID,
    SHADOW_PORTFOLIO_IDS,
    ShadowPortfolioLedger,
)


class ShadowPortfolioTests(unittest.TestCase):
    def test_catalog_ledgers_start_with_independent_thousand_dollar_navs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            ledger = ShadowPortfolioLedger(audit)
            states = ledger.snapshot_all(as_of=datetime.now(timezone.utc))
            self.assertEqual(tuple(state.portfolio_id for state in states), SHADOW_PORTFOLIO_IDS)
            self.assertEqual(len(states), 42)
            self.assertTrue(all(state.equity_usd == Decimal("1000") for state in states))

    def test_realized_unrealized_fees_financing_daily_pnl_and_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            ledger = ShadowPortfolioLedger(audit)
            as_of = datetime.now(timezone.utc)

            ledger.record_fill(
                "strategy_01", "AAPL", "buy", Decimal("2"), Decimal("100"), fee_usd=Decimal("1"), executed_at=as_of
            )
            open_state = ledger.snapshot("strategy_01", {"AAPL": Decimal("110")}, as_of=as_of)
            self.assertEqual(open_state.cash_usd, Decimal("799"))
            self.assertEqual(open_state.unrealized_pnl_usd, Decimal("20"))
            self.assertEqual(open_state.equity_usd, Decimal("1019"))
            self.assertEqual(open_state.daily_pnl_usd, Decimal("19"))

            ledger.accrue_financing("strategy_01", Decimal("0.5"))
            realized = ledger.record_fill(
                "strategy_01", "AAPL", "sell", Decimal("2"), Decimal("110"), fee_usd=Decimal("0.5"), executed_at=as_of
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

    def test_broker_history_close_reconciles_exact_net_profit_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            ledger = ShadowPortfolioLedger(
                audit, portfolio_ids=(MASTER_PORTFOLIO_ID,)
            )
            opened_at = datetime(2026, 8, 10, 13, 49, 45, tzinfo=timezone.utc)
            units = Decimal("1000") / Decimal("79.61")
            ledger.record_fill(
                MASTER_PORTFOLIO_ID,
                "OIL",
                "buy",
                units,
                Decimal("79.61"),
                executed_at=opened_at,
            )
            ledger.snapshot(
                MASTER_PORTFOLIO_ID,
                {"OIL": Decimal("90")},
                as_of=datetime(2026, 8, 11, 7, 44, tzinfo=timezone.utc),
            )
            audit.state_set("master_broker_position_id", "3577917785")
            audit.state_set("master_reconciliation_drift", '{"symbol":"OIL"}')
            trade = {
                "positionId": 3577917785,
                "instrumentId": 17,
                "isBuy": True,
                "openRate": 79.61,
                "openTimestamp": "2026-08-10T13:49:45.417Z",
                "closeRate": 82.76,
                "closeTimestamp": "2026-08-11T06:44:38.377Z",
                "netProfit": 39.57,
                "fees": 0.0,
                "units": 12.561236,
                "initialInvestment": 1000.0,
                "investment": 1000.0,
                "orderId": 372516753,
            }

            changed = ledger.reconcile_broker_close(
                MASTER_PORTFOLIO_ID, "OIL", 17, trade
            )
            state = ledger.snapshot(MASTER_PORTFOLIO_ID)

            self.assertTrue(changed)
            self.assertEqual(state.cash_usd, Decimal("1039.57"))
            self.assertEqual(state.equity_usd, Decimal("1039.57"))
            self.assertEqual(state.realized_pnl_usd, Decimal("39.57"))
            self.assertEqual(state.fees_usd, Decimal("0.0"))
            self.assertEqual(state.peak_equity_usd, Decimal("1039.57"))
            self.assertEqual(
                audit.db.execute(
                    "SELECT COUNT(*) FROM shadow_positions WHERE portfolio_id=?",
                    (MASTER_PORTFOLIO_ID,),
                ).fetchone()[0],
                0,
            )
            fill = audit.db.execute(
                """
                SELECT units,price,realized_pnl_usd FROM shadow_fills
                WHERE portfolio_id=? ORDER BY id DESC LIMIT 1
                """,
                (MASTER_PORTFOLIO_ID,),
            ).fetchone()
            self.assertEqual(Decimal(fill[0]), units)
            self.assertEqual(Decimal(fill[1]), Decimal("82.76"))
            self.assertEqual(Decimal(fill[2]), Decimal("39.57"))
            self.assertEqual(audit.state_get("master_broker_position_id", "missing"), "")
            self.assertEqual(audit.state_get("master_reconciliation_drift", "missing"), "")
            self.assertFalse(
                ledger.reconcile_broker_close(
                    MASTER_PORTFOLIO_ID, "OIL", 17, trade
                )
            )
            self.assertEqual(
                audit.db.execute(
                    "SELECT COUNT(*) FROM shadow_broker_close_reconciliations"
                ).fetchone()[0],
                1,
            )
            self.assertTrue(audit.verify_chain())

    def test_broker_history_mismatch_is_fail_closed_without_ledger_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            ledger = ShadowPortfolioLedger(
                audit, portfolio_ids=(MASTER_PORTFOLIO_ID,)
            )
            ledger.record_fill(
                MASTER_PORTFOLIO_ID,
                "OIL",
                "buy",
                Decimal("1"),
                Decimal("79.61"),
            )
            trade = {
                "positionId": 1,
                "instrumentId": 17,
                "isBuy": True,
                "openRate": 79.61,
                "openTimestamp": "2026-08-10T13:49:45Z",
                "closeRate": 82.76,
                "closeTimestamp": "2026-08-11T06:44:38Z",
                "netProfit": 39.57,
                "fees": 0,
                "units": 2,
                "initialInvestment": 79.61,
            }
            with self.assertRaisesRegex(ValueError, "units do not match"):
                ledger.reconcile_broker_close(
                    MASTER_PORTFOLIO_ID, "OIL", 17, trade
                )
            self.assertEqual(
                audit.db.execute(
                    "SELECT units FROM shadow_positions WHERE portfolio_id=?",
                    (MASTER_PORTFOLIO_ID,),
                ).fetchone()[0],
                "1",
            )
            self.assertEqual(
                audit.db.execute(
                    "SELECT COUNT(*) FROM shadow_broker_close_reconciliations"
                ).fetchone()[0],
                0,
            )
            self.assertTrue(audit.verify_chain())


if __name__ == "__main__":
    unittest.main()

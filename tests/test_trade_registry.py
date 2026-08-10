from __future__ import annotations

import sqlite3
import unittest
from decimal import Decimal

from etoro_agent.trade_registry import TradeRegistry, reconstruct_trades


def fill(
    fill_id: int,
    timestamp: str,
    side: str,
    units: str,
    price: str,
    fee: str,
    realized: str,
    *,
    portfolio_id: str = "strategy_01",
    symbol: str = "AAPL",
) -> dict[str, object]:
    return {
        "id": fill_id,
        "ts": timestamp,
        "portfolio_id": portfolio_id,
        "symbol": symbol,
        "side": side,
        "units": units,
        "price": price,
        "fee_usd": fee,
        "realized_pnl_usd": realized,
    }


class TradeReconstructionTests(unittest.TestCase):
    def test_scaled_long_and_partial_exits_reconcile_exactly(self) -> None:
        rows = [
            fill(1, "2026-08-10T10:00:00+00:00", "buy", "1", "100", "1", "0"),
            fill(2, "2026-08-10T10:05:00+00:00", "buy", "1", "120", "1", "0"),
            fill(3, "2026-08-10T10:10:00+00:00", "sell", "0.5", "130", "0.5", "10"),
            fill(4, "2026-08-10T10:20:00+00:00", "sell", "1.5", "100", "1.5", "-15"),
        ]

        trades = reconstruct_trades(rows)

        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual(trade.status, "closed")
        self.assertEqual(trade.side, "long")
        self.assertEqual(trade.entry_units, Decimal("2"))
        self.assertEqual(trade.exit_units, Decimal("2.0"))
        self.assertEqual(trade.entry_average_price, Decimal("110"))
        self.assertEqual(trade.exit_average_price, Decimal("107.5"))
        self.assertEqual(trade.gross_pnl_usd, Decimal("-5.0"))
        self.assertEqual(trade.fees_usd, Decimal("4.0"))
        self.assertEqual(trade.net_pnl_usd, Decimal("-9.0"))
        self.assertEqual(trade.realized_reconciliation_delta_usd, Decimal("0.0"))
        self.assertEqual(trade.duration_seconds, 1_200)
        self.assertEqual(len(trade.fills), 4)

    def test_short_cross_zero_splits_fill_fee_and_starts_stable_new_trade(self) -> None:
        rows = [
            fill(10, "2026-08-10T11:00:00Z", "sell", "2", "100", "2", "0"),
            fill(11, "2026-08-10T11:05:00Z", "buy", "3", "90", "3", "20"),
            fill(12, "2026-08-10T11:10:00Z", "sell", "1", "95", "1", "5"),
        ]

        first = reconstruct_trades(rows)
        second = reconstruct_trades(list(reversed(rows)))

        self.assertEqual([item.trade_id for item in first], [item.trade_id for item in second])
        self.assertEqual(len(first), 2)
        short, long = first
        self.assertEqual(short.side, "short")
        self.assertEqual(short.gross_pnl_usd, Decimal("20"))
        self.assertEqual(short.fees_usd, Decimal("4"))
        self.assertEqual(short.net_pnl_usd, Decimal("16"))
        self.assertEqual(short.fills[-1]["fee_usd"], "2")
        self.assertEqual(long.side, "long")
        self.assertEqual(long.entry_units, Decimal("1"))
        self.assertEqual(long.gross_pnl_usd, Decimal("5"))
        self.assertEqual(long.fees_usd, Decimal("2"))
        self.assertEqual(long.net_pnl_usd, Decimal("3"))
        self.assertEqual(long.fills[0]["fee_usd"], "1")

    def test_open_trade_exposes_partial_realization_without_fabricated_duration(self) -> None:
        rows = [
            fill(1, "2026-08-10T12:00:00+00:00", "buy", "3", "10", "0.3", "0"),
            fill(2, "2026-08-10T12:05:00+00:00", "sell", "1", "12", "0.1", "2"),
        ]

        trade = reconstruct_trades(rows)[0]

        self.assertEqual(trade.status, "open")
        self.assertEqual(trade.open_units, Decimal("2"))
        self.assertEqual(trade.current_average_price, Decimal("10"))
        self.assertEqual(trade.gross_pnl_usd, Decimal("2"))
        self.assertEqual(trade.net_pnl_usd, Decimal("1.6"))
        self.assertIsNone(trade.closed_at)
        self.assertIsNone(trade.duration_seconds)

    def test_registry_reads_existing_schema_without_writing(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE shadow_fills (
                id INTEGER PRIMARY KEY, ts TEXT NOT NULL, portfolio_id TEXT NOT NULL,
                symbol TEXT NOT NULL, side TEXT NOT NULL, units TEXT NOT NULL,
                price TEXT NOT NULL, fee_usd TEXT NOT NULL, realized_pnl_usd TEXT NOT NULL
            )
            """
        )
        row = fill(1, "2026-08-10T12:00:00+00:00", "buy", "1", "10", "0", "0")
        connection.execute(
            "INSERT INTO shadow_fills VALUES(?,?,?,?,?,?,?,?,?)", tuple(row.values())
        )
        before = connection.total_changes

        trades = TradeRegistry(connection).trades(portfolio_ids=("strategy_01",))

        self.assertEqual(len(trades), 1)
        self.assertEqual(connection.total_changes, before)


if __name__ == "__main__":
    unittest.main()

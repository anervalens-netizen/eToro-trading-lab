from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from etoro_agent.backtest_v2 import HistoricalBar, KernelBacktester
from etoro_agent.domain_v2 import IntentEnvelope, Side
from etoro_agent.risk_v2 import CapitalMandate


class V2BacktestTests(unittest.TestCase):
    def test_kernel_backtest_executes_next_bar_and_closes_through_exit_evaluator(self) -> None:
        start = datetime(2026, 8, 10, 12, tzinfo=UTC)
        bars = [
            HistoricalBar(
                start + timedelta(minutes=15 * i),
                Decimal(str(100 + i)),
                Decimal(str(101 + i)),
                Decimal(str(99 + i)),
                Decimal(str(100 + i)),
            )
            for i in range(8)
        ]
        mandate = CapitalMandate(
            frozenset({"AAPL"}),
            Decimal("500"),
            Decimal("20"),
            Decimal("1000"),
            Decimal("1000"),
            1,
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("0.20"),
            Decimal("0.30"),
            60,
            Decimal("100"),
            Decimal("500"),
        )

        def signal(index, history, bid, ask, snapshot_hash):
            if index != 1:
                return None
            now = history[-1].event_time
            return IntentEnvelope(
                "bt-intent",
                "master",
                "A",
                "baseline",
                "2",
                "AAPL",
                Side.BUY,
                Decimal("100"),
                Decimal("0.8"),
                Decimal("0.6"),
                Decimal("0.02"),
                Decimal("0.03"),
                3600,
                now,
                now,
                now + timedelta(hours=1),
                bid,
                ask,
                Decimal("500"),
                Decimal("50"),
                snapshot_hash,
                correlation_id="bt",
            )

        result = KernelBacktester(mandate, spread_bps=Decimal("5"), slippage_bps=Decimal("0")).run(
            "AAPL", bars, Decimal("1000"), signal
        )
        self.assertGreaterEqual(result.fills, 2)
        self.assertEqual(result.closed_positions, 1)
        self.assertTrue(result.event_chain_valid)
        self.assertGreater(result.event_count, 0)

    def test_signal_emitted_while_invested_is_not_replayed_after_exit(self) -> None:
        start = datetime(2026, 8, 10, 12, tzinfo=UTC)
        bars = [
            HistoricalBar(
                start + timedelta(minutes=15 * index),
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("100"),
            )
            for index in range(4)
        ]
        mandate = CapitalMandate(
            frozenset({"AAPL"}),
            Decimal("500"),
            Decimal("20"),
            Decimal("1000"),
            Decimal("1000"),
            1,
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("0.20"),
            Decimal("0.30"),
            60,
            Decimal("100"),
            Decimal("500"),
        )

        def signal(index, history, bid, ask, snapshot_hash):
            if index not in {0, 1}:
                return None
            now = history[-1].event_time
            return IntentEnvelope(
                f"bt-intent-{index}",
                "master",
                "A",
                "baseline",
                "2",
                "AAPL",
                Side.BUY,
                Decimal("100"),
                Decimal("0.8"),
                Decimal("0.6"),
                Decimal("0.02"),
                Decimal("0.03"),
                900,
                now,
                now,
                now + timedelta(hours=1),
                bid,
                ask,
                Decimal("500"),
                Decimal("50"),
                snapshot_hash,
                correlation_id=f"bt-{index}",
            )

        result = KernelBacktester(mandate, spread_bps=Decimal("5"), slippage_bps=Decimal("0")).run(
            "AAPL", bars, Decimal("1000"), signal
        )
        self.assertEqual(result.fills, 2)
        self.assertEqual(result.closed_positions, 1)


if __name__ == "__main__":
    unittest.main()

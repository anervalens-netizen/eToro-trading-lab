from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from etoro_agent.backtest_v2 import HistoricalBar
from etoro_agent.domain_v2 import IntentEnvelope, Side
from etoro_agent.parity_v2 import ParityHarnessV2
from etoro_agent.risk_v2 import CapitalMandate


class V2ParityTests(unittest.TestCase):
    def test_historical_and_shadow_match_on_same_recorded_bars(self) -> None:
        start = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
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
                "parity-intent",
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
                correlation_id="parity",
            )

        result = ParityHarnessV2(
            mandate,
            starting_equity=Decimal("1000"),
            slippage_bps=Decimal("0"),
        ).compare("AAPL", bars, signal)
        self.assertTrue(result.passed, result)


if __name__ == "__main__":
    unittest.main()

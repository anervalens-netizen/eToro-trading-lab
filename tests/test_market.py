from __future__ import annotations

import dataclasses
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from etoro_agent.data_quality import MarketDataQualityError, validate_candles
from etoro_agent.market import (
    INSTRUMENTS_BY_SYMBOL,
    CandleSnapshot,
    MarketDataCollector,
    MarketSnapshot,
    resolve_instrument,
)
from etoro_agent.mcp import MCPResult


class FakeMarketClient:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def execute_read(self, path: str, query: dict[str, str] | None = None) -> MCPResult:
        if path.endswith("/rates"):
            body = {"rates": [{"bid": "99.5", "ask": "100.5"}]}
        else:
            rows = []
            for offset, close in ((2, "99"), (1, "100")):
                timestamp = self.now - timedelta(hours=offset)
                rows.append(
                    {
                        "from": timestamp.isoformat().replace("+00:00", "Z"),
                        "open": close,
                        "high": str(Decimal(close) + 1),
                        "low": str(Decimal(close) - 1),
                        "close": close,
                        "volume": "10",
                    }
                )
            body = {"candles": [{"candles": rows}]}
        return MCPResult(200, True, body, None, {})


@dataclasses.dataclass(frozen=True)
class RawCandle:
    timestamp: datetime
    open: Decimal = Decimal("10")
    high: Decimal = Decimal("11")
    low: Decimal = Decimal("9")
    close: Decimal = Decimal("10")


class MarketTests(unittest.TestCase):
    def test_seven_instrument_catalog_is_exact_and_mismatch_fails_closed(self) -> None:
        self.assertEqual(
            {symbol: item.instrument_id for symbol, item in INSTRUMENTS_BY_SYMBOL.items()},
            {
                "EURUSD": 1,
                "SPX500": 27,
                "NSDQ100": 28,
                "AAPL": 1001,
                "TSLA": 1111,
                "BTC": 100000,
                "ETH": 100001,
            },
        )
        self.assertEqual(resolve_instrument("btc").instrument_id, 100000)
        with self.assertRaises(ValueError):
            resolve_instrument("BTC", 1)

    def test_collector_returns_immutable_versioned_quality_checked_snapshot(self) -> None:
        now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
        snapshot = MarketDataCollector(FakeMarketClient(now)).collect(
            "BTC", 100000, "OneHour", 2, now=now
        )
        self.assertEqual(snapshot.closes, (Decimal("99"), Decimal("100")))
        self.assertTrue(snapshot.quality and snapshot.quality.is_valid)
        self.assertEqual(snapshot.schema_version, 1)
        self.assertEqual(len(snapshot.content_hash), 64)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.bid = Decimal("1")  # type: ignore[misc]

        same = MarketSnapshot(
            snapshot.symbol,
            snapshot.instrument_id,
            snapshot.bid,
            snapshot.ask,
            snapshot.closes,
            snapshot.candles,
            snapshot.interval,
            snapshot.captured_at,
        )
        self.assertEqual(snapshot.content_hash, same.content_hash)

    def test_quality_reports_duplicate_gap_stale_and_non_utc(self) -> None:
        now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
        candles = (
            RawCandle(datetime(2026, 8, 9, 6)),
            RawCandle(datetime(2026, 8, 9, 6)),
            RawCandle(datetime(2026, 8, 9, 9)),
        )
        report = validate_candles(candles, "OneHour", now=now)
        codes = {issue.code for issue in report.issues}
        self.assertTrue(
            {"timestamp_not_utc", "duplicate_timestamp", "timestamps_not_ascending", "candle_gap", "stale_series"}.issubset(codes)
        )
        with self.assertRaises(MarketDataQualityError):
            report.require_valid()

    def test_candle_requires_timezone(self) -> None:
        with self.assertRaises(ValueError):
            CandleSnapshot(
                datetime(2026, 8, 9, 12),
                Decimal("1"),
                Decimal("1"),
                Decimal("1"),
                Decimal("1"),
            )


if __name__ == "__main__":
    unittest.main()

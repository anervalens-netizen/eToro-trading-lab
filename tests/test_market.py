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
    _session_adjusted_report,
    market_is_open,
    resolve_instrument,
)
from etoro_agent.mcp import MCPResult


class FakeMarketClient:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def execute_read(self, path: str, query: dict[str, str] | None = None) -> MCPResult:
        if path.endswith("/rates"):
            body = {
                "rates": [
                    {
                        "bid": "99.5",
                        "ask": "100.5",
                        "date": self.now.isoformat().replace("+00:00", "Z"),
                    }
                ]
            }
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
    def test_etoro_from_date_timestamp_is_supported(self) -> None:
        from etoro_agent.market import _parse_candle

        candle = _parse_candle(
            {
                "fromDate": "2026-08-07T20:00:00Z",
                "open": 1.1,
                "high": 1.2,
                "low": 1.0,
                "close": 1.15,
                "volume": None,
            }
        )
        self.assertEqual(candle.timestamp.tzinfo, timezone.utc)

    def test_market_session_gate_is_conservative(self) -> None:
        sunday = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
        monday_us_open = datetime(2026, 8, 10, 15, tzinfo=timezone.utc)
        self.assertTrue(market_is_open(INSTRUMENTS_BY_SYMBOL["BTC"], sunday))
        self.assertFalse(market_is_open(INSTRUMENTS_BY_SYMBOL["EURUSD"], sunday))
        self.assertFalse(market_is_open(INSTRUMENTS_BY_SYMBOL["AAPL"], sunday))
        self.assertFalse(
            market_is_open(
                INSTRUMENTS_BY_SYMBOL["SPX500"],
                datetime(2026, 8, 9, 21, 59, tzinfo=timezone.utc),
            )
        )
        self.assertTrue(
            market_is_open(
                INSTRUMENTS_BY_SYMBOL["SPX500"],
                datetime(2026, 8, 9, 22, 0, tzinfo=timezone.utc),
            )
        )
        self.assertFalse(
            market_is_open(
                INSTRUMENTS_BY_SYMBOL["NSDQ100"],
                datetime(2026, 8, 10, 21, 30, tzinfo=timezone.utc),
            )
        )
        self.assertTrue(
            market_is_open(INSTRUMENTS_BY_SYMBOL["AAPL"], monday_us_open)
        )

    def test_index_daily_maintenance_gap_is_expected(self) -> None:
        candles = (
            CandleSnapshot(
                datetime(2026, 8, 5, 20, 45, tzinfo=timezone.utc),
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("100"),
            ),
            CandleSnapshot(
                datetime(2026, 8, 5, 22, 0, tzinfo=timezone.utc),
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("100"),
            ),
        )
        raw = validate_candles(
            candles,
            "FifteenMinutes",
            now=datetime(2026, 8, 5, 22, 15, tzinfo=timezone.utc),
        )
        adjusted = _session_adjusted_report(
            raw, candles, INSTRUMENTS_BY_SYMBOL["SPX500"], True
        )
        self.assertTrue(adjusted.is_valid)
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
        now = datetime(2026, 8, 9, 12, 1, tzinfo=timezone.utc)
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
        now = datetime(2026, 8, 9, 12, 1, tzinfo=timezone.utc)
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

    def test_collector_excludes_forming_candle_and_keeps_requested_closed_count(self) -> None:
        now = datetime(2026, 8, 10, 12, 10, tzinfo=timezone.utc)

        class FormingClient(FakeMarketClient):
            requested_path = ""

            def execute_read(self, path, query=None):
                if path.endswith("/rates"):
                    return super().execute_read(path, query)
                self.requested_path = path
                rows = []
                for timestamp, close in (
                    (datetime(2026, 8, 10, 11, 30, tzinfo=timezone.utc), "99"),
                    (datetime(2026, 8, 10, 11, 45, tzinfo=timezone.utc), "100"),
                    (datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc), "101"),
                ):
                    rows.append(
                        {
                            "fromDate": timestamp.isoformat().replace("+00:00", "Z"),
                            "open": close,
                            "high": close,
                            "low": close,
                            "close": close,
                        }
                    )
                return MCPResult(
                    200, True, {"candles": [{"candles": rows}]}, None, {}
                )

        client = FormingClient(now)
        snapshot = MarketDataCollector(client).collect(
            "BTC",
            100000,
            "FifteenMinutes",
            2,
            now=now,
            close_grace_seconds=60,
        )
        self.assertTrue(client.requested_path.endswith("/3"))
        self.assertEqual(snapshot.closes, (Decimal("99"), Decimal("100")))
        self.assertEqual(
            snapshot.candles[-1].timestamp,
            datetime(2026, 8, 10, 11, 45, tzinfo=timezone.utc),
        )

    def test_quality_rejects_a_still_forming_candle(self) -> None:
        now = datetime(2026, 8, 10, 12, 10, tzinfo=timezone.utc)
        report = validate_candles(
            (RawCandle(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)),),
            "FifteenMinutes",
            now=now,
        )
        self.assertIn("candle_not_closed", {issue.code for issue in report.issues})

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

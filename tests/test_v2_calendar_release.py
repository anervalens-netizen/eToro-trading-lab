from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from etoro_agent.calendar_v2 import load_market_calendar_release
from etoro_agent.data_quality_v2 import DataQualityIssue, DataQualityReport
from etoro_agent.market_data_v2 import CandleSnapshot, InstrumentSpec, _session_adjusted_report


class CalendarReleaseV2Tests(unittest.TestCase):
    def test_deployed_calendar_handles_dst_break_weekend_expiry_and_unknown(self) -> None:
        calendar = load_market_calendar_release("config/market-calendar-v2.json")
        self.assertTrue(calendar.is_open("AAPL", datetime(2026, 8, 12, 15, tzinfo=UTC)))
        self.assertFalse(calendar.is_open("AAPL", datetime(2026, 8, 12, 13, tzinfo=UTC)))
        self.assertFalse(calendar.is_open("EURUSD", datetime(2026, 8, 15, 12, tzinfo=UTC)))
        self.assertFalse(calendar.is_open("UNKNOWN", datetime(2026, 8, 12, 12, tzinfo=UTC)))
        self.assertFalse(calendar.is_open("BTC", datetime(2026, 8, 21, 12, tzinfo=UTC)))

    def test_exception_closure_and_unknown_fields_are_fail_closed(self) -> None:
        value = json.loads(Path("config/market-calendar-v2.json").read_text(encoding="utf-8"))
        value["sessions"]["AAPL"]["exceptions"] = {"2026-08-12": []}
        with TemporaryDirectory() as folder:
            path = Path(folder) / "calendar.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            calendar = load_market_calendar_release(path)
            self.assertFalse(calendar.is_open("AAPL", datetime(2026, 8, 12, 15, tzinfo=UTC)))
            value["unexpected"] = True
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown"):
                load_market_calendar_release(path)

    def test_calendar_validity_must_cover_fetch_and_stay_bounded(self) -> None:
        value = json.loads(Path("config/market-calendar-v2.json").read_text(encoding="utf-8"))
        with TemporaryDirectory() as folder:
            path = Path(folder) / "calendar.json"
            value["valid_from"] = "2026-07-20T00:00:00Z"
            value["valid_until"] = "2026-08-20T00:00:00Z"
            path.write_text(json.dumps(value), encoding="utf-8")
            load_market_calendar_release(path)
            value["valid_until"] = "2026-08-20T00:00:01Z"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "validity interval"):
                load_market_calendar_release(path)
            for valid_from, valid_until in (
                ("2026-08-13T00:00:00Z", "2026-08-20T00:00:00Z"),
                ("2026-07-01T00:00:00Z", "2026-08-20T00:00:00Z"),
            ):
                value["valid_from"] = valid_from
                value["valid_until"] = valid_until
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "validity interval"):
                    load_market_calendar_release(path)

    def test_only_deployed_calendar_closures_explain_candle_gaps(self) -> None:
        calendar = load_market_calendar_release("config/market-calendar-v2.json")
        self.assertLess(calendar.valid_from, calendar.fetched_at)
        hour = timedelta(hours=1)
        self.assertTrue(
            calendar.explains_candle_gap(
                "SPX500",
                datetime(2026, 8, 5, 20, 45, tzinfo=UTC),
                datetime(2026, 8, 5, 22, tzinfo=UTC),
                timedelta(minutes=15),
            )
        )
        self.assertTrue(
            calendar.explains_candle_gap(
                "AAPL",
                datetime(2026, 8, 14, 19, tzinfo=UTC),
                datetime(2026, 8, 17, 14, tzinfo=UTC),
                hour,
            )
        )
        self.assertFalse(
            calendar.explains_candle_gap(
                "AAPL",
                datetime(2026, 8, 17, 19, tzinfo=UTC),
                datetime(2026, 8, 19, 14, tzinfo=UTC),
                hour,
            )
        )
        self.assertTrue(
            calendar.explains_candle_gap(
                "AAPL",
                datetime(2026, 8, 14, 12, tzinfo=UTC),
                datetime(2026, 8, 17, 12, tzinfo=UTC),
                timedelta(days=1),
            )
        )
        self.assertFalse(
            calendar.explains_candle_gap(
                "AAPL",
                datetime(2026, 8, 17, 12, tzinfo=UTC),
                datetime(2026, 8, 19, 12, tzinfo=UTC),
                timedelta(days=1),
            )
        )

    def test_market_quality_keeps_unscheduled_weekday_outage(self) -> None:
        instrument = InstrumentSpec("AAPL", 1001, "equity")
        issue = DataQualityIssue("candle_gap", "gap_seconds=154800", 1)
        report = DataQualityReport(
            datetime(2026, 8, 19, 15, tzinfo=UTC),
            "OneHour",
            2,
            3600,
            0,
            (issue,),
        )

        def candle(at: datetime) -> CandleSnapshot:
            return CandleSnapshot(
                at,
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("100"),
            )

        weekday_gap = _session_adjusted_report(
            report,
            (
                candle(datetime(2026, 8, 17, 19, tzinfo=UTC)),
                candle(datetime(2026, 8, 19, 14, tzinfo=UTC)),
            ),
            instrument,
            True,
        )
        weekend_gap = _session_adjusted_report(
            report,
            (
                candle(datetime(2026, 8, 14, 19, tzinfo=UTC)),
                candle(datetime(2026, 8, 17, 14, tzinfo=UTC)),
            ),
            instrument,
            True,
        )
        self.assertEqual(weekday_gap.issues, (issue,))
        self.assertEqual(weekend_gap.issues, ())


if __name__ == "__main__":
    unittest.main()

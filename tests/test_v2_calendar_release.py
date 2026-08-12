from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from etoro_agent.calendar_v2 import load_market_calendar_release


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


if __name__ == "__main__":
    unittest.main()

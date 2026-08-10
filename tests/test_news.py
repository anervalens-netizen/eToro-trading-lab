from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from etoro_agent.audit import AuditLog
from etoro_agent.news import (
    CommodityNewsScanner,
    CommodityNewsStore,
    NewsSource,
    classify_headline,
)


class CommodityNewsTests(unittest.TestCase):
    def test_classifier_separates_oil_and_gas_catalysts(self) -> None:
        source = NewsSource("wire", "Test", "https://example.test/news")
        oil = classify_headline(source, "Crude oil production cut after pipeline disruption")
        gas = classify_headline(source, "Natural gas storage build follows mild weather")
        self.assertEqual(oil and oil["symbols"], ["OIL"])
        self.assertEqual(oil and oil["direction_hint"], "bullish")
        self.assertEqual(gas and gas["symbols"], ["NATGAS"])
        self.assertEqual(gas and gas["direction_hint"], "bearish")
        self.assertIsNone(classify_headline(source, "Ordinary unrelated corporate update"))

    def test_first_scan_bootstraps_and_only_new_relevant_headline_creates_event(self) -> None:
        source = NewsSource("opec-test", "OPEC", "https://example.test/news", "OIL")
        pages = [
            '<html><a href="/old">Oil production remains stable</a></html>',
            '<html><a href="/old">Oil production remains stable</a>'
            '<a href="/new">OPEC production cut after supply disruption</a></html>',
        ]

        def fetcher(_: str) -> str:
            return pages.pop(0)

        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            scanner = CommodityNewsScanner(audit, sources=(source,), fetcher=fetcher)
            now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
            self.assertEqual(scanner.scan_once(now)["new_events"], 0)
            self.assertEqual(scanner.scan_once(now + timedelta(minutes=2))["new_events"], 1)
            events = CommodityNewsStore(audit).active_events(now + timedelta(minutes=3))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["symbols"], ["OIL"])
            self.assertNotIn("credential", str(events[0]).lower())

    def test_active_event_expires_after_six_hours(self) -> None:
        source = NewsSource("test", "Test", "https://example.test/news", "NATGAS")
        with tempfile.TemporaryDirectory() as folder:
            audit = AuditLog(Path(folder) / "audit.sqlite3")
            store = CommodityNewsStore(audit)
            now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
            classification = classify_headline(
                source, "Natural gas storage withdrawal after pipeline disruption"
            )
            assert classification is not None
            store.append_event(source, "Natural gas storage withdrawal after pipeline disruption", source.url, classification, now)
            self.assertEqual(len(store.active_events(now + timedelta(hours=5))), 1)
            self.assertEqual(store.active_events(now + timedelta(hours=7)), ())


if __name__ == "__main__":
    unittest.main()

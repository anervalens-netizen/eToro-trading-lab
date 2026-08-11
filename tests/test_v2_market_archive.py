from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from etoro_agent.market_worker_v2 import MarketArchiveIndexV2
from etoro_agent.ws_market_v2 import WebSocketEvent


class V2MarketArchiveTests(unittest.TestCase):
    def test_repeated_identical_receipts_remain_distinct_observations(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            index = MarketArchiveIndexV2(Path(folder) / "market.sqlite3")
            now = datetime(2026, 8, 11, 12, tzinfo=UTC)
            event = WebSocketEvent("rates", {"bid": 1}, now, now, "a" * 64, 7, False)
            index.record(event, "sha256/aa/payload.json")
            index.record(event, "sha256/aa/payload.json")

            rows = index.db.execute(
                "SELECT event_id,raw_hash FROM market_archive_v2 ORDER BY rowid"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0][0], rows[1][0])
            self.assertEqual({row[1] for row in rows}, {"a" * 64})
            index.db.close()

    def test_legacy_hash_primary_key_is_migrated_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market.sqlite3"
            db = sqlite3.connect(path)
            db.execute(
                """CREATE TABLE market_archive_v2(
                   raw_hash TEXT PRIMARY KEY, artifact_path TEXT NOT NULL, topic TEXT NOT NULL,
                   event_time TEXT NOT NULL, received_at TEXT NOT NULL, sequence INTEGER,
                   gap_detected INTEGER NOT NULL, indexed_at TEXT NOT NULL)"""
            )
            db.execute(
                "INSERT INTO market_archive_v2 VALUES(?,?,?,?,?,?,?,?)",
                ("b" * 64, "artifact", "rates", "t", "r", 1, 0, "i"),
            )
            db.commit()
            db.close()

            index = MarketArchiveIndexV2(path)
            self.assertEqual(
                index.db.execute("SELECT COUNT(*) FROM market_archive_v2").fetchone()[0], 1
            )
            columns = index.db.execute("PRAGMA table_info(market_archive_v2)").fetchall()
            self.assertEqual(columns[0][1], "event_id")
            index.db.close()


if __name__ == "__main__":
    unittest.main()

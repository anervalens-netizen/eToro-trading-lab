from __future__ import annotations

import hashlib
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
            state = index.db.execute(
                "SELECT connection_epoch,snapshot_complete,eligible_for_decision "
                "FROM market_archive_v2_eligibility LIMIT 1"
            ).fetchone()
            self.assertEqual(state, ("", 0, 0))
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
            self.assertEqual(len(columns), 9)
            index.db.close()

    def test_expanded_candidate_rolls_back_and_forward_without_observation_loss(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "market.sqlite3"
            raw = b'{"instrument":"SOL","price":"145.20"}'
            raw_path = root / "raw.json"
            raw_path.write_bytes(raw)
            raw_hash = hashlib.sha256(raw).hexdigest()
            db = sqlite3.connect(path)
            db.execute(
                """CREATE TABLE market_archive_v2(
                   event_id TEXT PRIMARY KEY,raw_hash TEXT NOT NULL,
                   artifact_path TEXT NOT NULL,topic TEXT NOT NULL,
                   event_time TEXT NOT NULL,received_at TEXT NOT NULL,sequence INTEGER,
                   gap_detected INTEGER NOT NULL,indexed_at TEXT NOT NULL,
                   connection_epoch TEXT NOT NULL DEFAULT '',
                   snapshot_complete INTEGER NOT NULL DEFAULT 0,
                   eligible_for_decision INTEGER NOT NULL DEFAULT 0)"""
            )
            db.execute(
                "INSERT INTO market_archive_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "candidate-before",
                    raw_hash,
                    str(raw_path),
                    "rates",
                    "event",
                    "received",
                    1,
                    0,
                    "indexed",
                    "epoch-before",
                    1,
                    1,
                ),
            )
            db.commit()
            db.close()

            candidate = MarketArchiveIndexV2(path)
            candidate.db.close()

            # Exact v0.5.15 compatibility boundary: positional nine-value INSERT.
            rollback = sqlite3.connect(path)
            rollback.execute(
                "INSERT INTO market_archive_v2 VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "rollback-middle",
                    raw_hash,
                    str(raw_path),
                    "rates",
                    "event-2",
                    "received-2",
                    2,
                    0,
                    "indexed-2",
                ),
            )
            rollback.commit()
            rollback.close()

            candidate = MarketArchiveIndexV2(path)
            now = datetime(2026, 8, 12, 20, tzinfo=UTC)
            candidate.record(
                WebSocketEvent(
                    "rates",
                    {"price": "145.30"},
                    now,
                    now,
                    raw_hash,
                    3,
                    False,
                    connection_epoch="epoch-after",
                    snapshot_complete=True,
                    eligible_for_decision=True,
                ),
                str(raw_path),
            )
            self.assertEqual(candidate.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                candidate.db.execute("SELECT COUNT(*) FROM market_archive_v2").fetchone()[0], 3
            )
            self.assertEqual(
                candidate.db.execute(
                    "SELECT COUNT(*) FROM market_archive_v2_eligibility"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                candidate.db.execute(
                    "SELECT connection_epoch,snapshot_complete,eligible_for_decision "
                    "FROM market_archive_v2_eligibility WHERE event_id='candidate-before'"
                ).fetchone(),
                ("epoch-before", 1, 1),
            )
            self.assertEqual(hashlib.sha256(raw_path.read_bytes()).hexdigest(), raw_hash)
            candidate.db.close()

    def test_unknown_market_index_shape_fails_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market.sqlite3"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE market_archive_v2(unexpected TEXT)")
            db.execute("INSERT INTO market_archive_v2 VALUES('preserve')")
            db.commit()
            db.close()
            with self.assertRaisesRegex(RuntimeError, "schema is incompatible"):
                MarketArchiveIndexV2(path)
            db = sqlite3.connect(path)
            self.assertEqual(
                db.execute("SELECT unexpected FROM market_archive_v2").fetchone()[0], "preserve"
            )
            db.close()


if __name__ == "__main__":
    unittest.main()

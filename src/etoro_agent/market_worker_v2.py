from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .config_v2 import load_config_v2
from .data_catalog_v2 import ImmutableDataCatalog
from .mcp import EtoroMCPClient
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .systemd_notify_v2 import ready, watchdog
from .ws_market_v2 import EtoroWebSocketCollector, WebSocketEvent


class MarketArchiveIndexV2:
    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(Path(path))
        columns = self.db.execute("PRAGMA table_info(market_archive_v2)").fetchall()
        if columns and columns[0][1] == "raw_hash" and int(columns[0][5]) == 1:
            self.db.execute("ALTER TABLE market_archive_v2 RENAME TO market_archive_v2_legacy")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS market_archive_v2(
               event_id TEXT PRIMARY KEY, raw_hash TEXT NOT NULL,
               artifact_path TEXT NOT NULL, topic TEXT NOT NULL,
               event_time TEXT NOT NULL, received_at TEXT NOT NULL, sequence INTEGER,
               gap_detected INTEGER NOT NULL, indexed_at TEXT NOT NULL)"""
        )
        if columns and columns[0][1] == "raw_hash" and int(columns[0][5]) == 1:
            self.db.execute(
                """INSERT INTO market_archive_v2(
                     event_id,raw_hash,artifact_path,topic,event_time,received_at,
                     sequence,gap_detected,indexed_at)
                   SELECT 'legacy-' || substr(raw_hash,1,24) || '-' || rowid,
                     raw_hash,artifact_path,topic,event_time,received_at,
                     sequence,gap_detected,indexed_at
                   FROM market_archive_v2_legacy"""
            )
            self.db.execute("DROP TABLE market_archive_v2_legacy")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS market_archive_v2_raw_hash_idx ON market_archive_v2(raw_hash)"
        )
        self.db.commit()

    def record(self, event: WebSocketEvent, artifact_path: str) -> None:
        self.db.execute(
            "INSERT INTO market_archive_v2 VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"receipt-{uuid.uuid4()}",
                event.raw_hash,
                artifact_path,
                event.topic,
                event.event_time.astimezone(UTC).isoformat(),
                event.received_at.astimezone(UTC).isoformat(),
                event.sequence,
                int(event.gap_detected),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.db.commit()


async def run(config_path: str, index_path: str, postgres_dsn_file: str) -> None:
    config = load_config_v2(config_path)
    if not config.websocket_enabled:
        raise PermissionError("WebSocket ingestion is disabled by configuration")
    EtoroMCPClient().verify_isolated_demo_read_scope()
    catalog = ImmutableDataCatalog(config.data_catalog_path)
    index = MarketArchiveIndexV2(index_path)
    dsn = Path(postgres_dsn_file).read_text(encoding="utf-8").strip()
    if not dsn:
        raise RuntimeError("market heartbeat PostgreSQL DSN is empty")
    store = PostgresRuntimeStoreV2.from_dsn(dsn)
    store.require_schema()
    last_heartbeat = 0.0

    async def persist_raw(raw: bytes, _: datetime) -> str:
        artifact = catalog.ingest_bytes(raw, suffix=".json")
        return artifact.relative_path

    async def on_event(event: WebSocketEvent) -> None:
        nonlocal last_heartbeat
        path = event.artifact_path
        if not path:
            artifact = catalog.ingest_bytes(
                json.dumps(
                    dict(event.payload), sort_keys=True, separators=(",", ":"), default=str
                ).encode(),
                suffix=".json",
            )
            path = artifact.relative_path
        index.record(event, path)
        current = time.monotonic()
        if event.gap_detected or current - last_heartbeat >= 30:
            store.market_heartbeat(
                "resynchronizing" if event.gap_detected else "healthy",
                {
                    "topic": event.topic,
                    "sequence": event.sequence,
                    "gap_detected": event.gap_detected,
                    "event_time": event.event_time.isoformat(),
                    "received_at": event.received_at.isoformat(),
                    "real_money": False,
                },
            )
            last_heartbeat = current
        watchdog()

    collector = EtoroWebSocketCollector(
        config.symbols,
        on_event=on_event,
        persist_raw=persist_raw,
        on_transport_heartbeat=watchdog,
    )
    store.market_heartbeat("starting", {"gap_detected": False, "real_money": False})
    ready()
    try:
        await collector.run_forever()
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive official eToro WebSocket events for v2 research"
    )
    parser.add_argument("--config", default="config/v2-demo.json")
    parser.add_argument("--index", default="runtime/market-archive-v2.sqlite3")
    parser.add_argument("--postgres-dsn-file", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.config, args.index, args.postgres_dsn_file))


if __name__ == "__main__":
    main()

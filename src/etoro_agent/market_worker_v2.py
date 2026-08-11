from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config_v2 import load_config_v2
from .data_catalog_v2 import ImmutableDataCatalog
from .ws_market_v2 import EtoroWebSocketCollector, WebSocketEvent


class MarketArchiveIndexV2:
    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(Path(path))
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS market_archive_v2(
               raw_hash TEXT PRIMARY KEY, artifact_path TEXT NOT NULL, topic TEXT NOT NULL,
               event_time TEXT NOT NULL, received_at TEXT NOT NULL, sequence INTEGER,
               gap_detected INTEGER NOT NULL, indexed_at TEXT NOT NULL)"""
        )
        self.db.commit()

    def record(self, event: WebSocketEvent, artifact_path: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO market_archive_v2 VALUES(?,?,?,?,?,?,?,?)",
            (
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


async def run(config_path: str, index_path: str) -> None:
    config = load_config_v2(config_path)
    if not config.websocket_enabled:
        raise PermissionError("WebSocket ingestion is disabled by configuration")
    catalog = ImmutableDataCatalog(config.data_catalog_path)
    index = MarketArchiveIndexV2(index_path)
    raw_by_hash: dict[str, str] = {}

    async def persist_raw(raw: bytes, _: datetime) -> None:
        artifact = catalog.ingest_bytes(raw, suffix=".json")
        raw_by_hash[artifact.sha256] = artifact.relative_path

    async def on_event(event: WebSocketEvent) -> None:
        path = raw_by_hash.pop(event.raw_hash, "")
        if not path:
            artifact = catalog.ingest_bytes(
                json.dumps(
                    dict(event.payload), sort_keys=True, separators=(",", ":"), default=str
                ).encode(),
                suffix=".json",
            )
            path = artifact.relative_path
        index.record(event, path)

    collector = EtoroWebSocketCollector(
        config.symbols,
        on_event=on_event,
        persist_raw=persist_raw,
    )
    await collector.run_forever()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive official eToro WebSocket events for v2 research"
    )
    parser.add_argument("--config", default="config/v2-demo.json")
    parser.add_argument("--index", default="runtime/market-archive-v2.sqlite3")
    args = parser.parse_args()
    asyncio.run(run(args.config, args.index))


if __name__ == "__main__":
    main()

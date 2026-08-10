from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping

from .domain_v2 import (
    BrokerOrder,
    DomainEvent,
    Fill,
    IntentEnvelope,
    OrderCommand,
    PositionState,
    PositionStatus,
    Side,
    canonical_json,
    utc,
)

ZERO_HASH = "0" * 64


class RuntimeStoreV2:
    """SQLite reference store with atomic state+event+outbox transactions.

    PostgreSQL is the intended multi-service canonical deployment. This store keeps
    deterministic replay/tests and a single-process fallback on the same schemas.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS v2_events(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                event_time TEXT NOT NULL,
                processing_time TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                causation_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS v2_positions(
                position_id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                status TEXT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS v2_positions_portfolio_status_idx
                ON v2_positions(portfolio_id,status);
            CREATE TABLE IF NOT EXISTS v2_intents(
                intent_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v2_decisions(
                decision_id TEXT PRIMARY KEY,
                packet_hash TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'DECIDED','CLAIMED','APPLIED','FAILED_RETRYABLE','FAILED_TERMINAL','EXPIRED'
                )),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                claimed_by TEXT,
                claim_token TEXT,
                lease_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                applied_effect_json TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS v2_decisions_claim_idx
                ON v2_decisions(state,expires_at,lease_expires_at,created_at);
            CREATE TABLE IF NOT EXISTS v2_order_commands(
                order_command_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                command_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v2_broker_orders(
                order_command_id TEXT PRIMARY KEY REFERENCES v2_order_commands(order_command_id),
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v2_fills(
                fill_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                order_command_id TEXT NOT NULL REFERENCES v2_order_commands(order_command_id),
                fill_json TEXT NOT NULL,
                event_time TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v2_outbox(
                outbox_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                delivered_at TEXT
            );
            CREATE INDEX IF NOT EXISTS v2_outbox_pending_idx
                ON v2_outbox(delivered_at,created_at);
            CREATE TABLE IF NOT EXISTS v2_state(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except Exception:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return canonical_json(value)

    def _append_event_tx(self, tx: sqlite3.Connection, event: DomainEvent) -> str:
        existing = tx.execute(
            "SELECT event_hash FROM v2_events WHERE idempotency_key=?",
            (event.idempotency_key,),
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        row = tx.execute(
            "SELECT event_hash FROM v2_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous = str(row[0]) if row else ZERO_HASH
        payload_json = self._json(dict(event.payload))
        body = self._json(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "schema_version": event.schema_version,
                "event_time": event.event_time,
                "processing_time": event.processing_time,
                "idempotency_key": event.idempotency_key,
                "causation_id": event.causation_id,
                "correlation_id": event.correlation_id,
                "payload": dict(event.payload),
            }
        )
        digest = hashlib.sha256((previous + body).encode("utf-8")).hexdigest()
        tx.execute(
            """INSERT INTO v2_events(
                event_id,event_type,schema_version,event_time,processing_time,
                idempotency_key,causation_id,correlation_id,payload_json,
                previous_hash,event_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.event_id,
                event.event_type,
                event.schema_version,
                event.event_time.isoformat(),
                event.processing_time.isoformat(),
                event.idempotency_key,
                event.causation_id,
                event.correlation_id,
                payload_json,
                previous,
                digest,
            ),
        )
        return digest

    def append_event(self, event: DomainEvent) -> str:
        with self.atomic() as tx:
            return self._append_event_tx(tx, event)

    de²È="25Œ¡¹½Ü¤(€€€€€€€Ý¥Ñ Í•±˜¹…Ñ½µ¥Œ ¤…ÌÑàè(€€€€€€€€€€€ÕÈ€ôÑà¹•á•ÕÑ” (€€€€€€€€€€€€€€€€ˆˆ‰UAQØÉ}‘•¥Í¥½¹ÌMPÍÑ…Ñ”ôü±±…¥µ}Ñ½­•¸õ9U10±±•…Í•}•áÁ¥É•Í}…Ðõ9U10°(€€€€€€€€€€€€€€€€€€ÕÁ‘…Ñ•‘}…Ðôü]!I‘•¥Í¥½¹}¥ôü9ÍÑ…Ñ”ô1%5œ9±…¥µ}Ñ½­•¸ôüˆˆˆ°(€€€€€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€€€€€‰%1}IQIe	1ˆ¥˜É•ÑÉå…‰±”•±Í”€‰%1}QI5%90ˆ°(€€€€€€€€€€€€€€€€€€€ÕÉÉ•¹Ð¹¥Í½™½Éµ…Ð ¤°‘•¥Í¥½¹}¥°±…¥µ}Ñ½­•¸°(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜ÕÈ¹É½Ý½Õ¹Ð€„ô€Äè(€€€€€€€€€€€€€€€É…¥Í”A•Éµ¥ÍÍ¥½¹ÉÉ½È ‰‘•¥Í¥½¸±…¥´Ñ½­•¸¥Ì¹½Ð…Ñ¥Ù”ˆ¤((€€€‘•˜Í…Ù•}½É‘•É}‰Õ¹‘±” (€€€€€€€Í•±˜°(€€€€€€€½µµ…¹è=É‘•É½µµ…¹°(€€€€€€€‰É½­•É}½É‘•Èè	É½­•É=É‘•È°(€€€€€€€•Ù•¹Ðè½µ…¥¹Ù•¹Ð°(€€€€€€€€¨°(€€€€€€€½ÕÑ‰½á}Ñ½Á¥ŒèÍÑÈð9½¹”€ô9½¹”°(€€€€€€€½ÕÑ‰½á}Á…å±½…è5…ÁÁ¥¹mÍÑÈ°¹åtð9½¹”€ô9½¹”°(€€€€¤€´ø‰½½°è(€€€€€€€Ý¥Ñ Í•±˜¹…Ñ½µ¥Œ ¤…ÌÑàè(€€€€€€€€€€€•á¥ÍÑ¥¹œ€ôÑà¹•á•ÕÑ” (€€€€€€€€€€€€€€€€‰M1P½É‘•É}½µµ…¹‘}¥I=4ØÉ}½É‘•É}½µµ…¹‘Ì]!I¥‘•µÁ½Ñ•¹å}­•äôüˆ°(€€€€€€€€€€€€€€€€¡½µµ…¹¹¥‘•µÁ½Ñ•¹å}­•ä°¤°(€€€€€€€€€€€€¤¹™•Ñ¡½¹” ¤(€€€€€€€€€€€¥˜•á¥ÍÑ¥¹œ¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸…±Í”(€€€€€€€€€€€Ñà¹•á•ÕÑ” (€€€€€€€€€€€€€€€€‰%9MIP%9Q<ØÉ}½É‘•É}½µµ…¹‘ÌY1UL ü°ü°ü°ü¤ˆ°(€€€€€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€€€€½µµ…¹¹½É‘•É}½µµ…¹‘}¥°(€€€€€€€€€€€€€€€€€€€½µµ…¹¹¥‘•µÁ½Ñ•¹å}­•ä°(€€€€€€€€€€€€€€€€€€€Í•±˜¹}©Í½¸¡…Í‘¥Ð¡½µµ…¹¤¤°(€€€€€€€€€€€€€€€€€€€½µµ…¹¹É•…Ñ•‘}…Ð¹¥Í½™½Éµ…Ð ¤°(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€¤(€€€€€€€€€€€Ñà¹•á•ÕÑ” (€€€€€€€€€€€€€€€€‰%9MIP%9Q<ØÉ}‰É½­•É}½É‘•ÉÌY1UL ü°ü°ü¤ˆ°(€€€€€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€€€€½µµ…¹¹½É‘•É}½µµ…¹‘}¥°(€€€€€€€€€€€€€€€€€€€Í•±˜¹}©Í½¸¡…Í‘¥Ð¡‰É½­•É}½É‘•È¤¤°(€€€€€€€€€€€€€€€€€€€•Ù•¹Ð¹ÁÉ½•ÍÍ¥¹}Ñ¥µ”¹¥Í½™½Éµ…Ð ¤°(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜½ÕÑ‰½á}Ñ½Á¥Œ¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€½ÕÑ‰½á}¥€ô˜‰½ÕÑ‰½àµí¡…Í¡±¥ˆ¹Í¡„ÈÔØ¡½µµ…¹¹¥‘•µÁ½Ñ•¹å}­•ä¹•¹½‘” ¤¤¹¡•á‘¥•ÍÐ ¥lèÈÑuôˆ(€€€€€€€€€€€€€€€Ñà¹•á•ÕÑ” (€€€€€€€€€€€€€€€€€€€€ˆˆ‰%9MIP%9Q<ØÉ}½ÕÑ‰½à¡½ÕÑ‰½á}¥±Ñ½Á¥Œ±Á…å±½…‘}©Í½¸±¥‘•µÁ½Ñ•¹å}­•ä±É•…Ñ•‘}…Ð¤(€€€€€€€€€€€€€€€€€€€€€€Y1UL ü°ü°ü°ü°ü¤ˆˆˆ°(€€€€€€€€€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€€€€€€€€½ÕÑ‰½á}¥°(€€€€€€€€€€€€€€€€€€€€€€€½ÕÑ‰½á}Ñ½Á¥Œ°(€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹}©Í½¸¡‘¥Ð¡½ÕÑ‰½á}Á…å±½…½Èíô¤¤°(€€€€€€€€€€€€€€€€€€€€€€€½µµ…¹¹¥‘•µÁ½Ñ•¹å}­•ä°(€€€€€€€€€€€€€€€€€€€€€€€•Ù•¹Ð¹ÁÉ½•ÍÍ¥¹}Ñ¥µ”¹¥Í½™½Éµ…Ð ¤°(€€€€€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€Í•±˜¹}…ÁÁ•¹‘}•Ù•¹Ñ}Ñà¡Ñà°•Ù•¹Ð¤(€€€€€€€€€€€É•ÑÕÉ¸QÉÕ”((€€€‘•˜Í…Ù•}‰É½­•É}½É‘•È¡Í•±˜°½É‘•Èè	É½­•É=É‘•È°•Ù•¹Ðè½µ…¥¹Ù•¹Ð¤€´ø9½¹”è(€€€€€€€Ý¥Ñ Í•±˜¹…Ñ½µ¥Œ ¤…ÌÑàè(€€€€€€€€€€€ÕÈ€ôÑà¹•á•ÕÑ” (€€€€€€€€€€€€€€€€‰UAQØÉ}‰É½­•É}½É‘•ÉÌMPÍÑ…Ñ•}©Í½¸ôü±ÕÁ‘…Ñ•‘}…Ðôü]!I½É‘•É}½µµ…¹‘}¥ôüˆ°(€€€€€€€€€€€€€€€€¡Í•±˜¹}©Í½¸¡…Í‘¥Ð¡½É‘•È¤¤°•Ù•¹Ð¹ÁÉ½•ÍÍ¥¹}Ñ¥µ”¹¥Í½™½Éµ…Ð ¤°½É‘•È¹½É‘•É}½µµ…¹‘}¥¤°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜ÕÈ¹É½Ý½Õ¹Ð€„ô€Äè(€€€€€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰‰É½­•È½É‘•Èµ¥ÍÍ¥¹œˆ¤(€€€€€€€€€€€Í•±˜¹}…ÁÁ•¹‘}•Ù•¹Ñ}Ñà¡Ñà°•Ù•¹Ð¤((€€€‘•˜™¥±±}•á¥ÍÑÌ¡Í•±˜°¥‘•µÁ½Ñ•¹å}­•äèÍÑÈ¤€´ø‰½½°è(€€€€€€€É•ÑÕÉ¸Í•±˜¹‘ˆ¹•á•ÕÑ” (€€€€€€€€€€€€‰M1P€ÄI=4ØÉ}™¥±±Ì]!I¥‘•µÁ½Ñ•¹å}­•äôüˆ°€¡¥‘•µÁ½Ñ•¹å}­•ä°¤(€€€€€€€€¤¹™•Ñ¡½¹” ¤¥Ì¹½Ð9½¹”((€€€‘•˜Í…Ù•}™¥±±}Á½Í¥Ñ¥½¹}‰Õ¹‘±” (€€€€€€€Í•±˜°(€€€€€€€™¥±°è¥±°°(€€€€€€€½É‘•Èè	É½­•É=É‘•È°(€€€€€€€Á½Í¥Ñ¥½¸èA½Í¥Ñ¥½¹MÑ…Ñ”°(€€€€€€€™¥±±}•Ù•¹Ðè½µ…¥¹Ù•¹Ð°(€€€€€€€Á½Í¥Ñ¥½¹}•Ù•¹Ðè½µ…¥¹Ù•¹Ð°(€€€€¤€´ø‰½½°è(€€€€€€€€ˆˆ‰Ñ½µ¥…±±äÁ•ÉÍ¥ÍÐ™¥±°°½É‘•ÈÁÉ½©•Ñ¥½¸°Á½Í¥Ñ¥½¸µÕÑ…Ñ¥½¸°…¹‰½Ñ •Ù•¹ÑÌ¸ˆˆˆ(€€€€€€€Ý¥Ñ Í•±˜¹…Ñ½µ¥Œ ¤…ÌÑàè(€€€€€€€€€€€ÕÈ€ôÑà¹•á•ÕÑ” (€€€€€€€€€€€€€€€€ˆˆ‰%9MIP=H%9=I%9Q<ØÉ}™¥±±Ì¡™¥±±}¥±¥‘•µÁ½Ñ•¹å}­•ä±½É‘•É}½µµ…¹‘}¥±™¥±±}©Í½¸±•Ù•¹Ñ}Ñ¥µ”¤(€€€€€€€€€€€€€€€€€€Y1UL ü°ü°ü°ü°ü¤ˆˆˆ°(€€€€€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€€€€™¥±°¹™¥±±}¥°™¥±°¹¥‘•µÁ½Ñ•¹å}­•ä°™¥±°¹½É‘•É}½µµ…¹‘}¥°(€€€€€€€€€€€€€€€€€€€Í•±˜¹}©Í½¸¡…Í‘¥Ð¡™¥±°¤¤°™¥±°¹•Ù•¹Ñ}Ñ¥µ”¹¥Í½™½Éµ…Ð ¤°(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜ÕÈ¹É½Ý½Õ¹Ð€ôô€Àè(€€€€€€€€€€€€€€€É•ÑÕÉ¸…±Í”(€€€€€€€€€€€½É‘•É}ÕÁ‘…Ñ”€ôÑà¹•á•ÕÑ” (€€€€€€€€€€€€€€€€‰UAQØÉ}‰É½­•É}½É‘•ÉÌMPÍÑ…Ñ•}©Í½¸ôü±ÕÁ‘…Ñ•‘}…Ðôü]!I½É‘•É}½µµ…¹‘}¥ôüˆ°(€€€€€€€€€€€€€€€€¡Í•±˜¹}©Í½¸¡…Í‘¥Ð¡½É‘•È¤¤°™¥±±}•Ù•¹Ð¹ÁÉ½•ÍÍ¥¹}Ñ¥µ”¹¥Í½™½Éµ…Ð ¤°½É‘•È¹½É‘•É}½µµ…¹‘}¥¤°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜½É‘•É}ÕÁ‘…Ñ”¹É½Ý½Õ¹Ð€„ô€Äè(€€€€€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰‰É½­•È½É‘•Èµ¥ÍÍ¥¹œˆ¤(€€€€€€€€€€€Ñà¹•á•ÕÑ” (€€€€€€€€€€€€€€€€ˆˆ‰%9MIP%9Q<ØÉ}Á½Í¥Ñ¥½¹Ì¡Á½Í¥Ñ¥½¹}¥±Á½ÉÑ™½±¥½}¥±ÍÑ…ÑÕÌ±ÍÑ…Ñ•}©Í½¸±ÕÁ‘…Ñ•‘}…Ð¤(€€€€€€€€€€€€€€€€€€Y1UL ü°ü°ü°ü°ü¤(€€€€€€€€€€€€€€€€€€=8=91%P¡Á½Í¥Ñ¥½¹}¥¤<UAQMP(€€€€€€€€€€€€€€€€€€€€Á½ÉÑ™½±¥½}¥õ•á±Õ‘•¹Á½ÉÑ™½±¥½}¥±ÍÑ…ÑÕÌõ•á±Õ‘•¹ÍÑ…ÑÕÌ°(€€€€€€€€€€€€€€€€€€€€ÍÑ…Ñ•}©Í½¸õ•á±Õ‘•¹ÍÑ…Ñ•}©Í½¸±ÕÁ‘…Ñ•‘}…Ðõ•á±Õ‘•¹ÕÁ‘…Ñ•‘}…Ðˆˆˆ°(€€€€€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€€€€Á½Í¥Ñ¥½¸¹Á½Í¥Ñ¥½¹}¥°Á½Í¥Ñ¥½¸¹Á½ÉÑ™½±¥½}¥°Á½Í¥Ñ¥½¸¹ÍÑ…ÑÕÌ¹Ù…±Õ”°(€€€€€€€€€€€€€€€€€€€Í•±˜¹}©Í½¸¡…Í‘¥Ð¡Á½Í¥Ñ¥½¸¤¤°Á½Í¥Ñ¥½¹}•Ù•¹Ð¹ÁÉ½•ÍÍ¥¹}Ñ¥µ”¹¥Í½™½Éµ…Ð ¤°(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€¤(€€€€€€€€€€€Í•±˜¹}…ÁÁ•¹‘}•Ù•¹Ñ}Ñà¡Ñà°™¥±±}•Ù•¹Ð¤(€€€€€€€€€€€Í•±˜¹}…ÁÁ•¹‘}•Ù•¹Ñ}Ñà¡Ñà°Á½Í¥Ñ¥½¹}•Ù•¹Ð¤(€€€€€€€€€€€É•ÑÕÉ¸QÉÕ”((€€€‘•˜½É‘•É}½µµ…¹¡Í•±˜°½É‘•É}½µµ…¹‘}¥èÍÑÈ¤€´ø=É‘•É½µµ…¹è(€€€€€€€É½Ü€ôÍ•±˜¹‘ˆ¹•á•ÕÑ” (€€€€€€€€€€€€‰M1P½µµ…¹‘}©Í½¸I=4ØÉ}½É‘•É}½µµ…¹‘Ì]!I½É‘•É}½µµ…¹‘}¥ôüˆ°(€€€€€€€€€€€€¡½É‘•É}½µµ…¹‘}¥°¤°(€€€€€€€€¤¹™•Ñ¡½¹” ¤(€€€€€€€¥˜É½Ü¥Ì9½¹”è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰½É‘•È½µµ…¹µ¥ÍÍ¥¹œˆ¤(€€€€€€€Ù…±Õ”€ô©Í½¸¹±½…‘Ì¡ÍÑÈ¡É½ÝlÁt¤¤(€€€€€€€™½È­•ä¥¸€ ‰…µ½Õ¹Ñ}ÕÍˆ°€‰ÅÕ…¹Ñ¥Ñäˆ°€‰Õ¹¥ÑÍ}Ñ½}‘•‘ÕÐˆ¤è(€€€€€€€€€€€¥˜Ù…±Õ”¹•Ð¡­•ä¤¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€Ù…±Õ•m­•åt€ô•¥µ…°¡ÍÑÈ¡Ù…±Õ•m­•åt¤¤(€€€€€€€Ù…±Õ•l‰Í¥‘”‰t€ôM¥‘”¡Ù…±Õ•l‰Í¥‘”‰t¤(€€€€€€€™½È­•ä¥¸€ ‰É•…Ñ•‘}…Ðˆ°€‰•áÁ¥É•Í}…Ðˆ¤è(€€€€€€€€€€€Ù…±Õ•m­•åt€ô‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð¡Ù…±Õ•m­•åt¤(€€€€€€€É•ÑÕÉ¸=É‘•É½µµ…¹ ¨©Ù…±Õ”¤((€€€‘•˜‰É½­•É}½É‘•È¡Í•±˜°½É‘•É}½µµ…¹‘}¥èÍÑÈ¤€´ø	É½­•É=É‘•Èè(€€€€€€€É½Ü€ôÍ•±˜¹‘ˆ¹•á•ÕÑ” (€€€€€€€€€€€€‰M1PÍÑ…Ñ•}©Í½¸I=4ØÉ}‰É½­•É}½É‘•ÉÌ]!I½É‘•É}½µµ…¹‘}¥ôüˆ°(€€€€€€€€€€€€¡½É‘•É}½µµ…¹‘}¥°¤°(€€€€€€€€¤¹™•Ñ¡½¹” ¤(€€€€€€€¥˜É½Ü¥Ì9½¹”è(€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰‰É½­•È½É‘•Èµ¥ÍÍ¥¹œˆ¤(€€€€€€€Ù…±Õ”€ô©Í½¸¹±½…‘Ì¡ÍÑÈ¡É½ÝlÁt¤¤(€€€€€€€™É½´€¹‘½µ…¥¹}ØÈ¥µÁ½ÉÐ=É‘•ÉMÑ…ÑÕÌ(€€€€€€€Ù…±Õ•l‰ÍÑ…ÑÕÌ‰t€ô=É‘•ÉMÑ…ÑÕÌ¡Ù…±Õ•l‰ÍÑ…ÑÕÌ‰t¤(€€€€€€€Ù…±Õ•l‰™¥±±•‘}ÅÕ…¹Ñ¥Ñä‰t€ô•¥µ…°¡ÍÑÈ¡Ù…±Õ•l‰™¥±±•‘}ÅÕ…¹Ñ¥Ñä‰t¤¤(€€€€€€€¥˜Ù…±Õ”¹•Ð ‰…Ù•É…•}™¥±±}ÁÉ¥”ˆ¤¥Ì¹½Ð9½¹”è(€€€€€€€€€€€Ù…±Õ•l‰…Ù•É…•}™¥±±}ÁÉ¥”‰t€ô•¥µ…°¡ÍÑÈ¡Ù…±Õ•l‰…Ù•É…•}™¥±±}ÁÉ¥”‰t¤¤(€€€€€€€™½È­•ä¥¸€ ‰ÍÕ‰µ¥ÑÑ•‘}…Ðˆ°€‰…­¹½Ý±•‘•‘}…Ðˆ°€‰±…ÍÑ}ÕÁ‘…Ñ•}…Ðˆ¤è(€€€€€€€€€€€¥˜Ù…±Õ”¹•Ð¡­•ä¤¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€Ù…±Õ•m­•åt€ô‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð¡Ù…±Õ•m­•åt¤(€€€€€€€É•ÑÕÉ¸	É½­•É=É‘•È ¨©Ù…±Õ”¤((€€€‘•˜Á•¹‘¥¹}½ÕÑ‰½à¡Í•±˜°±¥µ¥Ðè¥¹Ð€ô€ÄÀÀ¤€´øÑÕÁ±•m5…ÁÁ¥¹mÍÑÈ°¹åt°€¸¸¹tè(€€€€€€€É½ÝÌ€ôÍ•±˜¹‘ˆ¹•á•ÕÑ” (€€€€€€€€€€€€ˆˆ‰M1P½ÕÑ‰½á}¥±Ñ½Á¥Œ±Á…å±½…‘}©Í½¸±¥‘•µÁ½Ñ•¹å}­•ä±É•…Ñ•‘}…Ð(€€€€€€€€€€€€€€I=4ØÉ}½ÕÑ‰½à]!I‘•±¥Ù•É•‘}…Ð%L9U10=IH	dÉ•…Ñ•‘}…Ð1%5%P€üˆˆˆ°(€€€€€€€€€€€€¡µ…à Ä°µ¥¸¡±¥µ¥Ð°€ÄÀÀÀ¤¤°¤°(€€€€€€€€¤¹™•Ñ¡…±° ¤(€€€€€€€É•ÑÕÉ¸ÑÕÁ±” (€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰½ÕÑ‰½á}¥ˆèÍÑÈ¡É½ÝlÁt¤°€‰Ñ½Á¥ŒˆèÍÑÈ¡É½ÝlÅt¤°(€€€€€€€€€€€€€€€€‰Á…å±½…ˆè©Í½¸¹±½…‘Ì¡ÍÑÈ¡É½ÝlÉt¤¤°€‰¥‘•µÁ½Ñ•¹å}­•äˆèÍÑÈ¡É½ÝlÍt¤°(€€€€€€€€€€€€€€€€‰É•…Ñ•‘}…ÐˆèÍÑÈ¡É½ÝlÑt¤°(€€€€€€€€€€€ô(€€€€€€€€€€€™½ÈÉ½Ü¥¸É½ÝÌ(€€€€€€€€¤((€€€‘•˜µ…É­}½ÕÑ‰½á}‘•±¥Ù•É•¡Í•±˜°½ÕÑ‰½á}¥èÍÑÈ°…Ðè‘…Ñ•Ñ¥µ”¤€´ø9½¹”è(€€€€€€€Í•±˜¹‘ˆ¹•á•ÕÑ” (€€€€€€€€€€€€‰UAQØÉ}½ÕÑ‰½àMP‘•±¥Ù•É•‘}…Ðôü]!I½ÕÑ‰½á}¥ôü9‘•±¥Ù•É•‘}…Ð%L9U10ˆ°(€€€€€€€€€€€€¡ÕÑŒ¡…Ð¤¹¥Í½™½Éµ…Ð ¤°½ÕÑ‰½á}¥¤°(€€€€€€€€¤(
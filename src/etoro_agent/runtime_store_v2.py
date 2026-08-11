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

    def verify_event_chain(self) -> bool:
        previous = ZERO_HASH
        for row in self.db.execute(
            "SELECT * FROM v2_events ORDER BY sequence"
        ):
            if str(row["previous_hash"]) != previous:
                return False
            body = self._json(
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "schema_version": row["schema_version"],
                    "event_time": datetime.fromisoformat(row["event_time"]),
                    "processing_time": datetime.fromisoformat(row["processing_time"]),
                    "idempotency_key": row["idempotency_key"],
                    "causation_id": row["causation_id"],
                    "correlation_id": row["correlation_id"],
                    "payload": json.loads(row["payload_json"]),
                }
            )
            expected = hashlib.sha256((previous + body).encode("utf-8")).hexdigest()
            if expected != row["event_hash"]:
                return False
            previous = str(row["event_hash"])
        return True

    def save_position_tx(
        self, tx: sqlite3.Connection, position: PositionState, event: DomainEvent
    ) -> None:
        tx.execute(
            """INSERT INTO v2_positions(position_id,portfolio_id,status,state_json,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(position_id) DO UPDATE SET
                 portfolio_id=excluded.portfolio_id,status=excluded.status,
                 state_json=excluded.state_json,updated_at=excluded.updated_at""",
            (
                position.position_id,
                position.portfolio_id,
                position.status.value,
                self._json(asdict(position)),
                event.processing_time.isoformat(),
            ),
        )
        self._append_event_tx(tx, event)

    def save_position(self, position: PositionState, event: DomainEvent) -> None:
        with self.atomic() as tx:
            self.save_position_tx(tx, position, event)

    @staticmethod
    def _position_from_json(text: str) -> PositionState:
        value = json.loads(text)
        for key in (
            "quantity","entry_price","stop_price","take_profit_price","stop_fraction",
            "take_profit_fraction","financing_accrued","fees_accrued","realized_pnl",
            "unrealized_pnl","last_mark",
        ):
            if value.get(key) is not None:
                value[key] = Decimal(str(value[key]))
        value["side"] = Side(value["side"])
        value["status"] = PositionStatus(value["status"])
        if value.get("exit_reason") is not None:
            from .domain_v2 import ExitReason
            value["exit_reason"] = ExitReason(value["exit_reason"])
        for key in ("entry_event_time","entry_processing_time","expires_at"):
            value[key] = datetime.fromisoformat(value[key])
        return PositionState(**value)

    def positions(self, portfolio_id: str | None = None, *, open_only: bool = False) -> tuple[PositionState, ...]:
        clauses: list[str] = []
        params: list[str] = []
        if portfolio_id is not None:
            clauses.append("portfolio_id=?")
            params.append(portfolio_id)
        if open_only:
            clauses.append("status='OPEN'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.db.execute(
            f"SELECT state_json FROM v2_positions{where} ORDER BY position_id", params
        ).fetchall()
        return tuple(self._position_from_json(str(row[0])) for row in rows)

    def save_intent(self, intent: IntentEnvelope, state: str = "ACTIVE") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                """INSERT INTO v2_intents(intent_id,state,envelope_json,created_at,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(intent_id) DO NOTHING""",
                (intent.intent_id, state, self._json(asdict(intent)), intent.created_at.isoformat(), now),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def intent(self, intent_id: str) -> IntentEnvelope:
        row = self.db.execute(
            "SELECT envelope_json FROM v2_intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if row is None:
            raise ValueError("intent missing")
        value = json.loads(str(row[0]))
        for key in (
            "amount_usd", "raw_confidence", "confidence_threshold",
            "stop_loss_fraction", "take_profit_fraction", "reference_bid",
            "reference_ask", "max_price_drift_bps", "max_slippage_bps",
        ):
            value[key] = Decimal(str(value[key]))
        value["side"] = Side(value["side"])
        for key in ("created_at", "valid_after", "expires_at"):
            value[key] = datetime.fromisoformat(value[key])
        value["invalidation_conditions"] = tuple(value.get("invalidation_conditions", ()))
        value["evidence_refs"] = tuple(value.get("evidence_refs", ()))
        return IntentEnvelope(**value)

    def state_get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM v2_state WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def state_set(self, key: str, value: str, at: datetime | None = None) -> None:
        now = utc(at or datetime.now(timezone.utc)).isoformat()
        with self.atomic() as tx:
            tx.execute(
                """INSERT INTO v2_state(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (key, value, now),
            )

    def enqueue_decision(
        self,
        decision_id: str,
        packet_hash: str,
        decision: Mapping[str, Any],
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> bool:
        created = utc(created_at)
        expiry = utc(expires_at)
        if expiry <= created:
            raise ValueError("decision expiry must be after creation")
        with self.atomic() as tx:
            cur = tx.execute(
                """INSERT OR IGNORE INTO v2_decisions(
                   decision_id,packet_hash,decision_json,state,created_at,expires_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    decision_id, packet_hash, self._json(dict(decision)), "DECIDED",
                    created.isoformat(), expiry.isoformat(), created.isoformat(),
                ),
            )
            return cur.rowcount == 1

    def claim_decision(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int = 120,
    ) -> Mapping[str, Any] | None:
        if not worker_id.strip() or lease_seconds < 10:
            raise ValueError("worker/lease is invalid")
        current = utc(now)
        lease = current + timedelta(seconds=lease_seconds)
        with self.atomic() as tx:
            tx.execute(
                """UPDATE v2_decisions SET state='DECIDED',claimed_by=NULL,claim_token=NULL,
                   lease_expires_at=NULL,updated_at=?
                   WHERE state='CLAIMED' AND lease_expires_at<?""",
                (current.isoformat(), current.isoformat()),
            )
            tx.execute(
                """UPDATE v2_decisions SET state='EXPIRED',updated_at=?
                   WHERE state IN ('DECIDED','FAILED_RETRYABLE') AND expires_at<?""",
                (current.isoformat(), current.isoformat()),
            )
            row = tx.execute(
                """SELECT decision_id,packet_hash,decision_json,attempt_count,expires_at
                   FROM v2_decisions
                   WHERE state IN ('DECIDED','FAILED_RETRYABLE') AND expires_at>=?
                   ORDER BY created_at,decision_id LIMIT 1""",
                (current.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            attempt = int(row["attempt_count"]) + 1
            cur = tx.execute(
                """UPDATE v2_decisions SET state='CLAIMED',claimed_by=?,claim_token=?,
                   lease_expires_at=?,attempt_count=?,updated_at=?
                   WHERE decision_id=? AND state IN ('DECIDED','FAILED_RETRYABLE')""",
                (worker_id, token, lease.isoformat(), attempt, current.isoformat(), row["decision_id"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError("decision claim race")
            return {
                "decision_id": str(row["decision_id"]),
                "packet_hash": str(row["packet_hash"]),
                "decision": json.loads(str(row["decision_json"])),
                "claim_token": token,
                "attempt": attempt,
                "expires_at": str(row["expires_at"]),
            }

    def apply_claimed_decision(
        self,
        decision_id: str,
        claim_token: str,
        effect: Mapping[str, Any],
        event: DomainEvent,
    ) -> bool:
        with self.atomic() as tx:
            row = tx.execute(
                "SELECT state,claim_token FROM v2_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise ValueError("decision missing")
            if row["state"] == "APPLIED":
                return False
            if row["state"] != "CLAIMED" or not secrets.compare_digest(str(row["claim_token"]), claim_token):
                raise PermissionError("decision claim token is not active")
            tx.execute(
                """UPDATE v2_decisions SET state='APPLIED',applied_effect_json=?,
                   claim_token=NULL,lease_expires_at=NULL,updated_at=? WHERE decision_id=?""",
                (self._json(dict(effect)), event.processing_time.isoformat(), decision_id),
            )
            self._append_event_tx(tx, event)
            return True

    def fail_claimed_decision(
        self,
        decision_id: str,
        claim_token: str,
        *,
        retryable: bool,
        now: datetime,
    ) -> None:
        current = utc(now)
        with self.atomic() as tx:
            cur = tx.execute(
                """UPDATE v2_decisions SET state=?,claim_token=NULL,lease_expires_at=NULL,
                   updated_at=? WHERE decision_id=? AND state='CLAIMED' AND claim_token=?""",
                (
                    "FAILED_RETRYABLE" if retryable else "FAILED_TERMINAL",
                    current.isoformat(), decision_id, claim_token,
                ),
            )
            if cur.rowcount != 1:
                raise PermissionError("decision claim token is not active")

    def save_order_bundle(
        self,
        command: OrderCommand,
        broker_order: BrokerOrder,
        event: DomainEvent,
        *,
        outbox_topic: str | None = None,
        outbox_payload: Mapping[str, Any] | None = None,
    ) -> bool:
        with self.atomic() as tx:
            existing = tx.execute(
                "SELECT order_command_id FROM v2_order_commands WHERE idempotency_key=?",
                (command.idempotency_key,),
            ).fetchone()
            if existing is not None:
                return False
            tx.execute(
                "INSERT INTO v2_order_commands VALUES(?,?,?,?)",
                (
                    command.order_command_id,
                    command.idempotency_key,
                    self._json(asdict(command)),
                    command.created_at.isoformat(),
                ),
            )
            tx.execute(
                "INSERT INTO v2_broker_orders VALUES(?,?,?)",
                (
                    command.order_command_id,
                    self._json(asdict(broker_order)),
                    event.processing_time.isoformat(),
                ),
            )
            if outbox_topic is not None:
                outbox_id = f"outbox-{hashlib.sha256(command.idempotency_key.encode()).hexdigest()[:24]}"
                tx.execute(
                    """INSERT INTO v2_outbox(outbox_id,topic,payload_json,idempotency_key,created_at)
                       VALUES(?,?,?,?,?)""",
                    (
                        outbox_id,
                        outbox_topic,
                        self._json(dict(outbox_payload or {})),
                        command.idempotency_key,
                        event.processing_time.isoformat(),
                    ),
                )
            self._append_event_tx(tx, event)
            return True

    def save_broker_order(self, order: BrokerOrder, event: DomainEvent) -> None:
        with self.atomic() as tx:
            cur = tx.execute(
                "UPDATE v2_broker_orders SET state_json=?,updated_at=? WHERE order_command_id=?",
                (self._json(asdict(order)), event.processing_time.isoformat(), order.order_command_id),
            )
            if cur.rowcount != 1:
                raise ValueError("broker order missing")
            self._append_event_tx(tx, event)

    def save_fill(self, fill: Fill, order: BrokerOrder, event: DomainEvent) -> bool:
        with self.atomic() as tx:
            cur = tx.execute(
                """INSERT OR IGNORE INTO v2_fills(fill_id,idempotency_key,order_command_id,fill_json,event_time)
                   VALUES(?,?,?,?,?)""",
                (
                    fill.fill_id, fill.idempotency_key, fill.order_command_id,
                    self._json(asdict(fill)), fill.event_time.isoformat(),
                ),
            )
            if cur.rowcount == 0:
                return False
            tx.execute(
                "UPDATE v2_broker_orders SET state_json=?,updated_at=? WHERE order_command_id=?",
                (self._json(asdict(order)), event.processing_time.isoformat(), order.order_command_id),
            )
            self._append_event_tx(tx, event)
            return True

    def order_command(self, order_command_id: str) -> OrderCommand:
        row = self.db.execute(
            "SELECT command_json FROM v2_order_commands WHERE order_command_id=?",
            (order_command_id,),
        ).fetchone()
        if row is None:
            raise ValueError("order command missing")
        value = json.loads(str(row[0]))
        for key in ("amount_usd", "quantity", "units_to_deduct"):
            if value.get(key) is not None:
                value[key] = Decimal(str(value[key]))
        value["side"] = Side(value["side"])
        for key in ("created_at", "expires_at"):
            value[key] = datetime.fromisoformat(value[key])
        return OrderCommand(**value)

    def broker_order(self, order_command_id: str) -> BrokerOrder:
        row = self.db.execute(
            "SELECT state_json FROM v2_broker_orders WHERE order_command_id=?",
            (order_command_id,),
        ).fetchone()
        if row is None:
            raise ValueError("broker order missing")
        value = json.loads(str(row[0]))
        from .domain_v2 import OrderStatus
        value["status"] = OrderStatus(value["status"])
        value["filled_quantity"] = Decimal(str(value["filled_quantity"]))
        if value.get("average_fill_price") is not None:
            value["average_fill_price"] = Decimal(str(value["average_fill_price"]))
        for key in ("submitted_at", "acknowledged_at", "last_update_at"):
            if value.get(key) is not None:
                value[key] = datetime.fromisoformat(value[key])
        return BrokerOrder(**value)

    def pending_outbox(self, limit: int = 100) -> tuple[Mapping[str, Any], ...]:
        rows = self.db.execute(
            """SELECT outbox_id,topic,payload_json,idempotency_key,created_at
               FROM v2_outbox WHERE delivered_at IS NULL ORDER BY created_at LIMIT ?""",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return tuple(
            {
                "outbox_id": str(row[0]), "topic": str(row[1]),
                "payload": json.loads(str(row[2])), "idempotency_key": str(row[3]),
                "created_at": str(row[4]),
            }
            for row in rows
        )

    def mark_outbox_delivered(self, outbox_id: str, at: datetime) -> None:
        self.db.execute(
            "UPDATE v2_outbox SET delivered_at=? WHERE outbox_id=? AND delivered_at IS NULL",
            (utc(at).isoformat(), outbox_id),
        )

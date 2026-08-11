from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .models import ApprovedOrder, ExecutionState, KillState


class AuditLog:
    """Append-only, hash-chained SQLite audit log."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._writer_lock_fd = os.open(
            self.path.with_suffix(self.path.suffix + ".writer.lock"),
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA foreign_keys=ON")
        fcntl.flock(self._writer_lock_fd, fcntl.LOCK_EX)
        try:
            self._initialize_database()
        finally:
            fcntl.flock(self._writer_lock_fd, fcntl.LOCK_UN)

    def _initialize_database(self) -> None:
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS approvals (
                proposal_id TEXT PRIMARY KEY,
                request_json TEXT NOT NULL,
                approved_at TEXT,
                consumed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS pnl_daily (
                day TEXT PRIMARY KEY,
                realized_usd TEXT NOT NULL,
                unrealized_usd TEXT NOT NULL,
                equity_usd TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                symbol TEXT PRIMARY KEY,
                units TEXT NOT NULL,
                average_price TEXT NOT NULL,
                last_price TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                amount_usd TEXT NOT NULL,
                price TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pnl_daily_v2 (
                portfolio_id TEXT NOT NULL,
                day TEXT NOT NULL,
                realized_usd TEXT NOT NULL,
                unrealized_usd TEXT NOT NULL,
                fees_usd TEXT NOT NULL DEFAULT '0',
                financing_usd TEXT NOT NULL DEFAULT '0',
                equity_usd TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(portfolio_id, day)
            );
            CREATE TABLE IF NOT EXISTS service_heartbeats (
                service TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                details TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            """
        )
        self._ensure_column("approvals", "envelope_hash", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("approvals", "order_json", "TEXT")
        self._ensure_column("approvals", "actor", "TEXT")
        self._ensure_column("approvals", "expires_at", "INTEGER")
        self._ensure_column("approvals", "state", "TEXT NOT NULL DEFAULT 'AWAITING_APPROVAL'")
        self._ensure_column("approvals", "x_request_id", "TEXT")
        self._ensure_column("approvals", "response_json", "TEXT")
        self._ensure_column("approvals", "last_updated", "TEXT")
        self._ensure_column("approvals", "source", "TEXT NOT NULL DEFAULT 'manual'")
        self.db.commit()

    def _ensure_column(self, table: str, name: str, declaration: str) -> None:
        columns = {str(row[1]) for row in self.db.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            try:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            except sqlite3.OperationalError as exc:
                # Multiple services can enter the idempotent startup migration
                # together. Accept only the exact race where another writer won.
                refreshed = {str(row[1]) for row in self.db.execute(f"PRAGMA table_info({table})")}
                if name not in refreshed:
                    raise exc

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize a durable state mutation with its audit event."""

        with self._lock:
            fcntl.flock(self._writer_lock_fd, fcntl.LOCK_EX)
            try:
                if self.db.in_transaction:
                    raise RuntimeError("nested audit write transactions are unsupported")
                self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            finally:
                fcntl.flock(self._writer_lock_fd, fcntl.LOCK_UN)

    def append_tx(self, event_type: str, payload: dict[str, Any]) -> str:
        """Append inside ``write_transaction`` without committing separately."""

        if not self.db.in_transaction:
            raise RuntimeError("append_tx requires an active audit write transaction")
        ts = datetime.now(UTC).isoformat()
        row = self.db.execute(
            "SELECT event_hash FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = row[0] if row else "0" * 64
        body = self._canonical(
            {"ts": ts, "event_type": event_type, "payload": payload}
        )
        digest = hashlib.sha256((previous + body).encode()).hexdigest()
        self.db.execute(
            "INSERT INTO events(ts,event_type,payload,previous_hash,event_hash) VALUES(?,?,?,?,?)",
            (ts, event_type, self._canonical(payload), previous, digest),
        )
        return digest

    def append(self, event_type: str, payload: dict[str, Any]) -> str:
        with self.write_transaction():
            return self.append_tx(event_type, payload)

    def verify_chain(self) -> bool:
        previous = "0" * 64
        for ts, event_type, payload, stored_previous, event_hash in self.db.execute(
            "SELECT ts,event_type,payload,previous_hash,event_hash FROM events ORDER BY id"
        ):
            if stored_previous != previous:
                return False
            body = self._canonical(
                {"ts": ts, "event_type": event_type, "payload": json.loads(payload)}
            )
            expected = hashlib.sha256((previous + body).encode()).hexdigest()
            if expected != event_hash:
                return False
            previous = event_hash
        return True

    def register_proposal(
        self,
        proposal_id: str,
        request: dict[str, Any],
        order: ApprovedOrder | None = None,
        source: str = "manual",
    ) -> str:
        if not source or len(source) > 64:
            raise ValueError("proposal source is invalid")
        order_json = self._canonical(asdict(order)) if order is not None else None
        request_json = self._canonical(request)
        envelope_hash = hashlib.sha256((order_json or request_json).encode()).hexdigest()
        expires_at = order.expires_at if order is not None else None
        now = datetime.now(UTC).isoformat()
        with self._lock:
            existing = self.db.execute(
                """
                SELECT request_json,envelope_hash,order_json,expires_at,source
                FROM approvals WHERE proposal_id=?
                """,
                (proposal_id,),
            ).fetchone()
            if existing is not None:
                is_identical = (
                    str(existing["request_json"]) == request_json
                    and str(existing["envelope_hash"]) == envelope_hash
                    and existing["order_json"] == order_json
                    and existing["expires_at"] == expires_at
                    and str(existing["source"]) == source
                )
                if not is_identical:
                    raise ValueError("proposal identifiers are immutable and cannot be rebound")
                return envelope_hash
            self.db.execute(
                """
                INSERT INTO approvals(
                    proposal_id,request_json,envelope_hash,order_json,expires_at,state,last_updated,source
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    proposal_id,
                    request_json,
                    envelope_hash,
                    order_json,
                    expires_at,
                    ExecutionState.AWAITING_APPROVAL.value,
                    now,
                    source,
                ),
            )
            self.db.commit()
        return envelope_hash

    def approve_once(
        self,
        proposal_id: str,
        envelope_hash: str | None = None,
        actor: str = "local-owner",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        row = self.db.execute(
            "SELECT envelope_hash,expires_at FROM approvals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ValueError("proposal missing")
        if envelope_hash is not None and str(row["envelope_hash"]) != envelope_hash:
            raise PermissionError("approval does not match the exact sealed request")
        if row["expires_at"] is not None and int(row["expires_at"]) < int(
            datetime.now(UTC).timestamp()
        ):
            raise PermissionError("proposal expired before approval")
        cur = self.db.execute(
            """
            UPDATE approvals
            SET approved_at=?,actor=?,state=?,last_updated=?
            WHERE proposal_id=? AND approved_at IS NULL AND consumed_at IS NULL
            """,
            (now, actor, ExecutionState.APPROVED.value, now, proposal_id),
        )
        self.db.commit()
        if cur.rowcount != 1:
            raise ValueError("proposal missing, already approved, or consumed")
        self.append(
            (
                "standing_demo_authorization"
                if actor == "standing-demo-policy"
                else "operator_approval"
            ),
            {
                "proposal_id": proposal_id,
                "envelope_hash": str(row["envelope_hash"]),
                "actor": actor,
            },
        )

    def require_approval(self, proposal_id: str, envelope_hash: str) -> None:
        row = self.db.execute(
            """
            SELECT approved_at,consumed_at,envelope_hash,expires_at,state
            FROM approvals WHERE proposal_id=?
            """,
            (proposal_id,),
        ).fetchone()
        now = int(datetime.now(UTC).timestamp())
        if (
            row is None
            or row["approved_at"] is None
            or row["consumed_at"] is not None
            or str(row["envelope_hash"]) != envelope_hash
            or (row["expires_at"] is not None and int(row["expires_at"]) < now)
            or str(row["state"]) != ExecutionState.APPROVED.value
        ):
            raise PermissionError(
                "one-time exact operator approval is absent, expired, or consumed"
            )

    def begin_execution(self, proposal_id: str, envelope_hash: str, request_id: str) -> None:
        self.require_approval(proposal_id, envelope_hash)
        now = datetime.now(UTC).isoformat()
        cur = self.db.execute(
            """
            UPDATE approvals SET consumed_at=?,state=?,x_request_id=?,last_updated=?
            WHERE proposal_id=? AND envelope_hash=? AND approved_at IS NOT NULL
              AND consumed_at IS NULL AND state=?
            """,
            (
                now,
                ExecutionState.SENDING.value,
                request_id,
                now,
                proposal_id,
                envelope_hash,
                ExecutionState.APPROVED.value,
            ),
        )
        self.db.commit()
        if cur.rowcount != 1:
            raise PermissionError("one-time operator approval is absent or consumed")

    def reject_approved_before_send(self, proposal_id: str, error_type: str) -> bool:
        """Terminally reject an approved proposal that failed before network send."""

        now = datetime.now(UTC).isoformat()
        response = {"error_type": error_type, "network_write_attempted": False}
        with self._lock:
            cur = self.db.execute(
                """
                UPDATE approvals SET state=?,response_json=?,last_updated=?
                WHERE proposal_id=? AND state=? AND consumed_at IS NULL
                """,
                (
                    ExecutionState.REJECTED.value,
                    self._canonical(response),
                    now,
                    proposal_id,
                    ExecutionState.APPROVED.value,
                ),
            )
            self.db.commit()
        if cur.rowcount != 1:
            return False
        self.append(
            "demo_preflight_rejected",
            {"proposal_id": proposal_id, **response},
        )
        return True

    def reject_expired_before_send(self, proposal_id: str, *, now: int | None = None) -> bool:
        """Terminally reject an expired proposal without attempting a broker write."""

        current = int(datetime.now(UTC).timestamp()) if now is None else int(now)
        updated_at = datetime.now(UTC).isoformat()
        response = {
            "error_type": "ExpiredProposal",
            "network_write_attempted": False,
        }
        with self._lock:
            cur = self.db.execute(
                """
                UPDATE approvals SET state=?,response_json=?,last_updated=?
                WHERE proposal_id=? AND state IN (?,?) AND consumed_at IS NULL
                  AND expires_at IS NOT NULL AND expires_at<?
                """,
                (
                    ExecutionState.REJECTED.value,
                    self._canonical(response),
                    updated_at,
                    proposal_id,
                    ExecutionState.AWAITING_APPROVAL.value,
                    ExecutionState.APPROVED.value,
                    current,
                ),
            )
            self.db.commit()
        if cur.rowcount != 1:
            return False
        self.append(
            "demo_proposal_expired",
            {"proposal_id": proposal_id, **response},
        )
        return True

    def consume_approval(self, proposal_id: str) -> None:
        row = self.db.execute(
            "SELECT envelope_hash FROM approvals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise PermissionError("one-time operator approval is absent or consumed")
        self.begin_execution(proposal_id, str(row["envelope_hash"]), proposal_id)

    def finish_execution(
        self,
        proposal_id: str,
        state: ExecutionState,
        response: dict[str, Any],
    ) -> None:
        if state not in {
            ExecutionState.ACKNOWLEDGED,
            ExecutionState.UNKNOWN,
            ExecutionState.REJECTED,
            ExecutionState.PARTIAL,
            ExecutionState.FILLED,
            ExecutionState.CANCELLED,
            ExecutionState.RECONCILED,
        }:
            raise ValueError("invalid terminal execution state")
        now = datetime.now(UTC).isoformat()
        cur = self.db.execute(
            """
            UPDATE approvals SET state=?,response_json=?,last_updated=?
            WHERE proposal_id=? AND state=?
            """,
            (
                state.value,
                self._canonical(response),
                now,
                proposal_id,
                ExecutionState.SENDING.value,
            ),
        )
        self.db.commit()
        if cur.rowcount != 1:
            raise ValueError("proposal is not in SENDING state")

    def load_order(self, proposal_id: str) -> ApprovedOrder:
        row = self.db.execute(
            "SELECT order_json FROM approvals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if row is None or not row["order_json"]:
            raise ValueError("sealed proposal not found")
        return ApprovedOrder(**json.loads(str(row["order_json"])))

    def proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM approvals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT proposal_id,request_json,envelope_hash,approved_at,actor,expires_at,state,last_updated,source
            FROM approvals
            WHERE state IN (?,?,?)
            ORDER BY rowid DESC LIMIT ?
            """,
            (
                ExecutionState.AWAITING_APPROVAL.value,
                ExecutionState.APPROVED.value,
                ExecutionState.UNKNOWN.value,
                limit,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_daily_pnl(
        self,
        day: str,
        realized: str,
        unrealized: str,
        equity: str,
        portfolio_id: str = "legacy",
        fees: str = "0",
        financing: str = "0",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            "INSERT INTO pnl_daily VALUES(?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET realized_usd=excluded.realized_usd, unrealized_usd=excluded.unrealized_usd, equity_usd=excluded.equity_usd, recorded_at=excluded.recorded_at",
            (day, realized, unrealized, equity, now),
        )
        self.db.execute(
            """
            INSERT INTO pnl_daily_v2(
                portfolio_id,day,realized_usd,unrealized_usd,fees_usd,
                financing_usd,equity_usd,recorded_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(portfolio_id,day) DO UPDATE SET
                realized_usd=excluded.realized_usd,
                unrealized_usd=excluded.unrealized_usd,
                fees_usd=excluded.fees_usd,
                financing_usd=excluded.financing_usd,
                equity_usd=excluded.equity_usd,
                recorded_at=excluded.recorded_at
            """,
            (portfolio_id, day, realized, unrealized, fees, financing, equity, now),
        )
        self.db.commit()

    def event_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def state_get(self, key: str, default: str) -> str:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def state_set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.db.commit()

    def kill_state(self) -> KillState:
        raw = self.state_get("kill_state", KillState.HALT_NEW.value)
        try:
            return KillState(raw)
        except ValueError:
            return KillState.LOCKED

    def set_kill_state(self, state: KillState, actor: str, reason: str) -> None:
        self.state_set("kill_state", state.value)
        self.append(
            "kill_state_changed",
            {"state": state.value, "actor": actor, "reason": reason},
        )

    def heartbeat(self, service: str, status: str, details: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            INSERT INTO service_heartbeats(service,status,details,recorded_at)
            VALUES(?,?,?,?) ON CONFLICT(service) DO UPDATE SET
                status=excluded.status,details=excluded.details,recorded_at=excluded.recorded_at
            """,
            (service, status, self._canonical(details), now),
        )
        self.db.commit()

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id,ts,event_type,payload,event_hash FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload"]),
                "event_hash": row["event_hash"],
            }
            for row in rows
        ]

    def count_today(self, event_types: tuple[str, ...]) -> int:
        if not event_types:
            return 0
        row = self.db.execute(
            """SELECT COUNT(*) FROM events
               WHERE event_type IN (SELECT value FROM json_each(?))
                 AND substr(ts,1,10)=?""",
            (self._canonical(event_types), datetime.now(UTC).date().isoformat()),
        ).fetchone()
        return int(row[0])

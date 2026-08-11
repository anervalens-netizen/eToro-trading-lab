from __future__ import annotations

import hashlib
import json
import sysconfig
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

try:  # Optional until the PostgreSQL runtime is provisioned.
    import psycopg  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - availability is asserted through the public helper.
    psycopg = None


_REPOSITORY_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "ops" / "postgres" / "schema.sql"
_INSTALLED_SCHEMA_PATH = (
    Path(sysconfig.get_path("data")) / "share" / "etoro-demo-agent" / "schema.sql"
)
SCHEMA_PATH = (
    _REPOSITORY_SCHEMA_PATH if _REPOSITORY_SCHEMA_PATH.is_file() else _INSTALLED_SCHEMA_PATH
)
ZERO_HASH = "0" * 64
EXECUTION_STATES = frozenset(
    {
        "PROPOSED",
        "RISK_REJECTED",
        "SEALED",
        "AWAITING_APPROVAL",
        "APPROVED",
        "SENDING",
        "ACKNOWLEDGED",
        "UNKNOWN",
        "REJECTED",
        "PARTIAL",
        "FILLED",
        "CANCELLED",
        "RECONCILED",
    }
)
KILL_STATES = frozenset({"ACTIVE", "HALT_NEW", "REDUCE_ONLY", "LOCKED"})
ALLOWED_EXECUTION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "PROPOSED": frozenset({"RISK_REJECTED", "SEALED"}),
    "RISK_REJECTED": frozenset(),
    "SEALED": frozenset({"AWAITING_APPROVAL"}),
    "AWAITING_APPROVAL": frozenset({"APPROVED"}),
    "APPROVED": frozenset({"SENDING"}),
    "SENDING": frozenset({"ACKNOWLEDGED", "UNKNOWN", "REJECTED", "PARTIAL", "FILLED", "CANCELLED"}),
    "ACKNOWLEDGED": frozenset({"UNKNOWN", "PARTIAL", "FILLED", "CANCELLED", "RECONCILED"}),
    "UNKNOWN": frozenset(
        {"ACKNOWLEDGED", "REJECTED", "PARTIAL", "FILLED", "CANCELLED", "RECONCILED"}
    ),
    "REJECTED": frozenset({"RECONCILED"}),
    "PARTIAL": frozenset({"PARTIAL", "FILLED", "CANCELLED", "RECONCILED"}),
    "FILLED": frozenset({"RECONCILED"}),
    "CANCELLED": frozenset({"RECONCILED"}),
    "RECONCILED": frozenset(),
}
_SENSITIVE_KEY_FRAGMENTS = (
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
    "userkey",
)


class MissingPostgresDependency(RuntimeError):
    pass


class StoreConflictError(RuntimeError):
    pass


def psycopg_available() -> bool:
    return psycopg is not None


def load_schema() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compute_event_hash(previous_hash: str, canonical_body: str) -> str:
    if len(previous_hash) != 64 or any(
        character not in "0123456789abcdef" for character in previous_hash
    ):
        raise ValueError("previous event hash must contain 64 lowercase hexadecimal characters")
    return hashlib.sha256((previous_hash + canonical_body).encode()).hexdigest()


def validate_execution_transition(current: str, target: str) -> None:
    if current not in EXECUTION_STATES or target not in EXECUTION_STATES:
        raise ValueError("unknown execution state")
    if target not in ALLOWED_EXECUTION_TRANSITIONS[current]:
        raise ValueError(f"invalid execution transition: {current} -> {target}")


def ensure_no_credentials(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS):
                raise ValueError(f"credential-like field rejected at {path}.{key}")
            ensure_no_credentials(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            ensure_no_credentials(child, f"{path}[{index}]")


def _utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return result.astimezone(UTC)


class PostgresOperationalStore:
    """Durable operational state with no implicit DSN or credential discovery."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @classmethod
    def from_dsn(cls, dsn: str, *, connect_timeout_seconds: int = 5) -> PostgresOperationalStore:
        if psycopg is None:
            raise MissingPostgresDependency(
                "PostgreSQL support requires the optional 'psycopg' package"
            )
        if not dsn.strip():
            raise ValueError("a PostgreSQL DSN must be supplied explicitly")
        connection = psycopg.connect(dsn, connect_timeout=connect_timeout_seconds)
        return cls(connection)

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(load_schema(), prepare=False)

    @staticmethod
    def _append_event_cursor(
        cursor: Any,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        occurred_at: datetime | None = None,
    ) -> str:
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        ensure_no_credentials(payload)
        timestamp = _utc(occurred_at)
        timestamp_text = timestamp.isoformat(timespec="microseconds")
        canonical_payload = canonical_json(payload)
        canonical_body = canonical_json(
            {"ts": timestamp_text, "event_type": event_type, "payload": payload}
        )
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('etoro_event_hash_chain'))")
        cursor.execute("SELECT event_hash FROM events ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        previous_hash = str(row[0]).strip() if row else ZERO_HASH
        event_hash = compute_event_hash(previous_hash, canonical_body)
        cursor.execute(
            """
            INSERT INTO events(ts,event_type,payload,canonical_body,previous_hash,event_hash)
            VALUES(%s,%s,%s::jsonb,%s,%s,%s)
            """,
            (
                timestamp,
                event_type,
                canonical_payload,
                canonical_body,
                previous_hash,
                event_hash,
            ),
        )
        return event_hash

    def append_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        occurred_at: datetime | None = None,
    ) -> str:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            return self._append_event_cursor(cursor, event_type, payload, occurred_at=occurred_at)

    def verify_event_chain(self) -> bool:
        previous_hash = ZERO_HASH
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT previous_hash,event_hash,canonical_body FROM events ORDER BY id")
            for stored_previous, stored_hash, canonical_body in cursor.fetchall():
                stored_previous = str(stored_previous).strip()
                stored_hash = str(stored_hash).strip()
                if stored_previous != previous_hash:
                    return False
                if compute_event_hash(previous_hash, str(canonical_body)) != stored_hash:
                    return False
                previous_hash = stored_hash
        return True

    def register_proposal(
        self,
        proposal_id: str,
        request: Mapping[str, Any],
        envelope_hash: str,
        *,
        sealed_order: Mapping[str, Any] | None = None,
        expires_at: datetime | None = None,
        initial_state: str = "AWAITING_APPROVAL",
        recorded_at: datetime | None = None,
    ) -> None:
        if not proposal_id.strip():
            raise ValueError("proposal_id must not be empty")
        if initial_state not in EXECUTION_STATES:
            raise ValueError("unknown initial execution state")
        compute_event_hash(envelope_hash, "")  # Validate the exact envelope hash shape.
        ensure_no_credentials(request)
        ensure_no_credentials(sealed_order or {})
        timestamp = _utc(recorded_at)
        expiry = _utc(expires_at) if expires_at is not None else None
        request_json = canonical_json(request)
        order_json = canonical_json(sealed_order) if sealed_order is not None else None
        request_hash = hashlib.sha256(request_json.encode()).hexdigest()

        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                    INSERT INTO proposals(
                        proposal_id,request,request_hash,envelope_hash,sealed_order,state,
                        expires_at,created_at,updated_at
                    ) VALUES(%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s,%s)
                    ON CONFLICT(proposal_id) DO NOTHING
                    """,
                (
                    proposal_id,
                    request_json,
                    request_hash,
                    envelope_hash,
                    order_json,
                    initial_state,
                    expiry,
                    timestamp,
                    timestamp,
                ),
            )
            if cursor.rowcount != 1:
                cursor.execute(
                    "SELECT request_hash,envelope_hash FROM proposals WHERE proposal_id=%s",
                    (proposal_id,),
                )
                existing = cursor.fetchone()
                if (
                    existing is None
                    or str(existing[0]).strip() != request_hash
                    or str(existing[1]).strip() != envelope_hash
                ):
                    raise StoreConflictError(
                        "proposal_id already exists with different immutable content"
                    )
                return
            cursor.execute(
                """
                    INSERT INTO execution_transitions(
                        proposal_id,from_state,to_state,reason,response,recorded_at
                    ) VALUES(%s,NULL,%s,%s,NULL,%s)
                    """,
                (proposal_id, initial_state, "proposal registered", timestamp),
            )
            self._append_event_cursor(
                cursor,
                "proposal_registered",
                {
                    "proposal_id": proposal_id,
                    "request_hash": request_hash,
                    "envelope_hash": envelope_hash,
                    "state": initial_state,
                },
                occurred_at=timestamp,
            )

    def approve_once(
        self,
        proposal_id: str,
        envelope_hash: str,
        actor: str,
        *,
        approved_at: datetime | None = None,
    ) -> None:
        if not actor.strip():
            raise ValueError("approval actor must not be empty")
        timestamp = _utc(approved_at)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                    SELECT envelope_hash,state,expires_at
                    FROM proposals WHERE proposal_id=%s FOR UPDATE
                    """,
                (proposal_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("proposal missing")
            if str(row[0]).strip() != envelope_hash:
                raise PermissionError("approval does not match the exact sealed request")
            if str(row[1]) != "AWAITING_APPROVAL":
                raise PermissionError("proposal is not awaiting approval")
            if row[2] is not None and _utc(row[2]) < timestamp:
                raise PermissionError("proposal expired before approval")
            cursor.execute(
                """
                    INSERT INTO approvals(proposal_id,envelope_hash,actor,approved_at)
                    VALUES(%s,%s,%s,%s)
                    ON CONFLICT(proposal_id) DO NOTHING
                    """,
                (proposal_id, envelope_hash, actor, timestamp),
            )
            if cursor.rowcount != 1:
                raise PermissionError("proposal was already approved")
            cursor.execute(
                "UPDATE proposals SET state='APPROVED',updated_at=%s WHERE proposal_id=%s",
                (timestamp, proposal_id),
            )
            self._record_transition_cursor(
                cursor,
                proposal_id,
                "AWAITING_APPROVAL",
                "APPROVED",
                "exact request approved",
                None,
                timestamp,
            )
            self._append_event_cursor(
                cursor,
                "operator_approval",
                {
                    "proposal_id": proposal_id,
                    "envelope_hash": envelope_hash,
                    "actor": actor,
                },
                occurred_at=timestamp,
            )

    @staticmethod
    def _record_transition_cursor(
        cursor: Any,
        proposal_id: str,
        current: str,
        target: str,
        reason: str,
        response: Mapping[str, Any] | None,
        timestamp: datetime,
    ) -> None:
        validate_execution_transition(current, target)
        ensure_no_credentials(response or {})
        cursor.execute(
            """
            INSERT INTO execution_transitions(
                proposal_id,from_state,to_state,reason,response,recorded_at
            ) VALUES(%s,%s,%s,%s,%s::jsonb,%s)
            """,
            (
                proposal_id,
                current,
                target,
                reason,
                canonical_json(response) if response is not None else None,
                timestamp,
            ),
        )

    def begin_execution(
        self,
        proposal_id: str,
        envelope_hash: str,
        request_id: str,
        *,
        started_at: datetime | None = None,
    ) -> None:
        if not request_id.strip():
            raise ValueError("request_id must not be empty")
        timestamp = _utc(started_at)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                    SELECT p.envelope_hash,p.state,p.expires_at,a.approved_at,a.consumed_at
                    FROM proposals p JOIN approvals a USING(proposal_id)
                    WHERE p.proposal_id=%s FOR UPDATE OF p,a
                    """,
                (proposal_id,),
            )
            row = cursor.fetchone()
            if (
                row is None
                or str(row[0]).strip() != envelope_hash
                or str(row[1]) != "APPROVED"
                or row[3] is None
                or row[4] is not None
                or (row[2] is not None and _utc(row[2]) < timestamp)
            ):
                raise PermissionError("one-time exact approval is absent, expired, or consumed")
            cursor.execute(
                "UPDATE approvals SET consumed_at=%s WHERE proposal_id=%s AND consumed_at IS NULL",
                (timestamp, proposal_id),
            )
            if cursor.rowcount != 1:
                raise PermissionError("one-time approval was already consumed")
            cursor.execute(
                """
                    UPDATE proposals SET state='SENDING',x_request_id=%s,updated_at=%s
                    WHERE proposal_id=%s
                    """,
                (request_id, timestamp, proposal_id),
            )
            self._record_transition_cursor(
                cursor,
                proposal_id,
                "APPROVED",
                "SENDING",
                "one-time approval consumed",
                None,
                timestamp,
            )
            self._append_event_cursor(
                cursor,
                "execution_started",
                {"proposal_id": proposal_id, "request_id": request_id},
                occurred_at=timestamp,
            )

    def transition_execution(
        self,
        proposal_id: str,
        target_state: str,
        *,
        reason: str = "",
        response: Mapping[str, Any] | None = None,
        recorded_at: datetime | None = None,
    ) -> None:
        ensure_no_credentials(response or {})
        timestamp = _utc(recorded_at)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM proposals WHERE proposal_id=%s FOR UPDATE",
                (proposal_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("proposal missing")
            current = str(row[0])
            validate_execution_transition(current, target_state)
            response_json = canonical_json(response) if response is not None else None
            cursor.execute(
                """
                    UPDATE proposals SET state=%s,response=%s::jsonb,updated_at=%s
                    WHERE proposal_id=%s
                    """,
                (target_state, response_json, timestamp, proposal_id),
            )
            self._record_transition_cursor(
                cursor,
                proposal_id,
                current,
                target_state,
                reason,
                response,
                timestamp,
            )
            self._append_event_cursor(
                cursor,
                "execution_state_changed",
                {
                    "proposal_id": proposal_id,
                    "from_state": current,
                    "to_state": target_state,
                    "reason": reason,
                    "response": response,
                },
                occurred_at=timestamp,
            )

    def proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT proposal_id,request,envelope_hash,sealed_order,state,expires_at,
                       x_request_id,response,created_at,updated_at
                FROM proposals WHERE proposal_id=%s
                """,
                (proposal_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        keys = (
            "proposal_id",
            "request",
            "envelope_hash",
            "sealed_order",
            "state",
            "expires_at",
            "x_request_id",
            "response",
            "created_at",
            "updated_at",
        )
        return dict(zip(keys, row, strict=True))

    def list_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("pending proposal limit must be between 1 and 1000")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT proposal_id,envelope_hash,state,expires_at,x_request_id,updated_at
                FROM proposals
                WHERE state IN ('AWAITING_APPROVAL','APPROVED','UNKNOWN')
                ORDER BY updated_at DESC LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()
        keys = (
            "proposal_id",
            "envelope_hash",
            "state",
            "expires_at",
            "x_request_id",
            "updated_at",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def set_kill_state(
        self,
        state: str,
        actor: str,
        reason: str,
        *,
        changed_at: datetime | None = None,
    ) -> int:
        if state not in KILL_STATES:
            raise ValueError("unknown kill state")
        if not actor.strip() or not reason.strip():
            raise ValueError("kill-state actor and reason are required")
        timestamp = _utc(changed_at)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute("SELECT state,version FROM kill_switch WHERE singleton=TRUE FOR UPDATE")
            row = cursor.fetchone()
            current = str(row[0])
            version = int(row[1]) + 1
            cursor.execute(
                """
                    UPDATE kill_switch SET state=%s,actor=%s,reason=%s,version=%s,changed_at=%s
                    WHERE singleton=TRUE
                    """,
                (state, actor, reason, version, timestamp),
            )
            self._append_event_cursor(
                cursor,
                "kill_state_changed",
                {
                    "from_state": current,
                    "to_state": state,
                    "actor": actor,
                    "reason": reason,
                    "version": version,
                },
                occurred_at=timestamp,
            )
            return version

    def kill_state(self) -> tuple[str, int]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT state,version FROM kill_switch WHERE singleton=TRUE")
            row = cursor.fetchone()
        if row is None:
            return "LOCKED", 0
        state = str(row[0])
        return (state, int(row[1])) if state in KILL_STATES else ("LOCKED", int(row[1]))

    def record_daily_pnl(
        self,
        portfolio_id: str,
        day: date,
        *,
        realized_usd: Decimal,
        unrealized_usd: Decimal,
        fees_usd: Decimal,
        financing_usd: Decimal,
        daily_pnl_usd: Decimal,
        equity_usd: Decimal,
        recorded_at: datetime | None = None,
    ) -> None:
        if equity_usd < 0:
            raise ValueError("equity cannot be negative")
        timestamp = _utc(recorded_at)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                    INSERT INTO pnl_daily(
                        portfolio_id,day,realized_usd,unrealized_usd,fees_usd,
                        financing_usd,daily_pnl_usd,equity_usd,recorded_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(portfolio_id,day) DO UPDATE SET
                        realized_usd=EXCLUDED.realized_usd,
                        unrealized_usd=EXCLUDED.unrealized_usd,
                        fees_usd=EXCLUDED.fees_usd,
                        financing_usd=EXCLUDED.financing_usd,
                        daily_pnl_usd=EXCLUDED.daily_pnl_usd,
                        equity_usd=EXCLUDED.equity_usd,
                        recorded_at=EXCLUDED.recorded_at
                    """,
                (
                    portfolio_id,
                    day,
                    realized_usd,
                    unrealized_usd,
                    fees_usd,
                    financing_usd,
                    daily_pnl_usd,
                    equity_usd,
                    timestamp,
                ),
            )
            self._append_event_cursor(
                cursor,
                "daily_pnl_recorded",
                {
                    "portfolio_id": portfolio_id,
                    "day": day.isoformat(),
                    "realized_usd": realized_usd,
                    "unrealized_usd": unrealized_usd,
                    "fees_usd": fees_usd,
                    "financing_usd": financing_usd,
                    "daily_pnl_usd": daily_pnl_usd,
                    "equity_usd": equity_usd,
                },
                occurred_at=timestamp,
            )

    def heartbeat(
        self,
        service: str,
        status: str,
        details: Mapping[str, Any],
        *,
        recorded_at: datetime | None = None,
    ) -> None:
        if not service.strip() or not status.strip():
            raise ValueError("heartbeat service and status are required")
        ensure_no_credentials(details)
        timestamp = _utc(recorded_at)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                    INSERT INTO service_heartbeats(service,status,details,recorded_at)
                    VALUES(%s,%s,%s::jsonb,%s)
                    ON CONFLICT(service) DO UPDATE SET
                        status=EXCLUDED.status,details=EXCLUDED.details,
                        recorded_at=EXCLUDED.recorded_at
                    """,
                (service, status, canonical_json(details), timestamp),
            )

    def heartbeats(self) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT service,status,details,recorded_at FROM service_heartbeats ORDER BY service"
            )
            rows = cursor.fetchall()
        keys = ("service", "status", "details", "recorded_at")
        return [dict(zip(keys, row, strict=True)) for row in rows]

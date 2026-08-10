from __future__ import annotations

import hashlib
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

try:
    import psycopg  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    psycopg = None

from .domain_v2 import DomainEvent, canonical_json

ZERO_HASH = "0" * 64
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "ops" / "postgres" / "schema_v2.sql"


class PostgresStoreV2:
    """Canonical multi-service store for v2 runtime.

    Critical queues use row locking + SKIP LOCKED, while the append-only event
    chain is serialized with a PostgreSQL advisory transaction lock.
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @classmethod
    def from_dsn(cls, dsn: str, *, connect_timeout_seconds: int = 5) -> "PostgresStoreV2":
        if psycopg is None:
            raise RuntimeError("psycopg is required for PostgreSQL v2")
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        return cls(psycopg.connect(dsn, connect_timeout=connect_timeout_seconds))

    def migrate(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with self.connection.transaction():
            with self.connection.cursor() as cur:
                cur.execute(schema, prepare=False)

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                yield cursor

    @staticmethod
    def _event_body(event: DomainEvent) -> str:
        return canonical_json(
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

    def append_event_tx(self, cursor: Any, event: DomainEvent) -> str:
        cursor.execute(
            "SELECT event_hash FROM v2_events WHERE idempotency_key=%s",
            (event.idempotency_key,),
        )
        row = cursor.fetchone()
        if row is not None:
            return str(row[0]).strip()
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('etoro_v2_event_chain'))")
        cursor.execute("SELECT event_hash FROM v2_events ORDER BY sequence DESC LIMIT 1")
        row = cursor.fetchone()
        previous = str(row[0]).strip() if row else ZERO_HASH
        body = self._event_body(event)
        digest = hashlib.sha256((previous + body).encode()).hexdigest()
        cursor.execute(
            """INSERT INTO v2_events(
              event_id,event_type,schema_version,event_time,processing_time,idempotency_key,
              causation_id,correlation_id,payload,canonical_body,previous_hash,event_hash
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
            (
                event.event_id,
                event.event_type,
                event.schema_version,
                event.event_time,
                event.processing_time,
                event.idempotency_key,
                event.causation_id,
                event.correlation_id,
                canonical_json(dict(event.payload)),
                body,
                previous,
                digest,
            ),
        )
        return digest

    def append_event(self, event: DomainEvent) -> str:
        with self.transaction() as cursor:
            return self.append_event_tx(cursor, event)

    def verify_event_chain(self) -> bool:
        previous = ZERO_HASH
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT previous_hash,event_hash,canonical_body FROM v2_events ORDER BY sequence"
            )
            for stored_previous, stored_hash, body in cursor.fetchall():
                stored_previous = str(stored_previous).strip()
                stored_hash = str(stored_hash).strip()
                if stored_previous != previous:
                    return False
                if hashlib.sha256((previous + str(body)).encode()).hexdigest() != stored_hash:
                    return False
                previous = stored_hash
        return True

    def claim_decision(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 120,
    ) -> Mapping[str, Any] | None:
        if not worker_id.strip() or lease_seconds < 10:
            raise ValueError("worker/lease is invalid")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        lease = current + timedelta(seconds=lease_seconds)
        token = secrets.token_urlsafe(32)
        with self.transaction() as cursor:
            cursor.execute(
                """UPDATE v2_decisions SET state='DECIDED',claimed_by=NULL,claim_token=NULL,
                   lease_expires_at=NULL,updated_at=%s
                   WHERE state='CLAIMED' AND lease_expires_at<%s""",
                (current, current),
            )
            cursor.execute(
                """UPDATE v2_decisions SET state='EXPIRED',updated_at=%s
                   WHERE state IN ('DECIDED','FAILED_RETRYABLE') AND expires_at<%s""",
                (current, current),
            )
            cursor.execute(
                """SELECT decision_id,packet_hash,decision,attempt_count,expires_at
                   FROM v2_decisions
                   WHERE state IN ('DECIDED','FAILED_RETRYABLE') AND expires_at>=%s
                   ORDER BY created_at,decision_id
                   FOR UPDATE SKIP LOCKED LIMIT 1""",
                (current,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            attempt = int(row[3]) + 1
            cursor.execute(
                """UPDATE v2_decisions SET state='CLAIMED',claimed_by=%s,claim_token=%s,
                   lease_expires_at=%s,attempt_count=%s,updated_at=%s WHERE decision_id=%s""",
                (worker_id, token, lease, attempt, current, row[0]),
            )
            return {
                "decision_id": str(row[0]),
                "packet_hash": str(row[1]).strip(),
                "decision": row[2],
                "attempt": attempt,
                "claim_token": token,
                "expires_at": row[4].isoformat(),
            }

    def claim_outbox(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 60,
        limit: int = 20,
    ) -> tuple[Mapping[str, Any], ...]:
        if not worker_id.strip() or lease_seconds < 10:
            raise ValueError("worker/lease is invalid")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        lease = current + timedelta(seconds=lease_seconds)
        claimed: list[Mapping[str, Any]] = []
        with self.transaction() as cursor:
            cursor.execute(
                """SELECT outbox_id,topic,payload,idempotency_key,attempt_count
                   FROM v2_outbox WHERE delivered_at IS NULL
                     AND (lease_expires_at IS NULL OR lease_expires_at<%s)
                   ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s""",
                (current, max(1, min(limit, 100))),
            )
            for row in cursor.fetchall():
                token = secrets.token_urlsafe(32)
                attempt = int(row[4]) + 1
                cursor.execute(
                    """UPDATE v2_outbox SET claimed_by=%s,claim_token=%s,lease_expires_at=%s,
                       attempt_count=%s WHERE outbox_id=%s""",
                    (worker_id, token, lease, attempt, row[0]),
                )
                claimed.append(
                    {
                        "outbox_id": str(row[0]),
                        "topic": str(row[1]),
                        "payload": row[2],
                        "idempotency_key": str(row[3]),
                        "attempt": attempt,
                        "claim_token": token,
                    }
                )
        return tuple(claimed)

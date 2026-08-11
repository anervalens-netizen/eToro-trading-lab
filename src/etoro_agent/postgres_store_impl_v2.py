from __future__ import annotations

import hashlib
import secrets
import sysconfig
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

try:
    import psycopg  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    psycopg = None

from .domain_v2 import AuditIntegrityError, DomainEvent, canonical_json

ZERO_HASH = "0" * 64
_REPOSITORY_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "ops" / "postgres" / "schema_v2.sql"
_INSTALLED_SCHEMA_PATH = (
    Path(sysconfig.get_path("data")) / "share" / "etoro-demo-agent" / "schema_v2.sql"
)
_REPOSITORY_AI_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "postgres" / "schema_v2_ai.sql"
)
_INSTALLED_AI_SCHEMA_PATH = (
    Path(sysconfig.get_path("data")) / "share" / "etoro-demo-agent" / "schema_v2_ai.sql"
)
_REPOSITORY_INTEGRITY_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "postgres" / "schema_v3.sql"
)
_INSTALLED_INTEGRITY_SCHEMA_PATH = (
    Path(sysconfig.get_path("data")) / "share" / "etoro-demo-agent" / "schema_v3.sql"
)
_REPOSITORY_QUEUE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "postgres" / "schema_v4.sql"
)
_INSTALLED_QUEUE_SCHEMA_PATH = (
    Path(sysconfig.get_path("data")) / "share" / "etoro-demo-agent" / "schema_v4.sql"
)
_REPOSITORY_MARKET_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "postgres" / "schema_v5.sql"
)
_INSTALLED_MARKET_SCHEMA_PATH = (
    Path(sysconfig.get_path("data")) / "share" / "etoro-demo-agent" / "schema_v5.sql"
)
SCHEMA_PATH = (
    _REPOSITORY_SCHEMA_PATH if _REPOSITORY_SCHEMA_PATH.is_file() else _INSTALLED_SCHEMA_PATH
)
AI_SCHEMA_PATH = (
    _REPOSITORY_AI_SCHEMA_PATH
    if _REPOSITORY_AI_SCHEMA_PATH.is_file()
    else _INSTALLED_AI_SCHEMA_PATH
)
INTEGRITY_SCHEMA_PATH = (
    _REPOSITORY_INTEGRITY_SCHEMA_PATH
    if _REPOSITORY_INTEGRITY_SCHEMA_PATH.is_file()
    else _INSTALLED_INTEGRITY_SCHEMA_PATH
)
QUEUE_SCHEMA_PATH = (
    _REPOSITORY_QUEUE_SCHEMA_PATH
    if _REPOSITORY_QUEUE_SCHEMA_PATH.is_file()
    else _INSTALLED_QUEUE_SCHEMA_PATH
)
MARKET_SCHEMA_PATH = (
    _REPOSITORY_MARKET_SCHEMA_PATH
    if _REPOSITORY_MARKET_SCHEMA_PATH.is_file()
    else _INSTALLED_MARKET_SCHEMA_PATH
)
SCHEMA_VERSION = 5


class PostgresStoreV2:
    """Canonical multi-service store for v2 runtime.

    Critical queues use row locking + SKIP LOCKED, while the append-only event
    chain is serialized with a PostgreSQL advisory transaction lock.
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @classmethod
    def from_dsn(cls, dsn: str, *, connect_timeout_seconds: int = 5) -> Self:
        if psycopg is None:
            raise RuntimeError("psycopg is required for PostgreSQL v2")
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        # Reads must not leave an implicit outer transaction open. Every economic
        # mutation below establishes its own explicit `connection.transaction()`.
        return cls(
            psycopg.connect(
                dsn,
                connect_timeout=connect_timeout_seconds,
                autocommit=True,
            )
        )

    def migrate(self) -> None:
        migrations = (
            (1, "core", SCHEMA_PATH),
            (2, "ai_queue", AI_SCHEMA_PATH),
            (3, "event_integrity", INTEGRITY_SCHEMA_PATH),
            (4, "ai_dead_letter", QUEUE_SCHEMA_PATH),
            (5, "market_heartbeat_boundary", MARKET_SCHEMA_PATH),
        )
        with self.connection.transaction(), self.connection.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS v2_schema_migrations(
                   version INTEGER PRIMARY KEY CHECK(version>0),
                   name TEXT NOT NULL UNIQUE,
                   sha256 CHAR(64) NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
            )
            cur.execute("SELECT version,name,sha256 FROM v2_schema_migrations ORDER BY version")
            applied = {int(row[0]): (str(row[1]), str(row[2]).strip()) for row in cur.fetchall()}
            for version, name, path in migrations:
                schema = path.read_text(encoding="utf-8")
                digest = hashlib.sha256(schema.encode()).hexdigest()
                existing = applied.get(version)
                if existing is not None:
                    if existing != (name, digest):
                        raise RuntimeError("applied v2 database migration checksum mismatch")
                    continue
                cur.execute(schema, prepare=False)
                cur.execute(
                    "INSERT INTO v2_schema_migrations(version,name,sha256) VALUES(%s,%s,%s)",
                    (version, name, digest),
                )
            cur.execute(
                """INSERT INTO v2_meta(key,value,updated_at) VALUES('schema_version',%s,now())
                   ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at""",
                (str(SCHEMA_VERSION),),
            )
            cur.execute("SELECT 1 FROM v2_events LIMIT 1")
            if cur.fetchone() is None:
                current = datetime.now(UTC)
                self.append_event_tx(
                    cur,
                    DomainEvent(
                        event_id="evt-v2-schema-initialized",
                        event_type="SchemaInitialized",
                        schema_version=SCHEMA_VERSION,
                        event_time=current,
                        processing_time=current,
                        idempotency_key="v2-schema-initialized",
                        causation_id="",
                        correlation_id="v2-schema",
                        payload={"schema_version": SCHEMA_VERSION, "real_money": False},
                    ),
                )

    def require_schema(self) -> None:
        migrations = (
            (1, "core", SCHEMA_PATH),
            (2, "ai_queue", AI_SCHEMA_PATH),
            (3, "event_integrity", INTEGRITY_SCHEMA_PATH),
            (4, "ai_dead_letter", QUEUE_SCHEMA_PATH),
            (5, "market_heartbeat_boundary", MARKET_SCHEMA_PATH),
        )
        with self.connection.cursor() as cur:
            cur.execute("SELECT value FROM v2_meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None or int(row[0]) != SCHEMA_VERSION:
                raise RuntimeError("v2 PostgreSQL schema version is incompatible")
            cur.execute("SELECT version,name,sha256 FROM v2_schema_migrations ORDER BY version")
            applied = {
                int(item[0]): (str(item[1]), str(item[2]).strip()) for item in cur.fetchall()
            }
        for version, name, path in migrations:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if applied.get(version) != (name, digest):
                raise RuntimeError("v2 PostgreSQL migration history is incomplete or modified")

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        try:
            with self.connection.transaction(), self.connection.cursor() as cursor:
                yield cursor
        except AuditIntegrityError:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE v2_trading_state SET state='LOCKED',
                       actor='audit-integrity-guard',
                       reason='event idempotency conflict',version=version+1,
                       changed_at=now() WHERE singleton=TRUE"""
                )
                cursor.execute(
                    """INSERT INTO v2_meta(key,value,updated_at)
                       VALUES('audit_integrity_failure','event_idempotency_conflict',now())
                       ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,
                       updated_at=EXCLUDED.updated_at"""
                )
            raise

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
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('etoro_v2_event_chain'))")
        body = self._event_body(event)
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        cursor.execute(
            """SELECT event_hash,canonical_body,canonical_body_hash
               FROM v2_events WHERE idempotency_key=%s""",
            (event.idempotency_key,),
        )
        row = cursor.fetchone()
        if row is not None:
            if str(row[1]) != body or str(row[2]).strip() != body_hash:
                raise AuditIntegrityError(
                    "event idempotency key cannot be rebound to a different canonical body"
                )
            return str(row[0]).strip()
        cursor.execute("SELECT event_hash FROM v2_events ORDER BY sequence DESC LIMIT 1")
        row = cursor.fetchone()
        previous = str(row[0]).strip() if row else ZERO_HASH
        digest = hashlib.sha256((previous + body).encode()).hexdigest()
        cursor.execute(
            """INSERT INTO v2_events(
              event_id,event_type,schema_version,event_time,processing_time,idempotency_key,
              causation_id,correlation_id,payload,canonical_body,canonical_body_hash,
              previous_hash,event_hash
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)""",
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
                body_hash,
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
                """SELECT previous_hash,event_hash,canonical_body,canonical_body_hash
                   FROM v2_events ORDER BY sequence"""
            )
            for stored_previous, stored_hash, body, body_hash in cursor.fetchall():
                stored_previous = str(stored_previous).strip()
                stored_hash = str(stored_hash).strip()
                if stored_previous != previous:
                    return False
                if hashlib.sha256((previous + str(body)).encode()).hexdigest() != stored_hash:
                    return False
                if hashlib.sha256(str(body).encode()).hexdigest() != str(body_hash).strip():
                    return False
                previous = stored_hash
        return True

    def claim_decision(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 120,
        max_attempts: int = 3,
    ) -> Mapping[str, Any] | None:
        if not worker_id.strip() or lease_seconds < 10 or max_attempts < 1:
            raise ValueError("worker/lease is invalid")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        lease = current + timedelta(seconds=lease_seconds)
        token = secrets.token_urlsafe(32)
        with self.transaction() as cursor:
            cursor.execute(
                """SELECT decision_id,attempt_count FROM v2_decisions
                   WHERE state='CLAIMED' AND lease_expires_at<%s
                     AND attempt_count>=%s FOR UPDATE""",
                (current, max_attempts),
            )
            for decision_id, attempt_count in cursor.fetchall():
                cursor.execute(
                    """UPDATE v2_decisions SET state='FAILED_TERMINAL',
                       claimed_by=NULL,claim_token=NULL,lease_expires_at=NULL,
                       applied_effect=%s::jsonb,updated_at=%s WHERE decision_id=%s""",
                    (
                        canonical_json(
                            {
                                "reason": "apply_lease_exhausted",
                                "attempt": int(attempt_count),
                            }
                        ),
                        current,
                        decision_id,
                    ),
                )
                key = f"decision-dead-letter:{decision_id}:{int(attempt_count)}"
                self.append_event_tx(
                    cursor,
                    DomainEvent(
                        event_id="evt-" + hashlib.sha256(key.encode()).hexdigest()[:24],
                        event_type="DecisionDeadLettered",
                        schema_version=SCHEMA_VERSION,
                        event_time=current,
                        processing_time=current,
                        idempotency_key=key,
                        causation_id=str(decision_id),
                        correlation_id=str(decision_id),
                        payload={
                            "decision_id": str(decision_id),
                            "reason": "apply_lease_exhausted",
                            "attempt": int(attempt_count),
                        },
                    ),
                )
            cursor.execute(
                """UPDATE v2_decisions SET state='DECIDED',claimed_by=NULL,claim_token=NULL,
                   lease_expires_at=NULL,updated_at=%s
                   WHERE state='CLAIMED' AND lease_expires_at<%s AND attempt_count<%s""",
                (current, current, max_attempts),
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
                     AND attempt_count<%s
                   ORDER BY created_at,decision_id
                   FOR UPDATE SKIP LOCKED LIMIT 1""",
                (current, max_attempts),
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
        current = (now or datetime.now(UTC)).astimezone(UTC)
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

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .ai_v2 import AIIntentOutputV2, AIRole, DecisionPacketV2
from .codec_v2 import decode_dataclass
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .roles_v2 import parse_role_output


class PostgresAIPacketQueueV2:
    def __init__(self, store: PostgresRuntimeStoreV2) -> None:
        self.store = store
        self.store.require_schema()

    def migrate(self) -> None:
        with self.store.transaction() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS v2_ai_packets(
                  packet_id TEXT PRIMARY KEY,
                  packet_hash CHAR(64) NOT NULL UNIQUE CHECK(packet_hash ~ '^[0-9a-f]{64}$'),
                  packet JSONB NOT NULL,
                  role TEXT NOT NULL,
                  lane TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN ('PENDING','CLAIMED','DECIDED','ERROR','EXPIRED','APPLIED')),
                  created_at TIMESTAMPTZ NOT NULL,
                  expires_at TIMESTAMPTZ NOT NULL,
                  claimed_by TEXT,
                  claim_token TEXT,
                  lease_expires_at TIMESTAMPTZ,
                  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
                  output JSONB,
                  model TEXT,
                  prompt_hash CHAR(64),
                  updated_at TIMESTAMPTZ NOT NULL
                );
                CREATE INDEX IF NOT EXISTS v2_ai_packets_claim_idx
                  ON v2_ai_packets(state,expires_at,lease_expires_at,created_at);
                CREATE TABLE IF NOT EXISTS v2_ai_runs(
                  run_id TEXT PRIMARY KEY,
                  packet_id TEXT NOT NULL REFERENCES v2_ai_packets(packet_id) ON DELETE RESTRICT,
                  role TEXT NOT NULL,
                  lane TEXT NOT NULL,
                  model TEXT NOT NULL,
                  prompt_hash CHAR(64) NOT NULL,
                  output_hash CHAR(64),
                  status TEXT NOT NULL CHECK(status IN ('COMPLETED','ERROR')),
                  input_tokens INTEGER,
                  output_tokens INTEGER,
                  reasoning_tokens INTEGER,
                  latency_ms INTEGER NOT NULL CHECK(latency_ms>=0),
                  error_type TEXT,
                  created_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS v2_ai_budget_claims(
                  day DATE NOT NULL,
                  role TEXT NOT NULL,
                  lane TEXT NOT NULL,
                  claim_key TEXT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL,
                  PRIMARY KEY(day,role,lane,claim_key)
                );
                """,
                prepare=False,
            )

    def queue(self, packet: DecisionPacketV2, role: AIRole) -> bool:
        created = datetime.fromisoformat(packet.created_at.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(packet.expires_at.replace("Z", "+00:00"))
        if expires <= created:
            raise ValueError("AI packet expiry is invalid")
        with self.store.transaction() as cursor:
            cursor.execute(
                """INSERT INTO v2_ai_packets(packet_id,packet_hash,packet,role,lane,state,created_at,expires_at,updated_at)
                   VALUES(%s,%s,%s::jsonb,%s,%s,'PENDING',%s,%s,%s)
                   ON CONFLICT(packet_id) DO NOTHING""",
                (
                    packet.packet_id,
                    packet.packet_hash,
                    packet.canonical(),
                    role.value,
                    packet.lane,
                    created,
                    expires,
                    created,
                ),
            )
            created_row = cursor.rowcount == 1
            if not created_row:
                cursor.execute(
                    "SELECT packet_hash FROM v2_ai_packets WHERE packet_id=%s", (packet.packet_id,)
                )
                row = cursor.fetchone()
                if row is None or str(row[0]).strip() != packet.packet_hash:
                    raise ValueError("AI packet identifier cannot be rebound")
            return created_row

    def claim(
        self,
        worker_id: str,
        role: AIRole,
        *,
        now: datetime,
        lease_seconds: int = 300,
        daily_cap: int | None = None,
    ) -> Mapping[str, Any] | None:
        current = now.astimezone(UTC)
        if not worker_id.strip() or lease_seconds < 30 or (daily_cap is not None and daily_cap < 1):
            raise ValueError("AI claim arguments are invalid")
        lease = current + timedelta(seconds=lease_seconds)
        with self.store.transaction() as cursor:
            cursor.execute(
                """UPDATE v2_ai_packets SET state='PENDING',claimed_by=NULL,claim_token=NULL,
                   lease_expires_at=NULL,updated_at=%s WHERE state='CLAIMED' AND lease_expires_at<%s""",
                (current, current),
            )
            cursor.execute(
                """UPDATE v2_ai_packets SET state='EXPIRED',updated_at=%s
                   WHERE state IN ('PENDING','ERROR') AND expires_at<%s""",
                (current, current),
            )
            if daily_cap is not None:
                cursor.execute(
                    "SELECT COUNT(*) FROM v2_ai_budget_claims WHERE day=%s AND role=%s",
                    (current.date(), role.value),
                )
                if int(cursor.fetchone()[0]) >= daily_cap:
                    return None
            cursor.execute(
                """SELECT packet_id,packet_hash,packet,lane,attempt_count,expires_at
                   FROM v2_ai_packets WHERE role=%s AND state IN ('PENDING','ERROR') AND expires_at>=%s
                   ORDER BY created_at,packet_id FOR UPDATE SKIP LOCKED LIMIT 1""",
                (role.value, current),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            attempt = int(row[4]) + 1
            claim_key = f"{row[0]}:{attempt}"
            if daily_cap is not None:
                cursor.execute(
                    "INSERT INTO v2_ai_budget_claims(day,role,lane,claim_key,created_at) VALUES(%s,%s,%s,%s,%s)",
                    (current.date(), role.value, str(row[3]), claim_key, current),
                )
            cursor.execute(
                """UPDATE v2_ai_packets SET state='CLAIMED',claimed_by=%s,claim_token=%s,
                   lease_expires_at=%s,attempt_count=%s,updated_at=%s WHERE packet_id=%s""",
                (worker_id, token, lease, attempt, current, row[0]),
            )
            packet = self.store._mapping(row[2])
            return {
                "packet_id": str(row[0]),
                "packet_hash": str(row[1]).strip(),
                "packet": dict(packet),
                "role": role.value,
                "lane": str(row[3]),
                "attempt": attempt,
                "claim_token": token,
                "expires_at": row[5].isoformat(),
            }

    @staticmethod
    def _packet(value: Mapping[str, Any]) -> DecisionPacketV2:
        return decode_dataclass(DecisionPacketV2, value)

    def submit(
        self,
        packet_id: str,
        claim_token: str,
        output: Mapping[str, Any],
        *,
        model: str,
        prompt_hash: str,
        run: Mapping[str, Any],
        now: datetime,
    ) -> object:
        current = now.astimezone(UTC)
        with self.store.connection.cursor() as cursor:
            cursor.execute(
                "SELECT packet,role,lane,state,claim_token,lease_expires_at FROM v2_ai_packets WHERE packet_id=%s",
                (packet_id,),
            )
            row = cursor.fetchone()
        if (
            row is None
            or str(row[3]) != "CLAIMED"
            or not secrets.compare_digest(str(row[4]), claim_token)
        ):
            raise PermissionError("AI packet claim is not active")
        if row[5] is None or row[5] < current:
            raise PermissionError("AI packet lease expired")
        packet = self._packet(self.store._mapping(row[0]))
        role = AIRole(str(row[1]))
        if role is AIRole.PORTFOLIO_DECIDER:
            decision = AIIntentOutputV2.from_mapping(output)
            decision.validate(packet)
        else:
            decision = parse_role_output(role, output, packet)
        output_json = canonical_json_safe(decision)
        output_hash = hashlib.sha256(output_json.encode()).hexdigest()
        required = {
            "run_id",
            "status",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "error_type",
        }
        if set(run) != required or str(run["status"]) != "COMPLETED":
            raise ValueError("AI run telemetry is invalid")
        with self.store.transaction() as cursor:
            cursor.execute(
                """INSERT INTO v2_ai_runs(run_id,packet_id,role,lane,model,prompt_hash,output_hash,status,
                   input_tokens,output_tokens,reasoning_tokens,latency_ms,error_type,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,'COMPLETED',%s,%s,%s,%s,%s,%s)""",
                (
                    str(run["run_id"]),
                    packet_id,
                    role.value,
                    str(row[2]),
                    model,
                    prompt_hash,
                    output_hash,
                    run["input_tokens"],
                    run["output_tokens"],
                    run["reasoning_tokens"],
                    int(run["latency_ms"]),
                    run["error_type"],
                    current,
                ),
            )
            cursor.execute(
                """UPDATE v2_ai_packets SET state='DECIDED',output=%s::jsonb,model=%s,prompt_hash=%s,
                   claim_token=NULL,lease_expires_at=NULL,updated_at=%s
                   WHERE packet_id=%s AND state='CLAIMED' AND claim_token=%s""",
                (output_json, model, prompt_hash, current, packet_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise PermissionError("AI packet claim was lost")
        return decision

    def fail(
        self,
        packet_id: str,
        claim_token: str,
        *,
        model: str,
        prompt_hash: str,
        run: Mapping[str, Any],
        retryable: bool,
        now: datetime,
    ) -> None:
        current = now.astimezone(UTC)
        required = {
            "run_id",
            "status",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "error_type",
        }
        if set(run) != required or str(run["status"]) != "ERROR":
            raise ValueError("AI error telemetry is invalid")
        with self.store.transaction() as cursor:
            cursor.execute(
                "SELECT role,lane,state,claim_token FROM v2_ai_packets WHERE packet_id=%s FOR UPDATE",
                (packet_id,),
            )
            row = cursor.fetchone()
            if (
                row is None
                or str(row[2]) != "CLAIMED"
                or not secrets.compare_digest(str(row[3]), claim_token)
            ):
                raise PermissionError("AI packet claim is not active")
            cursor.execute(
                """INSERT INTO v2_ai_runs(run_id,packet_id,role,lane,model,prompt_hash,output_hash,status,
                   input_tokens,output_tokens,reasoning_tokens,latency_ms,error_type,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,NULL,'ERROR',%s,%s,%s,%s,%s,%s)""",
                (
                    str(run["run_id"]),
                    packet_id,
                    str(row[0]),
                    str(row[1]),
                    model,
                    prompt_hash,
                    run["input_tokens"],
                    run["output_tokens"],
                    run["reasoning_tokens"],
                    int(run["latency_ms"]),
                    run["error_type"],
                    current,
                ),
            )
            cursor.execute(
                """UPDATE v2_ai_packets SET state=%s,claim_token=NULL,lease_expires_at=NULL,updated_at=%s
                   WHERE packet_id=%s""",
                ("ERROR" if retryable else "EXPIRED", current, packet_id),
            )

    def decided(self, limit: int = 20) -> tuple[Mapping[str, Any], ...]:
        with self.store.connection.cursor() as cursor:
            cursor.execute(
                """SELECT packet_id,packet_hash,packet,role,lane,output,model,prompt_hash,updated_at
                   FROM v2_ai_packets WHERE state='DECIDED' ORDER BY updated_at LIMIT %s""",
                (max(1, min(limit, 100)),),
            )
            rows = cursor.fetchall()
        return tuple(
            {
                "packet_id": str(row[0]),
                "packet_hash": str(row[1]).strip(),
                "packet": dict(self.store._mapping(row[2])),
                "role": str(row[3]),
                "lane": str(row[4]),
                "output": dict(self.store._mapping(row[5])),
                "model": str(row[6]),
                "prompt_hash": str(row[7]).strip(),
                "updated_at": row[8].isoformat(),
            }
            for row in rows
        )

    def mark_applied(self, packet_id: str, *, now: datetime) -> None:
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE v2_ai_packets SET state='APPLIED',updated_at=%s WHERE packet_id=%s AND state='DECIDED'",
                (now.astimezone(UTC), packet_id),
            )
            if cursor.rowcount != 1:
                raise PermissionError("AI decision is not ready for application")


def canonical_json_safe(value: object) -> str:
    from dataclasses import asdict, is_dataclass

    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

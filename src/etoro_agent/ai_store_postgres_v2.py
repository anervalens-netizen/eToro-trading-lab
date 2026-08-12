from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .ai_v2 import AIIntentOutputV2, AIRole, DecisionPacketV2
from .codec_v2 import decode_dataclass
from .domain_v2 import DomainEvent
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .roles_v2 import parse_role_output

AUTHORITY_SHADOW = "SHADOW"
AUTHORITY_EXECUTION = "EXECUTION"


def _json(value: object) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class CanonicalPostgresAIStoreV2:
    """Canonical production AI queue: immutable packets, leases, budget claims and run telemetry."""

    def __init__(self, store: PostgresRuntimeStoreV2) -> None:
        self.store = store
        self.store.require_schema()

    def migrate(self) -> None:
        raise RuntimeError("AI schema changes must use the canonical v2 migration runner")

    def _dead_letter_event(
        self,
        cursor: Any,
        packet_id: str,
        stage: str,
        reason: str,
        attempt: int,
        current: datetime,
    ) -> None:
        key = f"ai-dead-letter:{packet_id}:{stage}:{attempt}"
        self.store.append_event_tx(
            cursor,
            DomainEvent(
                event_id="evt-" + hashlib.sha256(key.encode()).hexdigest()[:24],
                event_type="AIPacketDeadLettered",
                schema_version=4,
                event_time=current,
                processing_time=current,
                idempotency_key=key,
                causation_id=packet_id,
                correlation_id=packet_id,
                payload={
                    "packet_id": packet_id,
                    "stage": stage,
                    "reason": reason[:200],
                    "attempt": attempt,
                },
            ),
        )

    def _authority_expired_event(
        self,
        cursor: Any,
        packet_id: str,
        authority_mode: str,
        execution_epoch: int | None,
        current: datetime,
    ) -> None:
        key = f"ai-authority-expired:{packet_id}:{authority_mode}:{execution_epoch}"
        self.store.append_event_tx(
            cursor,
            DomainEvent(
                event_id="evt-" + hashlib.sha256(key.encode()).hexdigest()[:24],
                event_type="AIPacketAuthorityExpired",
                schema_version=6,
                event_time=current,
                processing_time=current,
                idempotency_key=key,
                causation_id=packet_id,
                correlation_id=packet_id,
                payload={
                    "packet_id": packet_id,
                    "required_authority_mode": authority_mode,
                    "required_execution_epoch": execution_epoch,
                    "broker_write": False,
                },
            ),
        )

    @staticmethod
    def _validate_authority(authority_mode: str, execution_epoch: int | None) -> None:
        if authority_mode == AUTHORITY_SHADOW and execution_epoch is None:
            return
        if (
            authority_mode == AUTHORITY_EXECUTION
            and execution_epoch is not None
            and execution_epoch >= 1
        ):
            return
        raise ValueError("AI packet authority mode/epoch is invalid")

    @staticmethod
    def _authority_matches_state(
        state: str,
        version: int,
        authority_mode: str,
        execution_epoch: int | None,
    ) -> bool:
        if authority_mode == AUTHORITY_SHADOW:
            return state == "LOCKED" and execution_epoch is None
        return (
            authority_mode == AUTHORITY_EXECUTION
            and state == "ACTIVE"
            and execution_epoch == version
        )

    def queue(
        self,
        packet: DecisionPacketV2,
        role: AIRole,
        *,
        authority_mode: str = AUTHORITY_SHADOW,
        execution_epoch: int | None = None,
    ) -> bool:
        self._validate_authority(authority_mode, execution_epoch)
        created = datetime.fromisoformat(packet.created_at.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(packet.expires_at.replace("Z", "+00:00"))
        if expires <= created:
            raise ValueError("AI packet expiry is invalid")
        with self.store.transaction() as cursor:
            cursor.execute(
                "SELECT state,version FROM v2_trading_state WHERE singleton=TRUE FOR SHARE"
            )
            state_row = cursor.fetchone()
            if state_row is None:
                raise RuntimeError("trading state singleton is missing")
            if not self._authority_matches_state(
                str(state_row[0]),
                int(state_row[1]),
                authority_mode,
                execution_epoch,
            ):
                raise PermissionError("AI packet authority epoch is no longer current")
            cursor.execute(
                """INSERT INTO v2_ai_packets(
                     packet_id,packet_hash,packet,role,lane,state,created_at,expires_at,updated_at,
                     authority_mode,execution_epoch)
                   VALUES(%s,%s,%s::jsonb,%s,%s,'PENDING',%s,%s,%s,%s,%s)
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
                    authority_mode,
                    execution_epoch,
                ),
            )
            created_row = cursor.rowcount == 1
            if not created_row:
                cursor.execute(
                    """SELECT packet_hash,packet,role,lane,authority_mode,execution_epoch
                       FROM v2_ai_packets
                       WHERE packet_id=%s""",
                    (packet.packet_id,),
                )
                row = cursor.fetchone()
                if (
                    row is None
                    or str(row[0]).strip() != packet.packet_hash
                    or dict(self.store._mapping(row[1])) != json.loads(packet.canonical())
                    or str(row[2]) != role.value
                    or str(row[3]) != packet.lane
                    or str(row[4]) != authority_mode
                    or (None if row[5] is None else int(row[5])) != execution_epoch
                ):
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
        max_attempts: int = 3,
    ) -> Mapping[str, Any] | None:
        current = now.astimezone(UTC)
        if (
            not worker_id.strip()
            or lease_seconds < 30
            or (daily_cap is not None and daily_cap < 1)
            or max_attempts < 1
        ):
            raise ValueError("AI claim arguments are invalid")
        lease = current + timedelta(seconds=lease_seconds)
        with self.store.transaction() as cursor:
            cursor.execute(
                """SELECT packet_id,attempt_count FROM v2_ai_packets
                   WHERE state='CLAIMED' AND lease_expires_at<%s AND attempt_count>=%s
                   FOR UPDATE""",
                (current, max_attempts),
            )
            for packet_id, attempt_count in cursor.fetchall():
                cursor.execute(
                    """UPDATE v2_ai_packets SET state='DEAD_LETTER',claimed_by=NULL,
                       claim_token=NULL,lease_expires_at=NULL,
                       terminal_reason='inference_lease_exhausted',dead_lettered_at=%s,
                       updated_at=%s WHERE packet_id=%s""",
                    (current, current, packet_id),
                )
                self._dead_letter_event(
                    cursor,
                    str(packet_id),
                    "inference",
                    "inference_lease_exhausted",
                    int(attempt_count),
                    current,
                )
            cursor.execute(
                "UPDATE v2_ai_packets SET state='PENDING',claimed_by=NULL,claim_token=NULL,lease_expires_at=NULL,updated_at=%s WHERE state='CLAIMED' AND lease_expires_at<%s",
                (current, current),
            )
            cursor.execute(
                """UPDATE v2_ai_packets SET state='DEAD_LETTER',
                   claimed_by=NULL,claim_token=NULL,lease_expires_at=NULL,
                   terminal_reason='inference_retry_exhausted',dead_lettered_at=%s,
                   updated_at=%s WHERE state='ERROR' AND attempt_count>=%s""",
                (current, current, max_attempts),
            )
            cursor.execute(
                "UPDATE v2_ai_packets SET state='EXPIRED',claimed_by=NULL,claim_token=NULL,lease_expires_at=NULL,updated_at=%s WHERE state IN ('PENDING','ERROR') AND expires_at<%s",
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
                   FROM v2_ai_packets WHERE role=%s AND state IN ('PENDING','ERROR')
                     AND expires_at>=%s AND attempt_count<%s
                   ORDER BY created_at,packet_id FOR UPDATE SKIP LOCKED LIMIT 1""",
                (role.value, current, max_attempts),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            attempt = int(row[4]) + 1
            if daily_cap is not None:
                cursor.execute(
                    "INSERT INTO v2_ai_budget_claims(day,role,lane,claim_key,created_at) VALUES(%s,%s,%s,%s,%s)",
                    (current.date(), role.value, str(row[3]), f"{row[0]}:{attempt}", current),
                )
            cursor.execute(
                "UPDATE v2_ai_packets SET state='CLAIMED',claimed_by=%s,claim_token=%s,lease_expires_at=%s,attempt_count=%s,updated_at=%s WHERE packet_id=%s",
                (worker_id, token, lease, attempt, current, row[0]),
            )
            return {
                "packet_id": str(row[0]),
                "packet_hash": str(row[1]).strip(),
                "packet": dict(self.store._mapping(row[2])),
                "role": role.value,
                "lane": str(row[3]),
                "attempt": attempt,
                "claim_token": token,
                "expires_at": row[5].isoformat(),
            }

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
        packet = decode_dataclass(DecisionPacketV2, self.store._mapping(row[0]))
        role = AIRole(str(row[1]))
        if role is AIRole.PORTFOLIO_DECIDER:
            decision = decode_dataclass(AIIntentOutputV2, output)
            decision.validate(packet)
        else:
            decision = parse_role_output(role, output, packet)
        output_json = _json(decision)
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
                   claimed_by=NULL,claim_token=NULL,lease_expires_at=NULL,updated_at=%s
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
        max_attempts: int = 3,
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
        if max_attempts < 1:
            raise ValueError("max attempts must be positive")
        with self.store.transaction() as cursor:
            cursor.execute(
                """SELECT role,lane,state,claim_token,attempt_count
                   FROM v2_ai_packets WHERE packet_id=%s FOR UPDATE""",
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
            attempts = int(row[4])
            if retryable and attempts < max_attempts:
                cursor.execute(
                    """UPDATE v2_ai_packets SET state='ERROR',claimed_by=NULL,claim_token=NULL,
                       lease_expires_at=NULL,updated_at=%s WHERE packet_id=%s""",
                    (current, packet_id),
                )
            else:
                reason = (
                    "inference_retry_exhausted"
                    if retryable
                    else f"inference_terminal:{str(run['error_type'] or 'unknown')}"
                )
                cursor.execute(
                    """UPDATE v2_ai_packets SET state='DEAD_LETTER',claimed_by=NULL,claim_token=NULL,
                       lease_expires_at=NULL,terminal_reason=%s,dead_lettered_at=%s,
                       updated_at=%s WHERE packet_id=%s""",
                    (reason[:200], current, current, packet_id),
                )
                self._dead_letter_event(cursor, packet_id, "inference", reason, attempts, current)

    def decided(self, limit: int = 20) -> tuple[Mapping[str, Any], ...]:
        with self.store.connection.cursor() as cursor:
            cursor.execute(
                """SELECT packet_id,packet_hash,packet,role,lane,output,model,prompt_hash,
                          updated_at,authority_mode,execution_epoch
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
                "authority_mode": str(row[9]),
                "execution_epoch": None if row[10] is None else int(row[10]),
            }
            for row in rows
        )

    def claim_decided(
        self,
        worker_id: str,
        role: AIRole,
        *,
        now: datetime,
        lease_seconds: int = 120,
        max_attempts: int = 3,
        authority_mode: str | None = None,
        execution_epoch: int | None = None,
    ) -> Mapping[str, Any] | None:
        if not worker_id.strip() or lease_seconds < 10 or max_attempts < 1:
            raise ValueError("decision apply worker/lease is invalid")
        if authority_mode is None:
            if execution_epoch is not None:
                raise ValueError("execution epoch requires an authority mode")
        else:
            self._validate_authority(authority_mode, execution_epoch)
        current = now.astimezone(UTC)
        lease = current + timedelta(seconds=lease_seconds)
        token = secrets.token_urlsafe(32)
        with self.store.transaction() as cursor:
            if authority_mode is not None:
                cursor.execute(
                    "SELECT state,version FROM v2_trading_state WHERE singleton=TRUE FOR SHARE"
                )
                state_row = cursor.fetchone()
                if state_row is None:
                    raise RuntimeError("trading state singleton is missing")
                if not self._authority_matches_state(
                    str(state_row[0]),
                    int(state_row[1]),
                    authority_mode,
                    execution_epoch,
                ):
                    return None
                cursor.execute(
                    """SELECT packet_id FROM v2_ai_packets
                       WHERE state='DECIDED' AND role=%s
                         AND NOT (
                           authority_mode=%s
                           AND execution_epoch IS NOT DISTINCT FROM %s
                         )
                       FOR UPDATE""",
                    (role.value, authority_mode, execution_epoch),
                )
                stale_packets = tuple(str(row[0]) for row in cursor.fetchall())
                for stale_packet_id in stale_packets:
                    cursor.execute(
                        """UPDATE v2_ai_packets SET state='EXPIRED',
                           apply_claimed_by=NULL,apply_claim_token=NULL,
                           apply_lease_expires_at=NULL,
                           terminal_reason='authority_epoch_closed',updated_at=%s
                           WHERE packet_id=%s AND state='DECIDED'""",
                        (current, stale_packet_id),
                    )
                    if cursor.rowcount == 1:
                        self._authority_expired_event(
                            cursor,
                            stale_packet_id,
                            authority_mode,
                            execution_epoch,
                            current,
                        )
            cursor.execute(
                """SELECT packet_id,apply_attempt_count FROM v2_ai_packets
                   WHERE state='DECIDED' AND apply_lease_expires_at<%s
                     AND apply_attempt_count>=%s FOR UPDATE""",
                (current, max_attempts),
            )
            for packet_id, attempt_count in cursor.fetchall():
                cursor.execute(
                    """UPDATE v2_ai_packets SET state='DEAD_LETTER',
                       apply_claimed_by=NULL,apply_claim_token=NULL,
                       apply_lease_expires_at=NULL,
                       terminal_reason='apply_lease_exhausted',dead_lettered_at=%s,
                       updated_at=%s WHERE packet_id=%s""",
                    (current, current, packet_id),
                )
                self._dead_letter_event(
                    cursor,
                    str(packet_id),
                    "apply",
                    "apply_lease_exhausted",
                    int(attempt_count),
                    current,
                )
            cursor.execute(
                """SELECT packet_id,packet_hash,packet,role,lane,output,model,
                          prompt_hash,updated_at,apply_attempt_count,
                          authority_mode,execution_epoch
                   FROM v2_ai_packets
                   WHERE state='DECIDED' AND role=%s
                     AND apply_attempt_count<%s
                     AND (apply_lease_expires_at IS NULL OR apply_lease_expires_at<%s)
                     AND (%s::text IS NULL OR (
                       authority_mode=%s
                       AND execution_epoch IS NOT DISTINCT FROM %s
                     ))
                   ORDER BY updated_at,packet_id
                   FOR UPDATE SKIP LOCKED LIMIT 1""",
                (
                    role.value,
                    max_attempts,
                    current,
                    authority_mode,
                    authority_mode,
                    execution_epoch,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            attempt = int(row[9]) + 1
            cursor.execute(
                """UPDATE v2_ai_packets SET apply_claimed_by=%s,
                   apply_claim_token=%s,apply_lease_expires_at=%s,
                   apply_attempt_count=%s,updated_at=%s
                   WHERE packet_id=%s AND state='DECIDED'""",
                (worker_id, token, lease, attempt, current, row[0]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("decision apply claim race")
            return {
                "packet_id": str(row[0]),
                "packet_hash": str(row[1]).strip(),
                "packet": dict(self.store._mapping(row[2])),
                "role": str(row[3]),
                "lane": str(row[4]),
                "output": dict(self.store._mapping(row[5])),
                "model": str(row[6]),
                "prompt_hash": str(row[7]).strip(),
                "updated_at": row[8].isoformat(),
                "apply_attempt": attempt,
                "apply_claim_token": token,
                "authority_mode": str(row[10]),
                "execution_epoch": None if row[11] is None else int(row[11]),
            }

    def mark_applied(
        self,
        packet_id: str,
        claim_token: str,
        effect: Mapping[str, Any],
        *,
        now: datetime,
    ) -> None:
        with self.store.transaction() as cursor:
            cursor.execute(
                """UPDATE v2_ai_packets SET state='APPLIED',applied_effect=%s::jsonb,
                   apply_claimed_by=NULL,apply_claim_token=NULL,
                   apply_lease_expires_at=NULL,updated_at=%s
                   WHERE packet_id=%s AND state='DECIDED'
                     AND apply_claim_token=%s AND apply_lease_expires_at>=%s""",
                (
                    _json(dict(effect)),
                    now.astimezone(UTC),
                    packet_id,
                    claim_token,
                    now.astimezone(UTC),
                ),
            )
            if cursor.rowcount != 1:
                raise PermissionError(
                    "AI decision apply claim is absent, expired, or already consumed"
                )

    def release_apply_claim(
        self,
        packet_id: str,
        claim_token: str,
        *,
        now: datetime,
        max_attempts: int = 3,
        reason: str = "apply_error",
    ) -> None:
        with self.store.transaction() as cursor:
            cursor.execute(
                """SELECT apply_attempt_count FROM v2_ai_packets
                   WHERE packet_id=%s AND state='DECIDED' AND apply_claim_token=%s
                   FOR UPDATE""",
                (packet_id, claim_token),
            )
            row = cursor.fetchone()
            if row is None:
                raise PermissionError("AI decision apply claim is not active")
            attempt = int(row[0])
            current = now.astimezone(UTC)
            if attempt >= max_attempts:
                cursor.execute(
                    """UPDATE v2_ai_packets SET state='DEAD_LETTER',
                       apply_claimed_by=NULL,apply_claim_token=NULL,
                       apply_lease_expires_at=NULL,terminal_reason=%s,
                       dead_lettered_at=%s,updated_at=%s WHERE packet_id=%s""",
                    (reason[:200], current, current, packet_id),
                )
                self._dead_letter_event(cursor, packet_id, "apply", reason, attempt, current)
                return
            cursor.execute(
                """UPDATE v2_ai_packets SET apply_claimed_by=NULL,
                   apply_claim_token=NULL,apply_lease_expires_at=NULL,updated_at=%s
                   WHERE packet_id=%s AND state='DECIDED' AND apply_claim_token=%s""",
                (current, packet_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise PermissionError("AI decision apply claim is not active")

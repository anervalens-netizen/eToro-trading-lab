from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from .audit import AuditLog


AI_ACTIONS = frozenset({"OPEN", "CLOSE", "HOLD"})


@dataclass(frozen=True)
class AIDecision:
    packet_id: str
    packet_hash: str
    action: str
    candidate_id: str
    confidence: Decimal
    reason_codes: tuple[str, ...]
    rationale: str
    model: str
    decided_at: str
    payload: dict[str, Any]


class AIDecisionStore:
    """One-time, hash-bound Sol decisions with no broker or risk authority."""

    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self.audit.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_decision_packets (
                packet_id TEXT PRIMARY KEY,
                packet_hash TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                decision_json TEXT,
                decided_at TEXT,
                consumed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS ai_decision_state_idx
                ON ai_decision_packets(state, expires_at, created_at);
            """
        )
        self.audit.db.commit()

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    def queue(self, payload: Mapping[str, Any], expires_at: int) -> tuple[str, str, bool]:
        if expires_at <= int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("AI decision packet must expire in the future")
        payload_json = self._canonical(dict(payload))
        packet_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        packet_id = f"ai-{packet_hash[:24]}"
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.audit.db.execute(
            """
            INSERT OR IGNORE INTO ai_decision_packets(
                packet_id,packet_hash,payload_json,state,created_at,expires_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (packet_id, packet_hash, payload_json, "PENDING", now, expires_at),
        )
        self.audit.db.commit()
        created = cursor.rowcount == 1
        if created:
            self.audit.append(
                "ai_decision_requested",
                {"packet_id": packet_id, "packet_hash": packet_hash, "expires_at": expires_at},
            )
        return packet_id, packet_hash, created

    def pending(self, limit: int = 20) -> tuple[dict[str, Any], ...]:
        now = int(datetime.now(timezone.utc).timestamp())
        rows = self.audit.db.execute(
            """
            SELECT packet_id,packet_hash,payload_json,created_at,expires_at
            FROM ai_decision_packets
            WHERE state='PENDING' AND expires_at>=?
            ORDER BY created_at LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        return tuple(
            {
                "packet_id": str(row[0]),
                "packet_hash": str(row[1]),
                "payload": json.loads(str(row[2])),
                "created_at": str(row[3]),
                "expires_at": int(row[4]),
            }
            for row in rows
        )

    def decide(
        self,
        packet_id: str,
        packet_hash: str,
        action: str,
        candidate_id: str,
        confidence: Decimal,
        reason_codes: tuple[str, ...],
        rationale: str,
        model: str,
    ) -> None:
        normalized = action.strip().upper()
        if normalized not in AI_ACTIONS:
            raise ValueError("AI action must be OPEN, CLOSE, or HOLD")
        if normalized == "OPEN" and not candidate_id.strip():
            raise ValueError("OPEN requires an exact candidate_id")
        if len(candidate_id) > 100:
            raise ValueError("candidate_id is too long")
        if not Decimal("0") <= confidence <= Decimal("1"):
            raise ValueError("AI confidence must be between zero and one")
        if not model.strip() or len(model) > 100:
            raise ValueError("AI model identifier is required")
        if not reason_codes or any(not code or len(code) > 64 for code in reason_codes):
            raise ValueError("at least one bounded AI reason code is required")
        if not rationale.strip() or len(rationale) > 1000:
            raise ValueError("AI rationale must be present and bounded")
        now_dt = datetime.now(timezone.utc)
        decision = {
            "action": normalized,
            "candidate_id": candidate_id.strip(),
            "confidence": str(confidence),
            "reason_codes": sorted(set(reason_codes)),
            "rationale": rationale.strip(),
            "model": model.strip(),
        }
        cursor = self.audit.db.execute(
            """
            UPDATE ai_decision_packets
            SET state='DECIDED',decision_json=?,decided_at=?
            WHERE packet_id=? AND packet_hash=? AND state='PENDING' AND expires_at>=?
            """,
            (
                self._canonical(decision),
                now_dt.isoformat(),
                packet_id,
                packet_hash,
                int(now_dt.timestamp()),
            ),
        )
        self.audit.db.commit()
        if cursor.rowcount != 1:
            raise PermissionError("AI packet missing, expired, hash-mismatched, or already decided")
        self.audit.append(
            "ai_decision_recorded",
            {"packet_id": packet_id, "packet_hash": packet_hash, **decision},
        )

    def consume_ready(self, limit: int = 20) -> tuple[AIDecision, ...]:
        now_dt = datetime.now(timezone.utc)
        rows = self.audit.db.execute(
            """
            SELECT packet_id,packet_hash,payload_json,decision_json,decided_at
            FROM ai_decision_packets
            WHERE state='DECIDED' AND expires_at>=?
            ORDER BY decided_at LIMIT ?
            """,
            (int(now_dt.timestamp()), limit),
        ).fetchall()
        decisions: list[AIDecision] = []
        for row in rows:
            raw = json.loads(str(row[3]))
            cursor = self.audit.db.execute(
                """
                UPDATE ai_decision_packets SET state='CONSUMED',consumed_at=?
                WHERE packet_id=? AND state='DECIDED'
                """,
                (now_dt.isoformat(), str(row[0])),
            )
            if cursor.rowcount != 1:
                continue
            decisions.append(
                AIDecision(
                    packet_id=str(row[0]),
                    packet_hash=str(row[1]),
                    action=str(raw["action"]),
                    candidate_id=str(raw.get("candidate_id", "")),
                    confidence=Decimal(str(raw["confidence"])),
                    reason_codes=tuple(str(value) for value in raw["reason_codes"]),
                    rationale=str(raw["rationale"]),
                    model=str(raw["model"]),
                    decided_at=str(row[4]),
                    payload=json.loads(str(row[2])),
                )
            )
        self.audit.db.commit()
        for decision in decisions:
            self.audit.append(
                "ai_decision_consumed",
                {
                    "packet_id": decision.packet_id,
                    "packet_hash": decision.packet_hash,
                    "action": decision.action,
                },
            )
        return tuple(decisions)

    def expire_pending(self) -> int:
        now = int(datetime.now(timezone.utc).timestamp())
        cursor = self.audit.db.execute(
            "UPDATE ai_decision_packets SET state='EXPIRED' WHERE state='PENDING' AND expires_at<?",
            (now,),
        )
        self.audit.db.commit()
        return int(cursor.rowcount)

    def invalidate_active(self, reason: str) -> int:
        if not reason.strip() or len(reason) > 200:
            raise ValueError("AI invalidation reason is required and bounded")
        cursor = self.audit.db.execute(
            "UPDATE ai_decision_packets SET state='INVALIDATED' "
            "WHERE state IN ('PENDING','DECIDED')",
        )
        self.audit.db.commit()
        count = int(cursor.rowcount)
        if count:
            self.audit.append(
                "ai_decision_packets_invalidated",
                {"count": count, "reason": reason.strip()},
            )
        return count

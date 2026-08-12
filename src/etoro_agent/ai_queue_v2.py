from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .ai_v2 import AIAction, AIIntentOutputV2, DecisionPacketV2


class AIPacketQueueV2:
    """Crash-safe stateless-model queue. Packets are immutable; model claims are leased."""

    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(Path(path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_packets_v2(
              packet_id TEXT PRIMARY KEY,
              packet_hash TEXT NOT NULL UNIQUE,
              packet_json TEXT NOT NULL,
              role TEXT NOT NULL,
              lane TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN (
                'PENDING','CLAIMED','DECIDED','ERROR','EXPIRED','DEAD_LETTER'
              )),
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              claimed_by TEXT,
              claim_token TEXT,
              lease_expires_at TEXT,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              decision_json TEXT,
              model TEXT,
              prompt_hash TEXT,
              decided_at TEXT,
              terminal_reason TEXT,
              dead_lettered_at TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ai_packets_v2_claim_idx
              ON ai_packets_v2(state,expires_at,lease_expires_at,created_at);
            CREATE TABLE IF NOT EXISTS ai_runs_v2(
              run_id TEXT PRIMARY KEY,
              packet_id TEXT NOT NULL REFERENCES ai_packets_v2(packet_id),
              role TEXT NOT NULL,
              lane TEXT NOT NULL,
              model TEXT NOT NULL,
              prompt_hash TEXT NOT NULL,
              output_hash TEXT,
              status TEXT NOT NULL CHECK(status IN ('COMPLETED','ERROR')),
              input_tokens INTEGER,
              output_tokens INTEGER,
              reasoning_tokens INTEGER,
              latency_ms INTEGER NOT NULL,
              error_type TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_daily_budget_v2(
              day TEXT NOT NULL,
              role TEXT NOT NULL,
              lane TEXT NOT NULL,
              claim_key TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(day,role,lane,claim_key)
            );
            """
        )
        self._ensure_dead_letter_schema()
        self._normalize_lease_state()

    def _normalize_lease_state(self) -> None:
        """Repair lease metadata written by queue versions predating strict invariants."""

        current = datetime.now(UTC).isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                """UPDATE ai_packets_v2 SET state='DEAD_LETTER',claimed_by=NULL,
                   claim_token=NULL,lease_expires_at=NULL,
                   terminal_reason=coalesce(terminal_reason,'invalid_inference_lease_migrated'),
                   dead_lettered_at=coalesce(dead_lettered_at,?),updated_at=?
                   WHERE state='CLAIMED'
                     AND (claimed_by IS NULL OR claim_token IS NULL OR lease_expires_at IS NULL)""",
                (current, current),
            )
            self.db.execute(
                """UPDATE ai_packets_v2 SET claimed_by=NULL,claim_token=NULL,
                   lease_expires_at=NULL WHERE state<>'CLAIMED'"""
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _ensure_dead_letter_schema(self) -> None:
        row = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_packets_v2'"
        ).fetchone()
        if row is None or "DEAD_LETTER" in str(row[0]):
            return
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.execute("PRAGMA legacy_alter_table=ON")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("ALTER TABLE ai_packets_v2 RENAME TO ai_packets_v2_legacy")
            self.db.execute(
                """CREATE TABLE ai_packets_v2(
                  packet_id TEXT PRIMARY KEY,
                  packet_hash TEXT NOT NULL UNIQUE,
                  packet_json TEXT NOT NULL,
                  role TEXT NOT NULL,
                  lane TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN (
                    'PENDING','CLAIMED','DECIDED','ERROR','EXPIRED','DEAD_LETTER'
                  )),
                  created_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  claimed_by TEXT,
                  claim_token TEXT,
                  lease_expires_at TEXT,
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  decision_json TEXT,
                  model TEXT,
                  prompt_hash TEXT,
                  decided_at TEXT,
                  terminal_reason TEXT,
                  dead_lettered_at TEXT,
                  updated_at TEXT NOT NULL
                )"""
            )
            self.db.execute(
                """INSERT INTO ai_packets_v2(
                   packet_id,packet_hash,packet_json,role,lane,state,created_at,expires_at,
                   claimed_by,claim_token,lease_expires_at,attempt_count,decision_json,
                   model,prompt_hash,decided_at,terminal_reason,dead_lettered_at,updated_at
                   ) SELECT packet_id,packet_hash,packet_json,role,lane,state,created_at,
                   expires_at,claimed_by,claim_token,lease_expires_at,attempt_count,
                   decision_json,model,prompt_hash,decided_at,NULL,NULL,updated_at
                   FROM ai_packets_v2_legacy"""
            )
            self.db.execute("DROP TABLE ai_packets_v2_legacy")
            self.db.execute(
                """CREATE INDEX ai_packets_v2_claim_idx
                   ON ai_packets_v2(state,expires_at,lease_expires_at,created_at)"""
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        finally:
            self.db.execute("PRAGMA legacy_alter_table=OFF")
            self.db.execute("PRAGMA foreign_keys=ON")
        if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("AI queue migration violated a foreign key")

    def _dead_letter(self, packet_id: str, reason: str, current: datetime) -> None:
        self.db.execute(
            """UPDATE ai_packets_v2 SET state='DEAD_LETTER',claimed_by=NULL,
               claim_token=NULL,lease_expires_at=NULL,terminal_reason=?,
               dead_lettered_at=?,updated_at=? WHERE packet_id=?""",
            (reason[:200], current.isoformat(), current.isoformat(), packet_id),
        )

    @staticmethod
    def _now(value: datetime | None = None) -> datetime:
        result = value or datetime.now(UTC)
        if result.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return result.astimezone(UTC)

    def queue(self, packet: DecisionPacketV2, role: str) -> bool:
        created = datetime.fromisoformat(packet.created_at.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(packet.expires_at.replace("Z", "+00:00"))
        if expires <= created:
            raise ValueError("AI packet expiry is invalid")
        body = packet.canonical()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                """INSERT OR IGNORE INTO ai_packets_v2(
                   packet_id,packet_hash,packet_json,role,lane,state,created_at,expires_at,updated_at
                   ) VALUES(?,?,?,?,?,'PENDING',?,?,?)""",
                (
                    packet.packet_id,
                    packet.packet_hash,
                    body,
                    role,
                    packet.lane,
                    created.astimezone(UTC).isoformat(),
                    expires.astimezone(UTC).isoformat(),
                    created.astimezone(UTC).isoformat(),
                ),
            )
            created_row = cur.rowcount == 1
            if not created_row:
                row = self.db.execute(
                    """SELECT packet_hash,packet_json,role,lane FROM ai_packets_v2
                       WHERE packet_id=?""",
                    (packet.packet_id,),
                ).fetchone()
                if row is None or tuple(row) != (packet.packet_hash, body, role, packet.lane):
                    raise ValueError("AI packet identifier cannot be rebound")
            self.db.commit()
            return created_row
        except Exception:
            self.db.rollback()
            raise

    def claim(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int = 300,
        daily_cap: int | None = None,
        max_attempts: int = 3,
    ) -> Mapping[str, Any] | None:
        if not worker_id.strip() or lease_seconds < 30:
            raise ValueError("AI worker/lease is invalid")
        if daily_cap is not None and daily_cap < 1:
            raise ValueError("daily cap must be positive")
        if max_attempts < 1:
            raise ValueError("max attempts must be positive")
        current = self._now(now)
        lease = current + timedelta(seconds=lease_seconds)
        day = current.date().isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            exhausted = self.db.execute(
                """SELECT packet_id FROM ai_packets_v2
                   WHERE state='CLAIMED' AND lease_expires_at<? AND attempt_count>=?""",
                (current.isoformat(), max_attempts),
            ).fetchall()
            for row in exhausted:
                self._dead_letter(str(row["packet_id"]), "inference_lease_exhausted", current)
            self.db.execute(
                """UPDATE ai_packets_v2 SET state='PENDING',claimed_by=NULL,claim_token=NULL,
                   lease_expires_at=NULL,updated_at=?
                   WHERE state='CLAIMED' AND lease_expires_at<? AND attempt_count<?""",
                (current.isoformat(), current.isoformat(), max_attempts),
            )
            retry_exhausted = self.db.execute(
                """SELECT packet_id FROM ai_packets_v2
                   WHERE state='ERROR' AND attempt_count>=?""",
                (max_attempts,),
            ).fetchall()
            for row in retry_exhausted:
                self._dead_letter(str(row["packet_id"]), "inference_retry_exhausted", current)
            self.db.execute(
                """UPDATE ai_packets_v2 SET state='EXPIRED',claimed_by=NULL,
                   claim_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE state IN ('PENDING','ERROR') AND expires_at<?""",
                (current.isoformat(), current.isoformat()),
            )
            rows = self.db.execute(
                """SELECT packet_id,packet_hash,packet_json,role,lane,attempt_count,expires_at
                   FROM ai_packets_v2 WHERE state IN ('PENDING','ERROR') AND expires_at>=?
                     AND attempt_count<?
                   ORDER BY created_at,packet_id""",
                (current.isoformat(), max_attempts),
            ).fetchall()
            selected = None
            for row in rows:
                if daily_cap is not None:
                    used = int(
                        self.db.execute(
                            "SELECT COUNT(*) FROM ai_daily_budget_v2 WHERE day=? AND role=? AND lane=?",
                            (day, str(row["role"]), str(row["lane"])),
                        ).fetchone()[0]
                    )
                    if used >= daily_cap:
                        continue
                selected = row
                break
            if selected is None:
                self.db.commit()
                return None
            token = secrets.token_urlsafe(32)
            attempt = int(selected["attempt_count"]) + 1
            claim_key = f"{selected['packet_id']}:{attempt}"
            if daily_cap is not None:
                self.db.execute(
                    "INSERT INTO ai_daily_budget_v2 VALUES(?,?,?,?,?)",
                    (day, selected["role"], selected["lane"], claim_key, current.isoformat()),
                )
            cur = self.db.execute(
                """UPDATE ai_packets_v2 SET state='CLAIMED',claimed_by=?,claim_token=?,
                   lease_expires_at=?,attempt_count=?,updated_at=?
                   WHERE packet_id=? AND state IN ('PENDING','ERROR')""",
                (
                    worker_id,
                    token,
                    lease.isoformat(),
                    attempt,
                    current.isoformat(),
                    selected["packet_id"],
                ),
            )
            if cur.rowcount != 1:
                raise PermissionError("AI packet claim race")
            self.db.commit()
            return {
                "packet_id": str(selected["packet_id"]),
                "packet_hash": str(selected["packet_hash"]),
                "packet": json.loads(str(selected["packet_json"])),
                "role": str(selected["role"]),
                "lane": str(selected["lane"]),
                "attempt": attempt,
                "claim_token": token,
                "expires_at": str(selected["expires_at"]),
            }
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _output_from_mapping(value: Mapping[str, Any]) -> AIIntentOutputV2:
        decimal_fields = {
            "self_reported_confidence",
            "self_reported_uncertainty",
            "amount_usd",
            "stop_loss_fraction",
            "take_profit_fraction",
            "max_slippage_bps",
            "partial_close_fraction",
        }
        normalized = dict(value)
        normalized["action"] = AIAction(str(normalized["action"]).upper())
        for key in decimal_fields:
            if normalized.get(key) is not None:
                normalized[key] = Decimal(str(normalized[key]))
        for key in ("reason_codes", "evidence_refs", "invalidation_conditions"):
            normalized[key] = tuple(str(item) for item in normalized.get(key, ()))
        return AIIntentOutputV2(**normalized)

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
    ) -> AIIntentOutputV2:
        current = self._now(now)
        row = self.db.execute(
            "SELECT packet_json,state,claim_token,lease_expires_at,role,lane FROM ai_packets_v2 WHERE packet_id=?",
            (packet_id,),
        ).fetchone()
        if row is None:
            raise ValueError("AI packet missing")
        if row["state"] != "CLAIMED" or not secrets.compare_digest(
            str(row["claim_token"]), claim_token
        ):
            raise PermissionError("AI packet claim token is not active")
        if datetime.fromisoformat(str(row["lease_expires_at"])) < current:
            raise PermissionError("AI packet claim lease expired")
        packet_raw = json.loads(str(row["packet_json"]))
        packet = DecisionPacketV2(
            packet_id=str(packet_raw["packet_id"]),
            created_at=str(packet_raw["created_at"]),
            expires_at=str(packet_raw["expires_at"]),
            lane=str(packet_raw["lane"]),
            mode=str(packet_raw["mode"]),
            market_snapshot_ids=tuple(packet_raw["market_snapshot_ids"]),
            feature_snapshot_id=str(packet_raw["feature_snapshot_id"]),
            broker_snapshot_hash=str(packet_raw["broker_snapshot_hash"]),
            risk_config_hash=str(packet_raw["risk_config_hash"]),
            model_context=dict(packet_raw["model_context"]),
            candidates=tuple(packet_raw["candidates"]),
            position=packet_raw.get("position"),
            exact_evidence_refs=tuple(packet_raw["exact_evidence_refs"]),
            schema_version=int(packet_raw.get("schema_version", 2)),
        )
        decision = self._output_from_mapping(output)
        decision.validate(packet)
        import hashlib

        decision_json = json.dumps(
            asdict(decision), sort_keys=True, separators=(",", ":"), default=str
        )
        output_hash = hashlib.sha256(decision_json.encode()).hexdigest()
        required_run = {
            "run_id",
            "status",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "error_type",
        }
        if set(run) != required_run or str(run["status"]) != "COMPLETED":
            raise ValueError("AI run telemetry is invalid")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO ai_runs_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(run["run_id"]),
                    packet_id,
                    str(row["role"]),
                    str(row["lane"]),
                    model,
                    prompt_hash,
                    output_hash,
                    "COMPLETED",
                    run["input_tokens"],
                    run["output_tokens"],
                    run["reasoning_tokens"],
                    int(run["latency_ms"]),
                    run["error_type"],
                    current.isoformat(),
                ),
            )
            cur = self.db.execute(
                """UPDATE ai_packets_v2 SET state='DECIDED',decision_json=?,model=?,prompt_hash=?,
                   decided_at=?,claimed_by=NULL,claim_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE packet_id=? AND state='CLAIMED' AND claim_token=?""",
                (
                    decision_json,
                    model,
                    prompt_hash,
                    current.isoformat(),
                    current.isoformat(),
                    packet_id,
                    claim_token,
                ),
            )
            if cur.rowcount != 1:
                raise PermissionError("AI packet claim was lost")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return decision

    def fail(
        self,
        packet_id: str,
        claim_token: str,
        *,
        retryable: bool,
        run: Mapping[str, Any],
        model: str,
        prompt_hash: str,
        now: datetime,
        max_attempts: int = 3,
    ) -> None:
        current = self._now(now)
        row = self.db.execute(
            "SELECT role,lane,state,claim_token,attempt_count FROM ai_packets_v2 WHERE packet_id=?",
            (packet_id,),
        ).fetchone()
        if (
            row is None
            or row["state"] != "CLAIMED"
            or not secrets.compare_digest(str(row["claim_token"]), claim_token)
        ):
            raise PermissionError("AI packet claim token is not active")
        required_run = {
            "run_id",
            "status",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "error_type",
        }
        if set(run) != required_run or str(run["status"]) != "ERROR":
            raise ValueError("AI error telemetry is invalid")
        if max_attempts < 1:
            raise ValueError("max attempts must be positive")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO ai_runs_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(run["run_id"]),
                    packet_id,
                    str(row["role"]),
                    str(row["lane"]),
                    model,
                    prompt_hash,
                    None,
                    "ERROR",
                    run["input_tokens"],
                    run["output_tokens"],
                    run["reasoning_tokens"],
                    int(run["latency_ms"]),
                    run["error_type"],
                    current.isoformat(),
                ),
            )
            if retryable and int(row["attempt_count"]) < max_attempts:
                self.db.execute(
                    """UPDATE ai_packets_v2 SET state='ERROR',claimed_by=NULL,claim_token=NULL,
                       lease_expires_at=NULL,updated_at=? WHERE packet_id=?""",
                    (current.isoformat(), packet_id),
                )
            elif retryable:
                self._dead_letter(packet_id, "inference_retry_exhausted", current)
            else:
                self._dead_letter(
                    packet_id,
                    f"inference_terminal:{str(run['error_type'] or 'unknown')}",
                    current,
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

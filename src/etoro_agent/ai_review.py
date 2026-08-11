from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from .audit import AuditLog

MINIMAX_PROVIDER = "minimax-coding-plan"
MINIMAX_MODEL = "minimax-coding-plan/MiniMax-M3"
SOL_MODEL = "gpt-5.6-sol"
REVIEW_PROMPT_VERSION = "trade-review-v1"
STRATEGY_PROPOSAL_PROMPT_VERSION = "strategy-proposal-v1"

_FORBIDDEN_KEY = re.compile(
    r"(?:api[_-]?key|user[_-]?key|credential|password|secret|authorization|"
    r"cookie|headers?|broker[_-]?url|mcp[_-]?url|access[_-]?token|refresh[_-]?token|tools?)",
    re.IGNORECASE,
)
_URL = re.compile(r"(?:https?|wss?)://", re.IGNORECASE)
_TRADE_FIELDS = frozenset(
    {
        "trade_id",
        "strategy_id",
        "strategy_version",
        "portfolio_id",
        "symbol",
        "side",
        "opened_at",
        "closed_at",
        "units",
        "notional_usd",
        "entry_price",
        "exit_price",
        "gross_pnl_usd",
        "fees_usd",
        "financing_usd",
        "net_pnl_usd",
        "holding_seconds",
        "exit_reason",
        "mae_usd",
        "mfe_usd",
        "regime",
        "rule_context",
        "risk_context",
        "market_context",
    }
)
_REVIEW_VERDICTS = frozenset(
    {
        "GOOD_PROCESS_GOOD_OUTCOME",
        "GOOD_PROCESS_BAD_OUTCOME",
        "BAD_PROCESS",
        "INCONCLUSIVE",
    }
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_text(value: Any, *, name: str, maximum: int) -> str:
    text = str(value).strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{name} must be present and at most {maximum} characters")
    if _URL.search(text):
        raise ValueError(f"{name} must not contain a URL")
    return text


def _sanitize_json(value: Any, *, path: str = "packet") -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > 4000:
            raise ValueError(f"{path} contains an oversized string")
        if _URL.search(value):
            raise ValueError(f"{path} must not contain broker or external URLs")
        return value
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        if len(value) > 100:
            raise ValueError(f"{path} contains too many fields")
        for key, item in value.items():
            name = str(key)
            if _FORBIDDEN_KEY.search(name):
                raise ValueError(f"{path}.{name} is a forbidden sensitive field")
            clean[name] = _sanitize_json(item, path=f"{path}.{name}")
        return clean
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        if len(value) > 100:
            raise ValueError(f"{path} contains too many items")
        return [_sanitize_json(item, path=f"{path}[]") for item in value]
    raise TypeError(f"{path} contains a non-JSON value")


@dataclass(frozen=True)
class TradeReviewPacket:
    packet_id: str
    packet_hash: str
    packet_json: str
    trade_id: str
    strategy_id: str

    @property
    def payload(self) -> Mapping[str, Any]:
        return MappingProxyType(json.loads(self.packet_json))


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: str | None = None


@dataclass(frozen=True)
class LLMRun:
    run_id: str
    purpose: str
    provider: str
    model: str
    status: str
    input_hash: str
    prompt_hash: str
    output_hash: str | None
    usage: LLMUsage
    latency_ms: int
    error_type: str | None
    error_message: str | None
    started_at: str
    completed_at: str


@dataclass(frozen=True)
class StrategyChangeProposal:
    proposal_id: str
    proposal_hash: str
    source_day: str
    strategy_id: str
    objective: str
    evidence: tuple[str, ...]
    suggested_experiments: tuple[str, ...]
    confidence: float
    model: str
    state: str = "RESEARCH_ONLY"


def build_trade_review_packet(trade: Mapping[str, Any]) -> TradeReviewPacket:
    selected = {key: trade[key] for key in _TRADE_FIELDS if key in trade}
    required = {
        "trade_id",
        "strategy_id",
        "symbol",
        "side",
        "opened_at",
        "closed_at",
        "entry_price",
        "exit_price",
        "net_pnl_usd",
    }
    missing = sorted(required - selected.keys())
    if missing:
        raise ValueError(f"closed trade is missing review fields: {','.join(missing)}")
    if selected["closed_at"] in {None, ""} or selected["exit_price"] in {None, ""}:
        raise ValueError("trade review requires a completed round trip")
    trade_id = _bounded_text(selected["trade_id"], name="trade_id", maximum=128)
    strategy_id = _bounded_text(selected["strategy_id"], name="strategy_id", maximum=128)
    selected["trade_id"] = trade_id
    selected["strategy_id"] = strategy_id
    selected["review_contract"] = {
        "scope": "POST_ROUND_TRIP_ONLY",
        "authority": "RESEARCH_ONLY",
        "prompt_version": REVIEW_PROMPT_VERSION,
    }
    sanitized = _sanitize_json(selected)
    packet_json = canonical_json(sanitized)
    packet_hash = sha256_text(packet_json)
    return TradeReviewPacket(
        packet_id=f"review-packet-{packet_hash[:24]}",
        packet_hash=packet_hash,
        packet_json=packet_json,
        trade_id=trade_id,
        strategy_id=strategy_id,
    )


def validate_trade_review(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "verdict",
        "process_score",
        "confidence",
        "rule_adherence",
        "reason_codes",
        "findings",
        "suggested_experiments",
        "summary",
    }
    if set(value) != allowed:
        raise ValueError("MiniMax review must match the strict output schema")
    verdict = str(value["verdict"]).upper()
    if verdict not in _REVIEW_VERDICTS:
        raise ValueError("MiniMax review verdict is invalid")
    score = value["process_score"]
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("MiniMax process_score must be an integer in [0,100]")
    confidence = float(value["confidence"])
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("MiniMax confidence must be in [0,1]")
    adherence = str(value["rule_adherence"]).upper()
    if adherence not in {"PASS", "FAIL", "UNKNOWN"}:
        raise ValueError("MiniMax rule_adherence is invalid")

    def text_list(name: str, maximum_items: int, maximum_length: int) -> list[str]:
        raw = value[name]
        if not isinstance(raw, list) or not raw or len(raw) > maximum_items:
            raise ValueError(f"MiniMax {name} must be a bounded non-empty list")
        return [_bounded_text(item, name=name, maximum=maximum_length) for item in raw]

    return {
        "verdict": verdict,
        "process_score": score,
        "confidence": confidence,
        "rule_adherence": adherence,
        "reason_codes": text_list("reason_codes", 8, 64),
        "findings": text_list("findings", 8, 500),
        "suggested_experiments": text_list("suggested_experiments", 5, 500),
        "summary": _bounded_text(value["summary"], name="summary", maximum=1000),
    }


def validate_strategy_change_proposal(
    aggregate: Mapping[str, Any], value: Mapping[str, Any]
) -> StrategyChangeProposal:
    allowed = {"strategy_id", "objective", "evidence", "suggested_experiments", "confidence"}
    if set(value) != allowed:
        raise ValueError("Sol strategy proposal must match the strict output schema")
    strategy_id = _bounded_text(value["strategy_id"], name="strategy_id", maximum=128)
    if strategy_id != str(aggregate["strategy_id"]):
        raise ValueError("Sol strategy proposal is not bound to its aggregate")
    evidence = value["evidence"]
    experiments = value["suggested_experiments"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
        raise ValueError("Sol proposal evidence must be a bounded list")
    if not isinstance(experiments, list) or not 1 <= len(experiments) <= 5:
        raise ValueError("Sol proposal experiments must be a bounded list")
    confidence = float(value["confidence"])
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Sol proposal confidence must be in [0,1]")
    body = {
        "source_day": str(aggregate["day"]),
        "strategy_id": strategy_id,
        "objective": _bounded_text(value["objective"], name="objective", maximum=500),
        "evidence": [_bounded_text(item, name="evidence", maximum=500) for item in evidence],
        "suggested_experiments": [
            _bounded_text(item, name="suggested_experiments", maximum=500) for item in experiments
        ],
        "confidence": confidence,
        "model": SOL_MODEL,
        "state": "RESEARCH_ONLY",
        "constraints": [
            "NO_LIVE_CODE_MUTATION",
            "NO_LIVE_CONFIG_MUTATION",
            "NO_RISK_POLICY_MUTATION",
            "BACKTEST_AND_SHADOW_AB_REQUIRED",
        ],
    }
    digest = sha256_text(canonical_json(body))
    return StrategyChangeProposal(
        proposal_id=f"strategy-proposal-{digest[:24]}",
        proposal_hash=digest,
        source_day=body["source_day"],
        strategy_id=strategy_id,
        objective=body["objective"],
        evidence=tuple(body["evidence"]),
        suggested_experiments=tuple(body["suggested_experiments"]),
        confidence=confidence,
        model=SOL_MODEL,
    )


class AIReviewStore:
    """Append-only LLM telemetry and post-trade research reviews."""

    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self.audit.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS llm_runs (
                run_id TEXT PRIMARY KEY,
                purpose TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('COMPLETED','ERROR')),
                input_hash TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                output_hash TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                cost_usd TEXT,
                latency_ms INTEGER NOT NULL,
                error_type TEXT,
                error_message TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS llm_runs_daily_idx
                ON llm_runs(provider,model,purpose,started_at);
            CREATE TABLE IF NOT EXISTS llm_daily_claims (
                claim_id TEXT PRIMARY KEY,
                claim_key TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                purpose TEXT NOT NULL,
                day TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                UNIQUE(provider,model,purpose,claim_key)
            );
            CREATE INDEX IF NOT EXISTS llm_daily_claims_cap_idx
                ON llm_daily_claims(provider,model,purpose,day);
            CREATE TABLE IF NOT EXISTS trade_ai_reviews (
                review_id TEXT PRIMARY KEY,
                trade_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                packet_hash TEXT NOT NULL,
                packet_json TEXT NOT NULL,
                review_hash TEXT NOT NULL,
                review_json TEXT NOT NULL,
                llm_run_id TEXT NOT NULL REFERENCES llm_runs(run_id),
                created_at TEXT NOT NULL,
                UNIQUE(trade_id,model,prompt_hash)
            );
            CREATE INDEX IF NOT EXISTS trade_ai_reviews_strategy_idx
                ON trade_ai_reviews(strategy_id,created_at);
            CREATE TABLE IF NOT EXISTS ai_review_jobs (
                job_id TEXT PRIMARY KEY,
                packet_id TEXT NOT NULL UNIQUE,
                packet_hash TEXT NOT NULL UNIQUE,
                packet_json TEXT NOT NULL,
                trade_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('PENDING','CLAIMED','COMPLETED','ERROR')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                lease_expires_at TEXT,
                claimed_by TEXT,
                claim_token TEXT,
                llm_run_id TEXT,
                review_id TEXT,
                result_hash TEXT,
                last_error_type TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ai_review_jobs_queue_idx
                ON ai_review_jobs(state,available_at,lease_expires_at,created_at);
            CREATE TABLE IF NOT EXISTS strategy_change_proposals (
                proposal_id TEXT PRIMARY KEY,
                proposal_hash TEXT NOT NULL UNIQUE,
                source_day TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state='RESEARCH_ONLY'),
                model TEXT NOT NULL,
                aggregate_hash TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                llm_run_id TEXT REFERENCES llm_runs(run_id),
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS strategy_change_proposals_day_strategy_idx
                ON strategy_change_proposals(source_day,strategy_id);
            CREATE TRIGGER IF NOT EXISTS llm_runs_no_update
                BEFORE UPDATE ON llm_runs BEGIN SELECT RAISE(ABORT,'llm_runs is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS llm_runs_no_delete
                BEFORE DELETE ON llm_runs BEGIN SELECT RAISE(ABORT,'llm_runs is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS llm_daily_claims_no_update
                BEFORE UPDATE ON llm_daily_claims BEGIN SELECT RAISE(ABORT,'llm_daily_claims is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS llm_daily_claims_no_delete
                BEFORE DELETE ON llm_daily_claims BEGIN SELECT RAISE(ABORT,'llm_daily_claims is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS trade_ai_reviews_no_update
                BEFORE UPDATE ON trade_ai_reviews BEGIN SELECT RAISE(ABORT,'trade_ai_reviews is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS trade_ai_reviews_no_delete
                BEFORE DELETE ON trade_ai_reviews BEGIN SELECT RAISE(ABORT,'trade_ai_reviews is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS ai_review_jobs_packet_immutable
                BEFORE UPDATE ON ai_review_jobs
                WHEN OLD.packet_id != NEW.packet_id
                  OR OLD.packet_hash != NEW.packet_hash
                  OR OLD.packet_json != NEW.packet_json
                  OR OLD.trade_id != NEW.trade_id
                  OR OLD.strategy_id != NEW.strategy_id
                  OR OLD.provider != NEW.provider
                  OR OLD.model != NEW.model
                  OR OLD.prompt_version != NEW.prompt_version
                  OR OLD.prompt_hash != NEW.prompt_hash
                BEGIN SELECT RAISE(ABORT,'ai_review_jobs packet is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS strategy_change_proposals_no_update
                BEFORE UPDATE ON strategy_change_proposals BEGIN SELECT RAISE(ABORT,'strategy_change_proposals is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS strategy_change_proposals_no_delete
                BEFORE DELETE ON strategy_change_proposals BEGIN SELECT RAISE(ABORT,'strategy_change_proposals is append-only'); END;
            """
        )
        self.audit._ensure_column("ai_review_jobs", "claim_token", "TEXT")
        self.audit.db.commit()

    def claim_daily_capacity(
        self,
        *,
        provider: str,
        model: str,
        purpose: str,
        input_hash: str,
        prompt_hash: str,
        daily_cap: int | None,
        claim_key: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Atomically reserve one daily call; crashes consume the slot fail-closed."""

        if daily_cap is not None and daily_cap < 1:
            raise ValueError("LLM daily cap must be positive")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        day = timestamp.date().isoformat()
        normalized_claim_key = claim_key or f"{input_hash}:{prompt_hash}"
        claim_body = canonical_json(
            {
                "provider": provider,
                "model": model,
                "purpose": purpose,
                "input_hash": input_hash,
                "prompt_hash": prompt_hash,
                "claim_key": normalized_claim_key,
            }
        )
        claim_id = f"llm-claim-{sha256_text(claim_body)[:24]}"
        try:
            self.audit.db.execute("BEGIN IMMEDIATE")
            duplicate = self.audit.db.execute(
                """SELECT 1 FROM llm_daily_claims
                   WHERE provider=? AND model=? AND purpose=? AND claim_key=?""",
                (provider, model, purpose, normalized_claim_key),
            ).fetchone()
            if duplicate is not None:
                self.audit.db.rollback()
                return "DUPLICATE"
            count = int(
                self.audit.db.execute(
                    """SELECT COUNT(*) FROM llm_daily_claims
                       WHERE provider=? AND model=? AND purpose=? AND day=?""",
                    (provider, model, purpose, day),
                ).fetchone()[0]
            )
            if daily_cap is not None and count >= daily_cap:
                self.audit.db.rollback()
                return "CAP_REACHED"
            self.audit.db.execute(
                """INSERT INTO llm_daily_claims(
                       claim_id,claim_key,provider,model,purpose,day,input_hash,prompt_hash,claimed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    claim_id,
                    normalized_claim_key,
                    provider,
                    model,
                    purpose,
                    day,
                    input_hash,
                    prompt_hash,
                    timestamp.isoformat(),
                ),
            )
            self.audit.db.commit()
            self.audit.append(
                "llm_daily_capacity_claimed",
                {
                    "claim_id": claim_id,
                    "provider": provider,
                    "model": model,
                    "purpose": purpose,
                    "day": day,
                    "input_hash": input_hash,
                    "prompt_hash": prompt_hash,
                },
            )
            return "CLAIMED"
        except Exception:
            self.audit.db.rollback()
            raise

    def runs_on_day(
        self,
        provider: str,
        model: str,
        purpose: str,
        day: date | None = None,
    ) -> int:
        target = day or datetime.now(UTC).date()
        start = datetime.combine(target, time.min, tzinfo=UTC).isoformat()
        end = datetime.combine(target, time.max, tzinfo=UTC).isoformat()
        return int(
            self.audit.db.execute(
                """SELECT COUNT(*) FROM llm_runs
                   WHERE provider=? AND model=? AND purpose=? AND started_at>=? AND started_at<=?""",
                (provider, model, purpose, start, end),
            ).fetchone()[0]
        )

    def has_review(self, trade_id: str, model: str, prompt_hash: str) -> bool:
        return (
            self.audit.db.execute(
                "SELECT 1 FROM trade_ai_reviews WHERE trade_id=? AND model=? AND prompt_hash=?",
                (trade_id, model, prompt_hash),
            ).fetchone()
            is not None
        )

    def queue_review_job(
        self,
        packet: TradeReviewPacket,
        prompt_hash: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, bool]:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        job_id = (
            f"ai-review-job-{sha256_text(packet.packet_hash + MINIMAX_MODEL + prompt_hash)[:24]}"
        )
        values = (
            job_id,
            packet.packet_id,
            packet.packet_hash,
            packet.packet_json,
            packet.trade_id,
            packet.strategy_id,
            MINIMAX_PROVIDER,
            MINIMAX_MODEL,
            REVIEW_PROMPT_VERSION,
            prompt_hash,
            "PENDING",
            timestamp.isoformat(),
            timestamp.isoformat(),
            timestamp.isoformat(),
        )
        cursor = self.audit.db.execute(
            """INSERT OR IGNORE INTO ai_review_jobs(
                   job_id,packet_id,packet_hash,packet_json,trade_id,strategy_id,
                   provider,model,prompt_version,prompt_hash,state,available_at,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        self.audit.db.commit()
        created = cursor.rowcount == 1
        if not created:
            existing = self.audit.db.execute(
                """SELECT job_id,packet_id,packet_hash,packet_json,trade_id,strategy_id,
                          provider,model,prompt_version,prompt_hash
                   FROM ai_review_jobs WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            expected = values[:10]
            if existing is None or tuple(existing) != expected:
                raise ValueError("AI review job identity is immutable and cannot be rebound")
        else:
            self.audit.append(
                "ai_review_job_queued",
                {
                    "job_id": job_id,
                    "packet_id": packet.packet_id,
                    "packet_hash": packet.packet_hash,
                    "trade_id": packet.trade_id,
                    "strategy_id": packet.strategy_id,
                    "model": MINIMAX_MODEL,
                },
            )
        return job_id, created

    def claim_pending_reviews(
        self,
        *,
        worker_id: str,
        limit: int = 5,
        daily_cap: int = 50,
        lease_seconds: int = 600,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if not worker_id.strip() or len(worker_id) > 128:
            raise ValueError("AI review worker_id is required and bounded")
        if not 1 <= limit <= 50 or daily_cap < 1 or lease_seconds < 30 or max_attempts < 1:
            raise ValueError("AI review claim limits are invalid")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        now_iso = timestamp.isoformat()
        day = timestamp.date().isoformat()
        lease_expires = (timestamp + timedelta(seconds=lease_seconds)).isoformat()
        claimed_rows: list[tuple[Any, ...]] = []
        reclaimed = 0
        try:
            self.audit.db.execute("BEGIN IMMEDIATE")
            reclaimed = int(
                self.audit.db.execute(
                    """UPDATE ai_review_jobs
                       SET state='PENDING',lease_expires_at=NULL,claimed_by=NULL,updated_at=?
                       WHERE state='CLAIMED' AND lease_expires_at<?""",
                    (now_iso, now_iso),
                ).rowcount
            )
            used = int(
                self.audit.db.execute(
                    """SELECT COUNT(*) FROM llm_daily_claims
                       WHERE provider=? AND model=? AND purpose=? AND day=?""",
                    (MINIMAX_PROVIDER, MINIMAX_MODEL, "TRADE_REVIEW", day),
                ).fetchone()[0]
            )
            capacity = max(0, min(limit, daily_cap - used))
            if capacity:
                candidates = self.audit.db.execute(
                    """SELECT job_id,packet_id,packet_hash,packet_json,trade_id,strategy_id,
                              prompt_hash,attempt_count
                       FROM ai_review_jobs
                       WHERE state IN ('PENDING','ERROR') AND available_at<=? AND attempt_count<?
                       ORDER BY created_at,job_id LIMIT ?""",
                    (now_iso, max_attempts, capacity),
                ).fetchall()
                for row in candidates:
                    attempt = int(row[7]) + 1
                    claim_key = f"{row[0]}:{attempt}"
                    claim_body = canonical_json(
                        {
                            "provider": MINIMAX_PROVIDER,
                            "model": MINIMAX_MODEL,
                            "purpose": "TRADE_REVIEW",
                            "claim_key": claim_key,
                        }
                    )
                    claim_id = f"llm-claim-{sha256_text(claim_body)[:24]}"
                    self.audit.db.execute(
                        """INSERT INTO llm_daily_claims(
                               claim_id,claim_key,provider,model,purpose,day,input_hash,
                               prompt_hash,claimed_at
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            claim_id,
                            claim_key,
                            MINIMAX_PROVIDER,
                            MINIMAX_MODEL,
                            "TRADE_REVIEW",
                            day,
                            str(row[2]),
                            str(row[6]),
                            now_iso,
                        ),
                    )
                    claim_token = secrets.token_urlsafe(32)
                    cursor = self.audit.db.execute(
                        """UPDATE ai_review_jobs
                           SET state='CLAIMED',attempt_count=?,lease_expires_at=?,claimed_by=?,
                               claim_token=?,updated_at=?,last_error_type=NULL
                           WHERE job_id=? AND state IN ('PENDING','ERROR')""",
                        (
                            attempt,
                            lease_expires,
                            worker_id,
                            claim_token,
                            now_iso,
                            str(row[0]),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise PermissionError("MiniMax review job claim was lost")
                    claimed_rows.append((*tuple(row), claim_token))
            self.audit.db.commit()
        except Exception:
            self.audit.db.rollback()
            raise
        if reclaimed:
            self.audit.append("ai_review_job_leases_reclaimed", {"count": reclaimed})
        for row in claimed_rows:
            self.audit.append(
                "ai_review_job_claimed",
                {
                    "job_id": str(row[0]),
                    "packet_id": str(row[1]),
                    "packet_hash": str(row[2]),
                    "worker_id": worker_id,
                    "attempt": int(row[7]) + 1,
                    "lease_expires_at": lease_expires,
                },
            )
        return tuple(
            {
                "packet_id": str(row[1]),
                "packet_hash": str(row[2]),
                "packet_json": str(row[3]),
                "trade_id": str(row[4]),
                "strategy_id": str(row[5]),
                "job_id": str(row[0]),
                "attempt": int(row[7]) + 1,
                "claim_token": str(row[8]),
            }
            for row in claimed_rows
        )

    def submit_review_result(
        self, result: Mapping[str, Any], *, now: datetime | None = None
    ) -> dict[str, str | None]:
        required = {
            "run_id",
            "purpose",
            "provider",
            "model",
            "status",
            "job_id",
            "attempt",
            "claim_token",
            "packet_id",
            "packet_hash",
            "trade_id",
            "strategy_id",
            "prompt_version",
            "prompt_hash",
            "output_hash",
            "review",
            "usage",
            "latency_ms",
            "error_type",
            "started_at",
            "completed_at",
        }
        if set(result) != required:
            raise ValueError("MiniMax result does not match the strict wire contract")
        result_json = canonical_json(dict(result))
        result_hash = sha256_text(result_json)
        row = self.audit.db.execute(
            """SELECT job_id,packet_json,trade_id,strategy_id,prompt_hash,state,result_hash,
                      llm_run_id,review_id,attempt_count,claim_token,lease_expires_at
               FROM ai_review_jobs WHERE packet_id=? AND packet_hash=?""",
            (str(result["packet_id"]), str(result["packet_hash"])),
        ).fetchone()
        if row is None:
            raise ValueError("MiniMax result is not bound to a durable review job")
        if (
            row[6] is not None
            and str(row[6]) == result_hash
            and str(row[0]) == str(result["job_id"])
            and int(row[9]) == int(result["attempt"])
        ):
            return {"status": str(row[5]), "run_id": str(row[7]), "review_id": row[8]}
        if (
            str(row[0]) != str(result["job_id"])
            or int(row[9]) != int(result["attempt"])
            or not secrets.compare_digest(str(row[10]), str(result["claim_token"]))
        ):
            raise PermissionError("MiniMax result does not hold the active claim token")
        if str(row[2]) != str(result["trade_id"]) or str(row[3]) != str(result["strategy_id"]):
            raise ValueError("MiniMax result trade binding is invalid")
        if str(row[4]) != str(result["prompt_hash"]):
            raise ValueError("MiniMax result prompt hash is invalid")
        if str(result["provider"]) != MINIMAX_PROVIDER or str(result["model"]) != MINIMAX_MODEL:
            raise ValueError("MiniMax result provider/model is invalid")
        if (
            str(result["purpose"]) != "TRADE_REVIEW"
            or str(result["prompt_version"]) != REVIEW_PROMPT_VERSION
        ):
            raise ValueError("MiniMax result purpose/version is invalid")
        if str(row[5]) != "CLAIMED":
            raise PermissionError("MiniMax result job is not currently claimed")

        usage_raw = result["usage"]
        if not isinstance(usage_raw, Mapping):
            raise ValueError("MiniMax result usage is invalid")
        allowed_usage = {
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cost_usd",
        }
        if set(usage_raw) != allowed_usage:
            raise ValueError("MiniMax result usage does not match the strict schema")

        def token(name: str) -> int | None:
            value = usage_raw[name]
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"MiniMax {name} is invalid")
            return value

        status = str(result["status"])
        if status not in {"COMPLETED", "ERROR"}:
            raise ValueError("MiniMax result status is invalid")
        run = LLMRun(
            run_id=_bounded_text(result["run_id"], name="run_id", maximum=128),
            purpose="TRADE_REVIEW",
            provider=MINIMAX_PROVIDER,
            model=MINIMAX_MODEL,
            status=status,
            input_hash=str(result["packet_hash"]),
            prompt_hash=str(result["prompt_hash"]),
            output_hash=str(result["output_hash"]) if result["output_hash"] is not None else None,
            usage=LLMUsage(
                input_tokens=token("input_tokens"),
                output_tokens=token("output_tokens"),
                reasoning_tokens=token("reasoning_tokens"),
                cache_read_tokens=token("cache_read_tokens"),
                cache_write_tokens=token("cache_write_tokens"),
                cost_usd=(
                    str(usage_raw["cost_usd"]) if usage_raw["cost_usd"] is not None else None
                ),
            ),
            latency_ms=int(result["latency_ms"]),
            error_type=(str(result["error_type"]) if result["error_type"] is not None else None),
            error_message=None,
            started_at=str(result["started_at"]),
            completed_at=str(result["completed_at"]),
        )
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        if row[11] is None or datetime.fromisoformat(str(row[11])) < timestamp:
            raise PermissionError("MiniMax result claim lease expired")
        if status == "ERROR":
            delay = min(900, 30 * (2 ** max(0, int(row[9]) - 1)))
            try:
                self.audit.db.execute("BEGIN IMMEDIATE")
                self.record_run(run, commit=False, audit_event=False)
                cursor = self.audit.db.execute(
                    """UPDATE ai_review_jobs
                       SET state='ERROR',available_at=?,lease_expires_at=NULL,claimed_by=NULL,
                           claim_token=NULL,llm_run_id=?,result_hash=?,last_error_type=?,updated_at=?
                       WHERE job_id=? AND state='CLAIMED' AND attempt_count=? AND claim_token=?""",
                    (
                        (timestamp + timedelta(seconds=delay)).isoformat(),
                        run.run_id,
                        result_hash,
                        run.error_type,
                        timestamp.isoformat(),
                        str(row[0]),
                        int(result["attempt"]),
                        str(result["claim_token"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise PermissionError("MiniMax review job lost its active claim")
                self.audit.db.commit()
            except Exception:
                self.audit.db.rollback()
                raise
            self._audit_run(run)
            self.audit.append(
                "ai_review_job_failed",
                {"job_id": str(row[0]), "run_id": run.run_id, "error_type": run.error_type},
            )
            return {"status": "ERROR", "run_id": run.run_id, "review_id": None}

        review_raw = result["review"]
        if not isinstance(review_raw, Mapping):
            raise ValueError("completed MiniMax result lacks a review")
        validated = validate_trade_review(review_raw)
        if sha256_text(canonical_json(validated)) != str(result["output_hash"]):
            raise ValueError("MiniMax output hash does not match the review")
        packet_payload = json.loads(str(row[1]))
        packet = build_trade_review_packet(packet_payload)
        review_json = canonical_json(validated)
        review_hash = sha256_text(review_json)
        try:
            self.audit.db.execute("BEGIN IMMEDIATE")
            self.record_run(run, commit=False, audit_event=False)
            review_id = self.record_review(
                packet,
                validated,
                run.run_id,
                str(row[4]),
                created_at=timestamp,
                commit=False,
                audit_event=False,
            )
            cursor = self.audit.db.execute(
                """UPDATE ai_review_jobs
                   SET state='COMPLETED',lease_expires_at=NULL,claimed_by=NULL,claim_token=NULL,llm_run_id=?,
                       review_id=?,result_hash=?,last_error_type=NULL,updated_at=?
                   WHERE job_id=? AND state='CLAIMED' AND attempt_count=? AND claim_token=?""",
                (
                    run.run_id,
                    review_id,
                    result_hash,
                    timestamp.isoformat(),
                    str(row[0]),
                    int(result["attempt"]),
                    str(result["claim_token"]),
                ),
            )
            if cursor.rowcount != 1:
                raise PermissionError("MiniMax review job lost its active claim")
            self.audit.db.commit()
        except Exception:
            self.audit.db.rollback()
            raise
        self._audit_run(run)
        self._audit_review(packet, review_id, review_hash, run.run_id)
        self.audit.append(
            "ai_review_job_completed",
            {"job_id": str(row[0]), "run_id": run.run_id, "review_id": review_id},
        )
        return {"status": "COMPLETED", "run_id": run.run_id, "review_id": review_id}

    def record_run(
        self,
        run: LLMRun,
        *,
        commit: bool = True,
        audit_event: bool = True,
    ) -> bool:
        if run.status not in {"COMPLETED", "ERROR"}:
            raise ValueError("LLM run status must be terminal")
        if run.latency_ms < 0:
            raise ValueError("LLM latency cannot be negative")
        values = (
            run.run_id,
            run.purpose,
            run.provider,
            run.model,
            run.status,
            run.input_hash,
            run.prompt_hash,
            run.output_hash,
            run.usage.input_tokens,
            run.usage.output_tokens,
            run.usage.reasoning_tokens,
            run.usage.cache_read_tokens,
            run.usage.cache_write_tokens,
            run.usage.cost_usd,
            run.latency_ms,
            run.error_type,
            run.error_message[:500] if run.error_message else None,
            run.started_at,
            run.completed_at,
        )
        existing = self.audit.db.execute(
            """SELECT run_id,purpose,provider,model,status,input_hash,prompt_hash,output_hash,
                      input_tokens,output_tokens,reasoning_tokens,cache_read_tokens,
                      cache_write_tokens,cost_usd,latency_ms,error_type,error_message,
                      started_at,completed_at FROM llm_runs WHERE run_id=?""",
            (run.run_id,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise ValueError("LLM run identifiers are immutable and cannot be rebound")
            return False
        self.audit.db.execute(
            """
            INSERT INTO llm_runs(
                run_id,purpose,provider,model,status,input_hash,prompt_hash,output_hash,
                input_tokens,output_tokens,reasoning_tokens,cache_read_tokens,
                cache_write_tokens,cost_usd,latency_ms,error_type,error_message,
                started_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            values,
        )
        if commit:
            self.audit.db.commit()
        if audit_event:
            self._audit_run(run)
        return True

    def _audit_run(self, run: LLMRun) -> None:
        self.audit.append(
            "llm_run_recorded",
            {
                "run_id": run.run_id,
                "purpose": run.purpose,
                "provider": run.provider,
                "model": run.model,
                "status": run.status,
                "input_hash": run.input_hash,
                "prompt_hash": run.prompt_hash,
                "output_hash": run.output_hash,
                "latency_ms": run.latency_ms,
            },
        )

    def record_review(
        self,
        packet: TradeReviewPacket,
        review: Mapping[str, Any],
        run_id: str,
        prompt_hash: str,
        *,
        created_at: datetime | None = None,
        commit: bool = True,
        audit_event: bool = True,
    ) -> str:
        validated = validate_trade_review(review)
        review_json = canonical_json(validated)
        review_hash = sha256_text(review_json)
        review_id = (
            f"trade-review-{sha256_text(packet.trade_id + MINIMAX_MODEL + prompt_hash)[:24]}"
        )
        now = (created_at or datetime.now(UTC)).astimezone(UTC).isoformat()
        existing = self.audit.db.execute(
            """SELECT review_id,packet_hash,review_hash,llm_run_id FROM trade_ai_reviews
               WHERE trade_id=? AND model=? AND prompt_hash=?""",
            (packet.trade_id, MINIMAX_MODEL, prompt_hash),
        ).fetchone()
        if existing is not None:
            if (
                str(existing[1]) != packet.packet_hash
                or str(existing[2]) != review_hash
                or str(existing[3]) != run_id
            ):
                raise ValueError("trade review identity is immutable and cannot be rebound")
            return str(existing[0])
        self.audit.db.execute(
            """
            INSERT INTO trade_ai_reviews(
                review_id,trade_id,strategy_id,provider,model,prompt_version,prompt_hash,
                packet_hash,packet_json,review_hash,review_json,llm_run_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                review_id,
                packet.trade_id,
                packet.strategy_id,
                MINIMAX_PROVIDER,
                MINIMAX_MODEL,
                REVIEW_PROMPT_VERSION,
                prompt_hash,
                packet.packet_hash,
                packet.packet_json,
                review_hash,
                review_json,
                run_id,
                now,
            ),
        )
        if commit:
            self.audit.db.commit()
        if audit_event:
            self._audit_review(packet, review_id, review_hash, run_id)
        return review_id

    def _audit_review(
        self,
        packet: TradeReviewPacket,
        review_id: str,
        review_hash: str,
        run_id: str,
    ) -> None:
        self.audit.append(
            "trade_ai_review_recorded",
            {
                "review_id": review_id,
                "trade_id": packet.trade_id,
                "strategy_id": packet.strategy_id,
                "packet_hash": packet.packet_hash,
                "review_hash": review_hash,
                "llm_run_id": run_id,
                "model": MINIMAX_MODEL,
            },
        )

    def daily_aggregate(self, day: date, strategy_id: str) -> dict[str, Any]:
        rows = self.audit.db.execute(
            """
            SELECT trade_id,packet_json,review_json,review_hash
            FROM trade_ai_reviews
            WHERE strategy_id=?
            ORDER BY created_at,review_id
            """,
            (strategy_id,),
        ).fetchall()
        verdicts: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        experiments: Counter[str] = Counter()
        process_scores: list[int] = []
        confidences: list[float] = []
        net_pnl = Decimal("0")
        evidence_hashes: list[str] = []
        for row in rows:
            packet = json.loads(str(row[1]))
            closed_at_raw = packet.get("closed_at")
            if not closed_at_raw:
                continue
            closed_at = datetime.fromisoformat(str(closed_at_raw).replace("Z", "+00:00"))
            if closed_at.tzinfo is None or closed_at.astimezone(UTC).date() != day:
                continue
            review = json.loads(str(row[2]))
            verdicts[str(review["verdict"])] += 1
            reasons.update(str(item) for item in review["reason_codes"])
            experiments.update(str(item) for item in review["suggested_experiments"])
            process_scores.append(int(review["process_score"]))
            confidences.append(float(review["confidence"]))
            try:
                net_pnl += Decimal(str(packet.get("net_pnl_usd", "0")))
            except InvalidOperation as exc:
                raise ValueError("trade review packet contains invalid net P&L") from exc
            evidence_hashes.append(str(row[3]))
        aggregate = {
            "day": day.isoformat(),
            "strategy_id": strategy_id,
            "review_count": len(evidence_hashes),
            "net_pnl_usd": str(net_pnl),
            "average_process_score": (
                round(sum(process_scores) / len(process_scores), 4) if process_scores else None
            ),
            "average_confidence": (
                round(sum(confidences) / len(confidences), 6) if confidences else None
            ),
            "verdict_counts": dict(sorted(verdicts.items())),
            "top_reason_codes": reasons.most_common(10),
            "top_suggested_experiments": experiments.most_common(10),
            "review_hashes": evidence_hashes,
            "authority": "RESEARCH_ONLY",
            "constraints": [
                "NO_LIVE_CODE_MUTATION",
                "NO_LIVE_CONFIG_MUTATION",
                "NO_RISK_POLICY_MUTATION",
                "BACKTEST_AND_SHADOW_AB_REQUIRED",
            ],
        }
        aggregate["aggregate_hash"] = sha256_text(canonical_json(aggregate))
        return aggregate

    def record_strategy_proposal(
        self,
        aggregate: Mapping[str, Any],
        proposal: StrategyChangeProposal,
        *,
        llm_run_id: str | None = None,
        created_at: datetime | None = None,
    ) -> bool:
        if proposal.state != "RESEARCH_ONLY":
            raise ValueError("strategy proposals have research-only authority")
        proposal_json = canonical_json(
            {
                "objective": proposal.objective,
                "evidence": list(proposal.evidence),
                "suggested_experiments": list(proposal.suggested_experiments),
                "confidence": proposal.confidence,
                "constraints": [
                    "NO_LIVE_CODE_MUTATION",
                    "NO_LIVE_CONFIG_MUTATION",
                    "NO_RISK_POLICY_MUTATION",
                    "BACKTEST_AND_SHADOW_AB_REQUIRED",
                ],
            }
        )
        now = (created_at or datetime.now(UTC)).astimezone(UTC).isoformat()
        existing = self.audit.db.execute(
            "SELECT proposal_id,proposal_hash,aggregate_hash,llm_run_id "
            "FROM strategy_change_proposals WHERE source_day=? AND strategy_id=?",
            (proposal.source_day, proposal.strategy_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing[0]) != proposal.proposal_id
                or str(existing[1]) != proposal.proposal_hash
                or str(existing[2]) != str(aggregate["aggregate_hash"])
                or (existing[3] or None) != llm_run_id
            ):
                raise ValueError("strategy proposal identity is immutable and cannot be rebound")
            return False
        self.audit.db.execute(
            """
            INSERT INTO strategy_change_proposals(
                proposal_id,proposal_hash,source_day,strategy_id,state,model,
                aggregate_hash,proposal_json,llm_run_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                proposal.proposal_id,
                proposal.proposal_hash,
                proposal.source_day,
                proposal.strategy_id,
                proposal.state,
                proposal.model,
                str(aggregate["aggregate_hash"]),
                proposal_json,
                llm_run_id,
                now,
            ),
        )
        self.audit.db.commit()
        self.audit.append(
            "strategy_change_proposal_recorded",
            {
                "proposal_id": proposal.proposal_id,
                "proposal_hash": proposal.proposal_hash,
                "aggregate_hash": str(aggregate["aggregate_hash"]),
                "strategy_id": proposal.strategy_id,
                "state": proposal.state,
            },
        )
        return True


def is_daily_cap_available(
    store: AIReviewStore,
    *,
    provider: str,
    model: str,
    purpose: str,
    daily_cap: int,
    day: date | None = None,
) -> bool:
    if daily_cap < 1:
        raise ValueError("LLM daily cap must be positive")
    return store.runs_on_day(provider, model, purpose, day) < daily_cap

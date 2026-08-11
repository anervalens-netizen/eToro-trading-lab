from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any


class AIRole(StrEnum):
    MARKET_REGIME_ANALYST = "market_regime_analyst"
    ADVERSARIAL_CRITIC = "adversarial_critic"
    PORTFOLIO_DECIDER = "portfolio_decider_sol"


class Lane(StrEnum):
    DETERMINISTIC = "A_deterministic"
    SOL_RANKER_VETO = "B_sol_ranker_veto"
    SOL_DIRECT = "C_sol_direct"
    SOL_CRITIC = "D_sol_plus_critic"
    STATISTICAL_ML = "E_simple_statistical_ml"


class AIAction(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    HOLD = "HOLD"


@dataclass(frozen=True)
class DecisionPacketV2:
    packet_id: str
    created_at: str
    expires_at: str
    lane: str
    mode: str
    market_snapshot_ids: tuple[str, ...]
    feature_snapshot_id: str
    broker_snapshot_hash: str
    risk_config_hash: str
    model_context: Mapping[str, Any]
    candidates: tuple[Mapping[str, Any], ...]
    position: Mapping[str, Any] | None
    exact_evidence_refs: tuple[str, ...]
    schema_version: int = 2

    def canonical(self) -> str:
        body = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)
        if len(body.encode("utf-8")) > 256_000:
            raise ValueError("Decision Packet v2 exceeds 256 KB")
        return body

    @property
    def packet_hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


_FORBIDDEN_FIELD_PARTS = (
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "userkey",
    "ssh",
    "privatekey",
)


def sanitize_packet_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise ValueError("packet nesting is too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("packet contains non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > 4000:
            raise ValueError("packet string is too large")
        if "<script" in value.lower() or "javascript:" in value.lower():
            raise ValueError("active content is forbidden in AI packets")
        return value
    if isinstance(value, Mapping):
        if len(value) > 100:
            raise ValueError("packet object contains too many keys")
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            name = str(key)
            normalized = "".join(c for c in name.lower() if c.isalnum())
            if any(part in normalized for part in _FORBIDDEN_FIELD_PARTS):
                raise ValueError(f"sensitive field forbidden in packet: {name}")
            cleaned[name] = sanitize_packet_payload(child, depth=depth + 1)
        return cleaned
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        if len(value) > 200:
            raise ValueError("packet sequence is too large")
        return [sanitize_packet_payload(item, depth=depth + 1) for item in value]
    raise TypeError(f"unsupported packet type: {type(value).__name__}")


@dataclass(frozen=True)
class AIIntentOutputV2:
    action: AIAction
    confidence: Decimal
    uncertainty: Decimal
    reason_codes: tuple[str, ...]
    rationale: str
    evidence_refs: tuple[str, ...]
    hypothesis_id: str
    lane_id: str
    symbol: str | None = None
    side: str | None = None
    amount_usd: Decimal | None = None
    stop_loss_fraction: Decimal | None = None
    take_profit_fraction: Decimal | None = None
    max_holding_seconds: int | None = None
    max_slippage_bps: Decimal | None = None
    partial_close_fraction: Decimal | None = None
    invalidation_conditions: tuple[str, ...] = ()
    candidate_id: str | None = None

    def selected_candidate(self, packet: DecisionPacketV2) -> Mapping[str, Any] | None:
        if not self.candidate_id:
            return None
        matches = [
            candidate
            for candidate in packet.candidates
            if str(candidate.get("candidate_id", "")) == self.candidate_id
        ]
        if len(matches) != 1:
            raise ValueError("AI OPEN candidate is not bound to exactly one packet candidate")
        return matches[0]

    def validate(self, packet: DecisionPacketV2) -> None:
        if self.lane_id != packet.lane:
            raise ValueError("AI lane attribution does not match the immutable packet")
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("AI confidence outside [0,1]")
        if not Decimal("0") <= self.uncertainty <= Decimal("1"):
            raise ValueError("AI uncertainty outside [0,1]")
        if not self.reason_codes or len(self.reason_codes) > 8:
            raise ValueError("AI reason codes are missing or oversized")
        if not self.rationale.strip() or len(self.rationale) > 1200:
            raise ValueError("AI rationale is missing or oversized")
        if not set(self.evidence_refs) <= set(packet.exact_evidence_refs):
            raise ValueError("AI output references evidence not present in the packet")
        if self.action is AIAction.OPEN:
            if packet.mode != "ENTRY_REVIEW" or packet.position is not None:
                raise ValueError("OPEN is allowed only for an entry-review packet")
            candidate = self.selected_candidate(packet)
            if candidate is None:
                raise ValueError("OPEN requires an exact deterministic candidate_id")
            plan = candidate.get("execution_plan")
            if candidate.get("executable") is not True or not isinstance(plan, Mapping):
                raise ValueError("OPEN candidate has no deterministic executable plan")
            forbidden_model_terms = (
                self.symbol,
                self.side,
                self.amount_usd,
                self.stop_loss_fraction,
                self.take_profit_fraction,
                self.max_holding_seconds,
                self.max_slippage_bps,
            )
            if any(value is not None for value in forbidden_model_terms):
                raise ValueError("OPEN terms must come only from the deterministic candidate plan")
            if self.hypothesis_id != str(candidate.get("strategy_id", "")):
                raise ValueError(
                    "AI hypothesis does not match the selected deterministic candidate"
                )
        elif self.action is AIAction.PARTIAL_CLOSE:
            if self.partial_close_fraction is None or not Decimal(
                "0"
            ) < self.partial_close_fraction < Decimal("1"):
                raise ValueError("PARTIAL_CLOSE requires a fraction in (0,1)")
            if packet.position is None:
                raise ValueError("PARTIAL_CLOSE requires an open position")
        elif self.action is AIAction.CLOSE and packet.position is None:
            raise ValueError("CLOSE requires an open position")


@dataclass(frozen=True)
class CalibrationBin:
    lower: Decimal
    upper: Decimal
    count: int
    mean_confidence: Decimal
    positive_rate: Decimal


@dataclass(frozen=True)
class CalibrationReport:
    observations: int
    brier_score: Decimal
    bins: tuple[CalibrationBin, ...]


class ConfidenceCalibrator:
    def evaluate(
        self,
        confidences: Sequence[Decimal],
        outcomes: Sequence[int],
        *,
        bins: int = 10,
    ) -> CalibrationReport:
        if len(confidences) != len(outcomes) or not confidences:
            raise ValueError("calibration observations are invalid")
        if bins < 2:
            raise ValueError("at least two calibration bins are required")
        if any(not Decimal("0") <= value <= Decimal("1") for value in confidences):
            raise ValueError("confidence outside [0,1]")
        if any(value not in {0, 1} for value in outcomes):
            raise ValueError("outcomes must be binary")
        brier = sum(
            (
                (confidence - Decimal(outcome)) ** 2
                for confidence, outcome in zip(confidences, outcomes, strict=True)
            ),
            Decimal("0"),
        ) / Decimal(len(confidences))
        rows: list[CalibrationBin] = []
        step = Decimal("1") / Decimal(bins)
        for index in range(bins):
            lower = step * Decimal(index)
            upper = Decimal("1") if index == bins - 1 else step * Decimal(index + 1)
            selected = [
                (c, o)
                for c, o in zip(confidences, outcomes, strict=True)
                if ((lower <= c <= upper) if index == bins - 1 else (lower <= c < upper))
            ]
            if not selected:
                continue
            rows.append(
                CalibrationBin(
                    lower,
                    upper,
                    len(selected),
                    sum((c for c, _ in selected), Decimal("0")) / Decimal(len(selected)),
                    sum((Decimal(o) for _, o in selected), Decimal("0")) / Decimal(len(selected)),
                )
            )
        return CalibrationReport(len(confidences), brier, tuple(rows))

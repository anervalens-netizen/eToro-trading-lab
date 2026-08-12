from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .calendar_v2 import load_market_calendar_release
from .codec_v2 import decode_dataclass
from .strict_parsing_v2 import load_strict_json_object, strict_int, strict_object, strict_string

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,31}")


class CandidateEngineIdentityV2(Protocol):
    version: str

    @property
    def engine_hash(self) -> str: ...

    @property
    def parameters_hash(self) -> str: ...


@dataclass(frozen=True)
class StrategyReleaseManifestV2:
    """Immutable evidence references for one executable candidate-engine release.

    The manifest records evidence identities and hashes; it never manufactures or evaluates
    research evidence. A trusted deployment must pin the manifest hash independently.
    """

    strategy_release_id: str
    engine_version: str
    engine_hash: str
    parameters_hash: str
    feature_schema_id: str
    feature_schema_hash: str
    calendar_release_id: str
    calendar_hash: str
    cost_model_release_id: str
    cost_model_hash: str
    observed_round_trip_cost_bps_p95: dict[str, Decimal]
    cost_observation_sample_size: int
    cost_observed_through: datetime
    cost_stress_multiple: Decimal
    point_in_time_dataset_id: str
    point_in_time_dataset_hash: str
    execution_simulator_id: str
    execution_simulator_hash: str
    oos_evidence_id: str
    oos_evidence_hash: str
    promotion_decision_id: str
    promotion_evidence_hash: str
    soak_evidence_id: str
    soak_evidence_hash: str
    oos_gate_passed: bool
    promotion_decision: str
    soak_gate_passed: bool
    adverse_execution_gate_passed: bool
    cost_stress_gate_passed: bool
    valid_from: datetime
    expires_at: datetime
    revoked: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("strategy release schema version is invalid")
        if any(
            type(value) is not bool
            for value in (
                self.oos_gate_passed,
                self.soak_gate_passed,
                self.adverse_execution_gate_passed,
                self.cost_stress_gate_passed,
                self.revoked,
            )
        ):
            raise ValueError("strategy release gate flags must be exact booleans")
        if (
            self.valid_from.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.cost_observed_through.tzinfo is None
        ):
            raise ValueError("strategy release timestamps must be timezone-aware")
        if self.expires_at <= self.valid_from:
            raise ValueError("strategy release validity interval is invalid")
        if (
            type(self.cost_observation_sample_size) is not int
            or self.cost_observation_sample_size < 100
        ):
            raise ValueError("strategy release cost sample is insufficient")
        if not self.cost_stress_multiple.is_finite() or self.cost_stress_multiple < Decimal("2"):
            raise ValueError("strategy release requires a 2x cost stress")
        if not isinstance(self.observed_round_trip_cost_bps_p95, dict) or not (
            self.observed_round_trip_cost_bps_p95
        ):
            raise ValueError("strategy release requires observed per-symbol costs")
        for symbol, value in self.observed_round_trip_cost_bps_p95.items():
            if type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None:
                raise ValueError("strategy release cost symbol is invalid")
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError("strategy release observed costs must be finite and positive")

    def canonical(self) -> str:
        value = asdict(self)
        value["valid_from"] = self.valid_from.astimezone(UTC).isoformat()
        value["expires_at"] = self.expires_at.astimezone(UTC).isoformat()
        value["cost_observed_through"] = self.cost_observed_through.astimezone(UTC).isoformat()
        value["cost_stress_multiple"] = str(self.cost_stress_multiple)
        value["observed_round_trip_cost_bps_p95"] = {
            symbol: str(cost)
            for symbol, cost in sorted(self.observed_round_trip_cost_bps_p95.items())
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @property
    def manifest_hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


@dataclass(frozen=True)
class VerifiedStrategyReleaseV2:
    manifest: StrategyReleaseManifestV2
    manifest_hash: str
    verified_at: datetime

    @property
    def strategy_release_id(self) -> str:
        return self.manifest.strategy_release_id

    def stressed_cost_bps(self, symbol: str) -> Decimal:
        try:
            observed = self.manifest.observed_round_trip_cost_bps_p95[symbol.upper()]
        except KeyError as exc:
            raise PermissionError("strategy release has no observed cost for symbol") from exc
        return observed * self.manifest.cost_stress_multiple


class StrategyReleaseVerifierV2:
    """Fail-closed verifier bound to deployment-pinned evidence identities."""

    def __init__(
        self,
        engine: CandidateEngineIdentityV2,
        *,
        trusted_manifest_hash: str,
        expected_feature_schema_hash: str,
        expected_calendar_hash: str,
        expected_cost_model_hash: str,
    ) -> None:
        values = (
            trusted_manifest_hash,
            expected_feature_schema_hash,
            expected_calendar_hash,
            expected_cost_model_hash,
        )
        if any(not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in values):
            raise ValueError("strategy release verifier requires pinned SHA-256 evidence")
        self.engine = engine
        self.trusted_manifest_hash = trusted_manifest_hash
        self.expected_feature_schema_hash = expected_feature_schema_hash
        self.expected_calendar_hash = expected_calendar_hash
        self.expected_cost_model_hash = expected_cost_model_hash

    def verify(
        self, manifest: StrategyReleaseManifestV2, *, now: datetime
    ) -> VerifiedStrategyReleaseV2:
        if now.tzinfo is None:
            raise ValueError("strategy release verification time must be timezone-aware")
        current = now.astimezone(UTC)
        ids = (
            manifest.strategy_release_id,
            manifest.engine_version,
            manifest.feature_schema_id,
            manifest.calendar_release_id,
            manifest.cost_model_release_id,
            manifest.point_in_time_dataset_id,
            manifest.execution_simulator_id,
            manifest.oos_evidence_id,
            manifest.promotion_decision_id,
            manifest.soak_evidence_id,
        )
        hashes = (
            manifest.engine_hash,
            manifest.parameters_hash,
            manifest.feature_schema_hash,
            manifest.calendar_hash,
            manifest.cost_model_hash,
            manifest.point_in_time_dataset_hash,
            manifest.execution_simulator_hash,
            manifest.oos_evidence_hash,
            manifest.promotion_evidence_hash,
            manifest.soak_evidence_hash,
        )
        failures: list[str] = []
        if manifest.schema_version != 1:
            failures.append("unsupported_manifest_schema")
        if any(not isinstance(value, str) or _ID.fullmatch(value) is None for value in ids):
            failures.append("invalid_evidence_id")
        if any(not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in hashes):
            failures.append("invalid_evidence_hash")
        if manifest.manifest_hash != self.trusted_manifest_hash:
            failures.append("untrusted_manifest_hash")
        if manifest.engine_version != self.engine.version:
            failures.append("engine_version_mismatch")
        if manifest.engine_hash != self.engine.engine_hash:
            failures.append("engine_hash_mismatch")
        if manifest.parameters_hash != self.engine.parameters_hash:
            failures.append("parameters_hash_mismatch")
        if manifest.feature_schema_hash != self.expected_feature_schema_hash:
            failures.append("feature_schema_hash_mismatch")
        if manifest.calendar_hash != self.expected_calendar_hash:
            failures.append("calendar_hash_mismatch")
        if manifest.cost_model_hash != self.expected_cost_model_hash:
            failures.append("cost_model_hash_mismatch")
        if not manifest.oos_gate_passed:
            failures.append("oos_gate_not_passed")
        if manifest.promotion_decision != "PROMOTE":
            failures.append("strategy_not_promoted")
        if not manifest.soak_gate_passed:
            failures.append("soak_gate_not_passed")
        if not manifest.adverse_execution_gate_passed:
            failures.append("adverse_execution_gate_not_passed")
        if not manifest.cost_stress_gate_passed:
            failures.append("cost_stress_gate_not_passed")
        cost_observed = manifest.cost_observed_through.astimezone(UTC)
        if cost_observed > current or (current - cost_observed).total_seconds() > 30 * 86400:
            failures.append("observed_cost_model_stale")
        if manifest.revoked:
            failures.append("strategy_release_revoked")
        if current < manifest.valid_from.astimezone(UTC):
            failures.append("strategy_release_not_yet_valid")
        if current >= manifest.expires_at.astimezone(UTC):
            failures.append("strategy_release_expired")
        if failures:
            raise PermissionError("strategy release rejected: " + ",".join(sorted(set(failures))))
        return VerifiedStrategyReleaseV2(manifest, manifest.manifest_hash, current)


def load_and_verify_strategy_release(
    *,
    manifest_path: str | Path,
    trust_path: str | Path,
    engine: CandidateEngineIdentityV2,
    expected_feature_schema_hash: str,
    expected_cost_model_hash: str,
    now: datetime,
) -> VerifiedStrategyReleaseV2:
    trust = strict_object(
        load_strict_json_object(trust_path),
        label="strategy deployment trust",
        required=("schema_version", "manifest_sha256", "calendar_sha256"),
    )
    if strict_int(trust["schema_version"], label="strategy trust schema") != 1:
        raise ValueError("strategy trust schema version is unsupported")
    manifest_hash = strict_string(trust["manifest_sha256"], label="strategy trusted manifest hash")
    calendar_hash = strict_string(trust["calendar_sha256"], label="strategy trusted calendar hash")
    manifest = decode_dataclass(
        StrategyReleaseManifestV2,
        load_strict_json_object(manifest_path),
    )
    return StrategyReleaseVerifierV2(
        engine,
        trusted_manifest_hash=manifest_hash,
        expected_feature_schema_hash=expected_feature_schema_hash,
        expected_calendar_hash=calendar_hash,
        expected_cost_model_hash=expected_cost_model_hash,
    ).verify(manifest, now=now)


def require_deployed_strategy_release(
    engine: CandidateEngineIdentityV2,
    *,
    expected_feature_schema_hash: str,
    expected_cost_model_hash: str,
    now: datetime,
) -> VerifiedStrategyReleaseV2:
    manifest_path = os.getenv(
        "ETORO_V2_STRATEGY_RELEASE_FILE",
        "/etc/etoro-agent/v2-strategy-release.json",
    )
    trust_path = os.getenv(
        "ETORO_V2_STRATEGY_TRUST_FILE",
        "/etc/etoro-agent/v2-strategy-trust.json",
    )
    try:
        calendar_path = os.getenv(
            "ETORO_V2_MARKET_CALENDAR_FILE",
            "config/market-calendar-v2.json",
        )
        calendar = load_market_calendar_release(calendar_path)
        verified = load_and_verify_strategy_release(
            manifest_path=manifest_path,
            trust_path=trust_path,
            engine=engine,
            expected_feature_schema_hash=expected_feature_schema_hash,
            expected_cost_model_hash=expected_cost_model_hash,
            now=now,
        )
        if verified.manifest.calendar_hash != calendar.release_hash:
            raise PermissionError("strategy release calendar does not match deployed calendar")
        if not calendar.valid_from <= now.astimezone(UTC) < calendar.valid_until:
            raise PermissionError("deployed market calendar is stale")
        return verified
    except (OSError, ValueError, TypeError) as exc:
        raise PermissionError("promoted strategy release evidence is unavailable") from exc

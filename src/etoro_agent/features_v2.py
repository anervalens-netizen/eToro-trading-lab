from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping, Sequence

from .cost_model_v2 import CalibratedCostProfile
from .domain_v2 import ONE, ZERO


@dataclass(frozen=True)
class FeatureSnapshotV2:
    snapshot_id: str
    symbol: str
    event_time: datetime
    feature_version: str
    values: Mapping[str, Decimal]
    source_snapshot_ids: tuple[str, ...]
    data_quality_ok: bool


@dataclass(frozen=True)
class TradabilityDecisionV2:
    tradable: bool
    reasons: tuple[str, ...]
    expected_edge_bps: Decimal
    stressed_cost_bps: Decimal


def build_feature_snapshot(
    symbol: str,
    event_time: datetime,
    values: Mapping[str, Decimal],
    source_snapshot_ids: Sequence[str],
    *,
    feature_version: str,
    data_quality_ok: bool,
) -> FeatureSnapshotV2:
    if event_time.tzinfo is None:
        raise ValueError("feature event_time must be timezone-aware")
    if not source_snapshot_ids:
        raise ValueError("feature snapshot needs immutable source ids")
    if any(not value.is_finite() for value in values.values()):
        raise ValueError("feature snapshot contains non-finite values")
    canonical = json.dumps(
        {
            "symbol": symbol.upper(),
            "event_time": event_time.astimezone(timezone.utc).isoformat(),
            "feature_version": feature_version,
            "values": {key: str(value) for key, value in sorted(values.items())},
            "source_snapshot_ids": list(source_snapshot_ids),
            "data_quality_ok": data_quality_ok,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return FeatureSnapshotV2(
        f"feature-{hashlib.sha256(canonical.encode()).hexdigest()[:24]}",
        symbol.upper(), event_time.astimezone(timezone.utc), feature_version,
        dict(values), tuple(source_snapshot_ids), data_quality_ok,
    )


def order_flow_imbalance(bid_sizes: Sequence[Decimal], ask_sizes: Sequence[Decimal]) -> Decimal:
    if not bid_sizes or len(bid_sizes) != len(ask_sizes):
        raise ValueError("OFI size series are invalid")
    bid = sum(bid_sizes, ZERO)
    ask = sum(ask_sizes, ZERO)
    total = bid + ask
    return ZERO if total == ZERO else (bid - ask) / total


def spread_expansion_bps(spreads_bps: Sequence[Decimal], lookback: int = 20) -> Decimal:
    if len(spreads_bps) < max(2, lookback):
        raise ValueError("spread expansion needs sufficient history")
    baseline = sum(spreads_bps[-lookback:-1], ZERO) / Decimal(lookback - 1)
    return spreads_bps[-1] - baseline


class TradabilityGateV2:
    def __init__(self, *, minimum_edge_cost_multiple: Decimal = Decimal("1.5")) -> None:
        if minimum_edge_cost_multiple <= ZERO:
            raise ValueError("edge/cost multiple must be positive")
        self.minimum_edge_cost_multiple = minimum_edge_cost_multiple

    def evaluate(
        self,
        feature: FeatureSnapshotV2,
        *,
        expected_edge_bps: Decimal,
        costs: CalibratedCostProfile,
        cost_stress_multiplier: Decimal = ONE,
    ) -> TradabilityDecisionV2:
        reasons: list[str] = []
        if not feature.data_quality_ok:
            reasons.append("data_quality_failure")
        stressed = costs.stressed_round_trip_bps(cost_stress_multiplier)
        if expected_edge_bps <= ZERO:
            reasons.append("non_positive_expected_edge")
        if expected_edge_bps < stressed * self.minimum_edge_cost_multiple:
            reasons.append("edge_does_not_clear_stressed_costs")
        return TradabilityDecisionV2(not reasons, tuple(sorted(set(reasons))), expected_edge_bps, stressed)

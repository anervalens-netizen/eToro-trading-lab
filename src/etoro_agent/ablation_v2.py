from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LaneMetricsV2:
    lane_id: str
    closed_trades: int
    net_pnl_usd: Decimal
    max_drawdown_fraction: Decimal
    profit_factor: Decimal
    expectancy_usd: Decimal
    model_calls: int = 0
    model_cost_usd: Decimal = Decimal("0")


@dataclass(frozen=True)
class LaneComparisonV2:
    baseline_lane: str
    candidate_lane: str
    incremental_net_pnl_usd: Decimal
    incremental_expectancy_usd: Decimal
    drawdown_delta: Decimal
    model_cost_usd: Decimal
    value_add_after_model_cost_usd: Decimal
    improves_baseline: bool


def compare_lanes(baseline: LaneMetricsV2, candidate: LaneMetricsV2) -> LaneComparisonV2:
    incremental = candidate.net_pnl_usd - baseline.net_pnl_usd
    value_add = incremental - candidate.model_cost_usd
    improves = (
        value_add > 0
        and candidate.expectancy_usd > baseline.expectancy_usd
        and candidate.max_drawdown_fraction <= baseline.max_drawdown_fraction
    )
    return LaneComparisonV2(
        baseline.lane_id,
        candidate.lane_id,
        incremental,
        candidate.expectancy_usd - baseline.expectancy_usd,
        candidate.max_drawdown_fraction - baseline.max_drawdown_fraction,
        candidate.model_cost_usd,
        value_add,
        improves,
    )


def rank_lanes(metrics: Sequence[LaneMetricsV2]) -> tuple[LaneMetricsV2, ...]:
    return tuple(
        sorted(
            metrics,
            key=lambda item: (
                -(item.net_pnl_usd - item.model_cost_usd),
                item.max_drawdown_fraction,
                -item.expectancy_usd,
                item.lane_id,
            ),
        )
    )

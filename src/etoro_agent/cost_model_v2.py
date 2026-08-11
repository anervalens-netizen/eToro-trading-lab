from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from .domain_v2 import BPS, ZERO


@dataclass(frozen=True)
class CostObservation:
    symbol: str
    spread_bps: Decimal
    slippage_bps: Decimal
    latency_ms: int
    financing_bps_per_day: Decimal = ZERO
    external_cost_bps: Decimal = ZERO


@dataclass(frozen=True)
class CalibratedCostProfile:
    symbol: str
    spread_bps_p50: Decimal
    spread_bps_p95: Decimal
    slippage_bps_p50: Decimal
    slippage_bps_p95: Decimal
    latency_ms_p50: int
    latency_ms_p95: int
    financing_bps_per_day: Decimal
    external_cost_bps: Decimal
    observations: int

    def stressed_round_trip_bps(self, multiplier: Decimal = Decimal("1")) -> Decimal:
        if multiplier <= ZERO:
            raise ValueError("stress multiplier must be positive")
        return multiplier * (
            self.spread_bps_p95
            + Decimal("2") * self.slippage_bps_p95
            + Decimal("2") * self.external_cost_bps
        )

    def break_even_return_fraction(self, multiplier: Decimal = Decimal("1")) -> Decimal:
        return self.stressed_round_trip_bps(multiplier) / BPS


def _percentile(values: list[Decimal], fraction: Decimal) -> Decimal:
    if not values:
        raise ValueError("cannot calculate percentile of empty observations")
    ordered = sorted(values)
    index = int((Decimal(len(ordered) - 1) * fraction).to_integral_value())
    return ordered[max(0, min(index, len(ordered) - 1))]


def _percentile_int(values: list[int], fraction: Decimal) -> int:
    ordered = sorted(values)
    index = int((Decimal(len(ordered) - 1) * fraction).to_integral_value())
    return ordered[max(0, min(index, len(ordered) - 1))]


class CostCalibrator:
    def calibrate(
        self, symbol: str, observations: Iterable[CostObservation]
    ) -> CalibratedCostProfile:
        rows = [item for item in observations if item.symbol.upper() == symbol.upper()]
        if len(rows) < 5:
            raise ValueError("at least five cost observations are required")
        spreads = [item.spread_bps for item in rows]
        slippage = [item.slippage_bps for item in rows]
        latencies = [item.latency_ms for item in rows]
        financing = [item.financing_bps_per_day for item in rows]
        external = [item.external_cost_bps for item in rows]
        return CalibratedCostProfile(
            symbol.upper(),
            _percentile(spreads, Decimal("0.50")),
            _percentile(spreads, Decimal("0.95")),
            _percentile(slippage, Decimal("0.50")),
            _percentile(slippage, Decimal("0.95")),
            _percentile_int(latencies, Decimal("0.50")),
            _percentile_int(latencies, Decimal("0.95")),
            Decimal(str(median(financing))),
            Decimal(str(median(external))),
            len(rows),
        )

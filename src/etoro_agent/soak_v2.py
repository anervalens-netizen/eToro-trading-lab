from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class SoakDayV2:
    day: date
    trades: int
    net_pnl_usd: Decimal
    max_data_gap_seconds: int
    unknown_orders: int
    reconciliation_delta_usd: Decimal
    critical_incidents: int


@dataclass(frozen=True)
class SoakReportV2:
    calendar_days: int
    active_days: int
    trades: int
    net_pnl_usd: Decimal
    unknown_orders: int
    critical_incidents: int
    max_abs_reconciliation_delta_usd: Decimal
    operational_gate_passed: bool


def evaluate_soak(days: Sequence[SoakDayV2], *, minimum_calendar_days: int = 30) -> SoakReportV2:
    if not days:
        return SoakReportV2(0, 0, 0, Decimal("0"), 0, 0, Decimal("0"), False)
    ordered = sorted(days, key=lambda item: item.day)
    calendar_days = (ordered[-1].day - ordered[0].day).days + 1
    trades = sum(item.trades for item in ordered)
    pnl = sum((item.net_pnl_usd for item in ordered), Decimal("0"))
    unknown = sum(item.unknown_orders for item in ordered)
    critical = sum(item.critical_incidents for item in ordered)
    reconcile = max((abs(item.reconciliation_delta_usd) for item in ordered), default=Decimal("0"))
    operational = (
        calendar_days >= minimum_calendar_days and unknown == 0 and critical == 0 and reconcile == 0
    )
    return SoakReportV2(
        calendar_days,
        sum(item.trades > 0 for item in ordered),
        trades,
        pnl,
        unknown,
        critical,
        reconcile,
        operational,
    )

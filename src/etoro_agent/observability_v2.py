from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class HealthSignal:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class RuntimeMetricsV2:
    generated_at: str
    data_age_seconds: float | None
    websocket_reconnects: int
    pending_unknown_orders: int
    oldest_unknown_age_seconds: float | None
    reconcile_delta_usd: Decimal
    model_failure_rate: Decimal
    audit_chain_valid: bool
    last_backup_at: str | None
    last_anchor_at: str | None
    signals: tuple[HealthSignal, ...]


class HealthEvaluatorV2:
    def evaluate(
        self,
        *,
        now: datetime,
        data_observed_at: datetime | None,
        max_data_age_seconds: int,
        websocket_reconnects: int,
        unknown_orders: Mapping[str, datetime],
        reconcile_delta_usd: Decimal,
        model_failures: int,
        model_runs: int,
        audit_chain_valid: bool,
        last_backup_at: datetime | None,
        last_anchor_at: datetime | None,
        backup_max_age: timedelta = timedelta(hours=26),
        anchor_max_age: timedelta = timedelta(hours=2),
    ) -> RuntimeMetricsV2:
        current = now.astimezone(UTC)
        signals: list[HealthSignal] = []
        age = (
            None
            if data_observed_at is None
            else max(0.0, (current - data_observed_at.astimezone(UTC)).total_seconds())
        )
        if age is None or age > max_data_age_seconds:
            signals.append(HealthSignal("market_data", "HALT_NEW", "feed stale or unavailable"))
        oldest_unknown = None
        if unknown_orders:
            oldest_unknown = max(
                0.0,
                max(
                    (current - value.astimezone(UTC)).total_seconds()
                    for value in unknown_orders.values()
                ),
            )
            signals.append(
                HealthSignal(
                    "orders_unknown", "HALT_NEW", f"{len(unknown_orders)} unresolved order(s)"
                )
            )
        if reconcile_delta_usd != 0:
            signals.append(
                HealthSignal("reconciliation", "HALT_NEW", f"delta={reconcile_delta_usd}")
            )
        failure_rate = Decimal(model_failures) / Decimal(model_runs) if model_runs else Decimal("0")
        if failure_rate > Decimal("0.25"):
            signals.append(HealthSignal("model", "DEGRADED", f"failure_rate={failure_rate}"))
        if not audit_chain_valid:
            signals.append(HealthSignal("audit", "LOCKED", "event chain invalid"))
        if last_backup_at is None or current - last_backup_at.astimezone(UTC) > backup_max_age:
            signals.append(HealthSignal("backup", "DEGRADED", "backup overdue"))
        if last_anchor_at is None or current - last_anchor_at.astimezone(UTC) > anchor_max_age:
            signals.append(HealthSignal("audit_anchor", "HALT_NEW", "audit anchor overdue"))
        return RuntimeMetricsV2(
            current.isoformat(),
            age,
            websocket_reconnects,
            len(unknown_orders),
            oldest_unknown,
            reconcile_delta_usd,
            failure_rate,
            audit_chain_valid,
            None if last_backup_at is None else last_backup_at.astimezone(UTC).isoformat(),
            None if last_anchor_at is None else last_anchor_at.astimezone(UTC).isoformat(),
            tuple(signals),
        )

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .config_v2 import AppConfigV2
from .domain_v2 import OrderStatus, Side
from .etoro_api_current_v2 import (
    BrokerAccountSnapshotV2,
    EtoroPublicApiDemoClientV2,
    decode_broker_order_identity_v2,
)
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .risk_v2 import BrokerTruth
from .runtime_store_v2 import RuntimeStoreV2

RuntimeTruthStoreV2 = RuntimeStoreV2 | PostgresRuntimeStoreV2


def _strict_integral_alias(
    row: Mapping[str, object], aliases: tuple[str, ...], *, label: str
) -> int:
    present = [name for name in aliases if name in row]
    if not present:
        raise ValueError(f"broker {label} is missing")
    values: list[int] = []
    for name in present:
        raw = row[name]
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
            raise ValueError(f"broker {label} is invalid")
        try:
            parsed = Decimal(str(raw).strip())
        except (ArithmeticError, ValueError) as exc:
            raise ValueError(f"broker {label} is invalid") from exc
        if not parsed.is_finite() or parsed <= 0 or parsed != parsed.to_integral_value():
            raise ValueError(f"broker {label} is invalid")
        values.append(int(parsed))
    if any(value != values[0] for value in values[1:]):
        raise ValueError(f"broker {label} aliases disagree")
    return values[0]


def _strict_decimal_alias(
    row: Mapping[str, object], aliases: tuple[str, ...], *, label: str
) -> Decimal:
    present = [name for name in aliases if name in row]
    if not present:
        raise ValueError(f"broker {label} is missing")
    values: list[Decimal] = []
    for name in present:
        raw = row[name]
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
            raise ValueError(f"broker {label} is invalid")
        try:
            parsed = abs(Decimal(str(raw).strip()))
        except (ArithmeticError, ValueError) as exc:
            raise ValueError(f"broker {label} is invalid") from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError(f"broker {label} is invalid")
        values.append(parsed)
    if any(value != values[0] for value in values[1:]):
        raise ValueError(f"broker {label} aliases disagree")
    return values[0]


def _period_loss_metrics(
    realized_events: tuple[tuple[datetime, Decimal], ...],
    *,
    unrealized_usd: Decimal,
    now: datetime,
) -> tuple[Decimal, Decimal, Decimal]:
    current = now.astimezone(UTC)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    month_start = day_start.replace(day=1)
    conservative_unrealized = min(Decimal("0"), unrealized_usd)

    def total(since: datetime) -> Decimal:
        return (
            sum(
                (amount for event_time, amount in realized_events if event_time >= since),
                Decimal("0"),
            )
            + conservative_unrealized
        )

    return total(day_start), total(week_start), total(month_start)


def _risk_history(
    store: RuntimeTruthStoreV2,
    *,
    unrealized_usd: Decimal,
    now: datetime,
) -> tuple[Decimal, Decimal, Decimal, datetime | None]:
    current = now.astimezone(UTC)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    earliest = min(day_start - timedelta(days=day_start.weekday()), day_start.replace(day=1))
    if isinstance(store, PostgresRuntimeStoreV2):
        with store.connection.cursor() as cursor:
            cursor.execute(
                """SELECT event_time,payload->>'realized_delta_usd'
                   FROM v2_events
                   WHERE event_type IN ('PositionReduced','PositionClosed')
                     AND event_time >= %s
                   ORDER BY event_time,sequence""",
                (earliest,),
            )
            raw_events = cursor.fetchall()
            cursor.execute("SELECT MAX(event_time) FROM v2_fills")
            last_row = cursor.fetchone()
    else:
        raw_events = tuple(
            (
                datetime.fromisoformat(str(row[0])),
                json.loads(str(row[1])).get("realized_delta_usd"),
            )
            for row in store.db.execute(
                """SELECT event_time,payload_json FROM v2_events
                   WHERE event_type IN ('PositionReduced','PositionClosed')
                     AND event_time >= ?
                   ORDER BY event_time,sequence""",
                (earliest.isoformat(),),
            ).fetchall()
        )
        last_row = store.db.execute("SELECT MAX(event_time) FROM v2_fills").fetchone()

    events: list[tuple[datetime, Decimal]] = []
    for event_time, raw_amount in raw_events:
        if raw_amount is None:
            raise RuntimeError("dated realized P&L provenance is incomplete")
        amount = Decimal(str(raw_amount))
        if not amount.is_finite():
            raise RuntimeError("dated realized P&L provenance is invalid")
        events.append((event_time, amount))
    last_trade_at = None if last_row is None or last_row[0] is None else last_row[0]
    if isinstance(last_trade_at, str):
        last_trade_at = datetime.fromisoformat(last_trade_at)
    daily, weekly, monthly = _period_loss_metrics(
        tuple(events), unrealized_usd=unrealized_usd, now=current
    )
    return daily, weekly, monthly, last_trade_at


def broker_truth_v2(
    store: RuntimeTruthStoreV2,
    client: EtoroPublicApiDemoClientV2,
    *,
    config: AppConfigV2,
    now: datetime,
    snapshot: BrokerAccountSnapshotV2 | None = None,
) -> BrokerTruth:
    """Build the one canonical broker/local truth used at decision and final submit."""

    account = snapshot or client.account_snapshot()
    if not isinstance(account, BrokerAccountSnapshotV2):
        raise TypeError("broker account snapshot contract is invalid")
    peak = store.update_peak_equity(account.equity_usd, at=now)
    daily_pnl, weekly_pnl, monthly_pnl, last_trade_at = _risk_history(
        store, unrealized_usd=account.unrealized_pnl_usd, now=now
    )

    local_positions = store.positions(open_only=True)
    broker_by_id = {
        str(position.get("positionID", position.get("positionId", ""))).strip(): position
        for position in account.positions
        if isinstance(position, Mapping)
        and str(position.get("positionID", position.get("positionId", ""))).strip()
    }
    local_by_id = {
        str(position.broker_position_id): position
        for position in local_positions
        if position.broker_position_id is not None
    }
    failures: list[str] = list(account.foreign_activity)
    if len(local_by_id) != len(local_positions):
        failures.append("local_position_without_broker_id")
    for broker_id in sorted(set(local_by_id) - set(broker_by_id)):
        failures.append(f"missing_broker_position:{broker_id}")
    for broker_id in sorted(set(broker_by_id) - set(local_by_id)):
        failures.append(f"unbound_broker_position:{broker_id}")
    for broker_id in sorted(set(local_by_id) & set(broker_by_id)):
        local = local_by_id[broker_id]
        broker_position = broker_by_id[broker_id]
        try:
            instrument_id = _strict_integral_alias(
                broker_position,
                ("instrumentID", "instrumentId"),
                label="position instrument identity",
            )
            broker_quantity = _strict_decimal_alias(
                broker_position,
                ("units", "quantity", "unitsOwned", "netUnits"),
                label="position quantity",
            )
            broker_entry = _strict_decimal_alias(
                broker_position,
                ("openRate", "averageOpenRate", "entryPrice"),
                label="position entry price",
            )
        except (ArithmeticError, TypeError, ValueError):
            failures.append(f"invalid_economics:{broker_id}")
            continue
        if instrument_id != config.symbols.get(local.symbol):
            failures.append(f"instrument_mismatch:{broker_id}")
        raw_side = broker_position.get("isBuy")
        broker_side = Side.BUY if raw_side is True else Side.SELL if raw_side is False else None
        if broker_side is not local.side:
            failures.append(f"side_mismatch:{broker_id}")
        if abs(broker_quantity - local.quantity) > Decimal("0.00000001"):
            failures.append(f"quantity_mismatch:{broker_id}")
        if abs(broker_entry - local.entry_price) > max(
            Decimal("0.00000001"), local.entry_price * Decimal("0.0005")
        ):
            failures.append(f"entry_mismatch:{broker_id}")
        raw_amount = broker_position.get("amount")
        if raw_amount is not None:
            broker_notional = abs(Decimal(str(raw_amount)))
            local_notional = local.quantity * local.entry_price
            if not broker_notional.is_finite() or abs(broker_notional - local_notional) > max(
                Decimal("0.01"), local_notional * Decimal("0.02")
            ):
                failures.append(f"exposure_mismatch:{broker_id}")
        for raw_name, local_value, label in (
            ("fees", local.fees_accrued, "fees"),
            ("financing", local.financing_accrued, "financing"),
        ):
            if broker_position.get(raw_name) is None:
                continue
            broker_value = abs(Decimal(str(broker_position[raw_name])))
            if not broker_value.is_finite() or abs(broker_value - local_value) > Decimal("0.01"):
                failures.append(f"{label}_mismatch:{broker_id}")

    local_pending = store.broker_orders_by_status(
        (
            OrderStatus.ACKNOWLEDGED.value,
            OrderStatus.PARTIALLY_FILLED.value,
            OrderStatus.UNKNOWN.value,
        )
    )
    broker_pending = tuple(
        decode_broker_order_identity_v2(row)
        for row in (*account.open_orders, *account.pending_orders)
    )
    matched_broker_rows: set[int] = set()
    for local_order in local_pending:
        broker_order_id = str(local_order.broker_order_id or "").strip()
        client_reference_id = str(local_order.client_order_id or "").strip()
        if not broker_order_id and not client_reference_id:
            failures.append(f"pending_order_unresolved:{local_order.order_command_id}")
            continue
        matches = {
            index
            for index, identity in enumerate(broker_pending)
            if (
                (not broker_order_id or broker_order_id == identity.order_id)
                and (not client_reference_id or client_reference_id == identity.client_reference_id)
            )
        }
        if len(matches) != 1:
            failures.append(f"pending_order_unresolved:{local_order.order_command_id}")
        elif matched_broker_rows & matches:
            failures.append(f"pending_order_reused:{local_order.order_command_id}")
        matched_broker_rows.update(matches)
    if len(matched_broker_rows) != len(broker_pending):
        failures.append("unbound_broker_pending_order")

    return BrokerTruth(
        equity_usd=account.equity_usd,
        peak_equity_usd=peak,
        available_cash_usd=account.available_cash_usd,
        gross_exposure_usd=account.gross_exposure_usd,
        correlated_exposure_usd=account.gross_exposure_usd,
        open_positions=len(account.positions),
        pending_order_notional_usd=(account.pending_manual_orders_usd + account.pending_orders_usd),
        daily_pnl_usd=daily_pnl,
        weekly_pnl_usd=weekly_pnl,
        monthly_pnl_usd=monthly_pnl,
        snapshot_hash=account.snapshot_hash,
        observed_at=account.observed_at,
        last_trade_at=last_trade_at,
        reconciliation_ok=not failures,
        reconciliation_detail=tuple(sorted(set(failures))),
    )

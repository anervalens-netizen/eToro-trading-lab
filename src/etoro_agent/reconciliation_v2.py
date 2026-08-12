from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config_v2 import AppConfigV2, load_config_v2
from .domain_v2 import ExitReason, Fill, OrderStatus, PositionState, Side
from .etoro_api_current_v2 import (
    BrokerOrderIdentityV2,
    EtoroPublicApiDemoClientV2,
    decode_broker_order_identity_v2,
    decode_broker_position_identity_v2,
)
from .kernel_v2 import UnifiedTradingKernel
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .risk_v2 import GlobalRiskKernel
from .systemd_notify_v2 import ready, watchdog


def _dsn(config: AppConfigV2) -> str:
    path = os.getenv("ETORO_V2_POSTGRES_DSN_FILE") or config.postgres_dsn_file
    if not path:
        raise RuntimeError("PostgreSQL DSN credential file is required")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("PostgreSQL DSN credential file is empty")
    return value


def _decimal(row: Mapping[str, Any], names: tuple[str, ...]) -> Decimal | None:
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        try:
            parsed = abs(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _finite_decimal(
    row: Mapping[str, Any],
    names: tuple[str, ...],
    *,
    allow_zero: bool = False,
    absolute: bool = True,
) -> Decimal | None:
    for name in names:
        if name not in row or row.get(name) is None:
            continue
        try:
            value = Decimal(str(row[name]))
        except (InvalidOperation, ValueError, TypeError):
            return None
        if not value.is_finite():
            return None
        parsed = abs(value) if absolute else value
        if (absolute and parsed > 0) or (not absolute and parsed != 0):
            return parsed
        if allow_zero and parsed == 0:
            return parsed
        return None
    return None


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
            return datetime.fromtimestamp(seconds, UTC)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _status_name(payload: Mapping[str, Any]) -> str:
    def normalize(raw: object) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError("broker order status is invalid")
        normalized = "".join(character for character in raw.lower() if character.isalnum())
        if not normalized:
            raise RuntimeError("broker order status is invalid")
        return normalized

    values: list[str] = []
    if "status" in payload:
        raw = payload["status"]
        if isinstance(raw, Mapping):
            aliases = [name for name in ("name", "id") if name in raw]
            if not aliases:
                raise RuntimeError("broker order status mapping is empty")
            values.extend(normalize(raw[name]) for name in aliases)
        else:
            values.append(normalize(raw))
    for name in ("statusName", "state"):
        if name in payload:
            values.append(normalize(payload[name]))
    if not values:
        raise RuntimeError("broker order status is missing")
    if any(value != values[0] for value in values[1:]):
        raise RuntimeError("broker order status aliases conflict")
    allowed = {
        "rejected",
        "cancelled",
        "canceled",
        "expired",
        "failed",
        "created",
        "pending",
        "accepted",
        "open",
        "partiallyfilled",
        "inprogress",
        "filled",
        "completed",
        "executed",
        "closed",
    }
    if values[0] not in allowed:
        raise RuntimeError("broker order status is unsupported")
    return values[0]


def _strict_evidence_timestamp(
    primary: Mapping[str, Any],
    primary_aliases: tuple[str, ...],
    *,
    fallback: Mapping[str, Any],
    fallback_alias: str,
    label: str,
) -> datetime:
    present = [name for name in primary_aliases if name in primary]
    primary_values = [_timestamp(primary[name]) for name in present]
    if any(value is None for value in primary_values):
        raise RuntimeError(f"{label} timestamp is invalid")
    decoded_primary = [value for value in primary_values if value is not None]
    if decoded_primary and any(value != decoded_primary[0] for value in decoded_primary[1:]):
        raise RuntimeError(f"{label} timestamp aliases conflict")
    fallback_value = None
    if fallback_alias in fallback:
        fallback_value = _timestamp(fallback[fallback_alias])
        if fallback_value is None:
            raise RuntimeError(f"{label} fallback timestamp is invalid")
    if decoded_primary:
        if fallback_value is not None and fallback_value != decoded_primary[0]:
            raise RuntimeError(f"{label} cross-object timestamps conflict")
        return decoded_primary[0]
    if fallback_value is None:
        raise RuntimeError(f"{label} timestamp is missing")
    return fallback_value


def _position_id(row: Mapping[str, Any]) -> str:
    return str(row.get("positionID", row.get("positionId", ""))).strip()


def _instrument_id(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("instrumentID", row.get("instrumentId", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def _strict_instrument_identity(row: Mapping[str, Any], *, label: str) -> int:
    present = [name for name in ("instrumentID", "instrumentId") if name in row]
    if not present:
        raise RuntimeError(f"{label} instrument identity is missing")
    values: list[int] = []
    for name in present:
        raw = row[name]
        if type(raw) is not int or raw <= 0:
            raise RuntimeError(f"{label} instrument identity is invalid")
        values.append(raw)
    if any(value != values[0] for value in values[1:]):
        raise RuntimeError(f"{label} instrument identity aliases conflict")
    return values[0]


def _strict_symbol(row: Mapping[str, Any], *, expected: str, label: str) -> str:
    if "symbol" not in row:
        raise RuntimeError(f"{label} symbol is missing")
    symbol = row["symbol"]
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper():
        raise RuntimeError(f"{label} symbol is invalid")
    if symbol != expected:
        raise RuntimeError(f"{label} symbol mismatch")
    return symbol


def _strict_pending_position_references(row: Mapping[str, Any]) -> frozenset[str]:
    references: set[str] = set()
    singular_present = any(name in row for name in ("positionID", "positionId"))
    if singular_present:
        references.add(decode_broker_position_identity_v2(row))
    list_aliases = [name for name in ("positionIDs", "positionIds") if name in row]
    decoded_lists: list[tuple[str, ...]] = []
    for name in list_aliases:
        raw_values = row[name]
        if not isinstance(raw_values, list):
            raise RuntimeError("DEMO pending position references are invalid")
        values: list[str] = []
        for raw in raw_values:
            if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                raise RuntimeError("DEMO pending position reference is invalid")
            normalized = str(raw).strip()
            if not normalized:
                raise RuntimeError("DEMO pending position reference is invalid")
            values.append(normalized)
        if len(values) != len(set(values)):
            raise RuntimeError("DEMO pending position references are duplicated")
        decoded_lists.append(tuple(values))
    if decoded_lists and any(values != decoded_lists[0] for values in decoded_lists[1:]):
        raise RuntimeError("DEMO pending position reference aliases conflict")
    if decoded_lists:
        references.update(decoded_lists[0])
    return frozenset(references)


def _strict_optional_identity_family(
    row: Mapping[str, Any], aliases: tuple[str, ...], *, label: str
) -> str | None:
    present = [name for name in aliases if name in row]
    if not present:
        return None
    values: list[str] = []
    for name in present:
        raw = row[name]
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise RuntimeError(f"{label} identity is invalid")
        normalized = str(raw).strip()
        if not normalized:
            raise RuntimeError(f"{label} identity is invalid")
        values.append(normalized)
    if any(value != values[0] for value in values[1:]):
        raise RuntimeError(f"{label} identity aliases conflict")
    return values[0]


def _strict_history_identities(
    row: Mapping[str, Any],
) -> tuple[str | None, BrokerOrderIdentityV2 | None, str | None]:
    trade_id = _strict_optional_identity_family(
        row, ("tradeID", "tradeId"), label="broker history trade"
    )
    has_order_identity = any(
        name in row
        for name in (
            "orderID",
            "orderId",
            "referenceID",
            "referenceId",
            "requestID",
            "requestId",
        )
    )
    order_identity = decode_broker_order_identity_v2(row) if has_order_identity else None
    if trade_id is None and order_identity is None:
        raise RuntimeError("DEMO trading history row lacks trade/order identity")
    position_id = (
        decode_broker_position_identity_v2(row)
        if any(name in row for name in ("positionID", "positionId"))
        else None
    )
    return trade_id, order_identity, position_id


def _strict_broker_side(row: Mapping[str, Any], *, label: str) -> Side:
    aliases = [name for name in ("isBuy", "side", "direction") if name in row]
    if not aliases:
        raise RuntimeError(f"{label} side is missing")
    normalized: list[Side] = []
    for name in aliases:
        raw = row[name]
        if name == "isBuy":
            if type(raw) is not bool:
                raise RuntimeError(f"{label} side alias is invalid")
            normalized.append(Side.BUY if raw else Side.SELL)
            continue
        if type(raw) is not str:
            raise RuntimeError(f"{label} side alias is invalid")
        value = raw.strip().lower()
        if value in {"buy", "long"}:
            normalized.append(Side.BUY)
        elif value in {"sell", "short"}:
            normalized.append(Side.SELL)
        else:
            raise RuntimeError(f"{label} side alias is invalid")
    if any(value is not normalized[0] for value in normalized[1:]):
        raise RuntimeError(f"{label} side aliases conflict")
    return normalized[0]


class DemoReconciliationWorkerV2:
    """Read-only broker reconciliation for ACK/UNKNOWN DEMO orders.

    It can project a fill only when broker position identity, quantity and entry
    price are exact. Ambiguous closes remain manual-review and lock new risk.
    """

    def __init__(
        self,
        config: AppConfigV2,
        store: PostgresRuntimeStoreV2,
        kernel: UnifiedTradingKernel,
        client: EtoroPublicApiDemoClientV2 | None = None,
        *,
        grace_seconds: int = 120,
    ) -> None:
        if grace_seconds < 30:
            raise ValueError("reconciliation grace must be at least 30 seconds")
        self.config = config
        self.store = store
        self.kernel = kernel
        self.client = client or EtoroPublicApiDemoClientV2()
        self.grace = timedelta(seconds=grace_seconds)

    def _portfolio(
        self,
    ) -> tuple[
        tuple[Mapping[str, Any], ...],
        tuple[Mapping[str, Any], ...],
        str,
    ]:
        response = self.client.demo_portfolio()
        if not response.ok or not isinstance(response.body, dict):
            raise RuntimeError("DEMO portfolio reconciliation is unavailable")
        root = response.body.get("clientPortfolio", response.body)
        if not isinstance(root, Mapping):
            raise RuntimeError("DEMO portfolio reconciliation shape is invalid")
        rows = root.get("positions", [])
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise RuntimeError("DEMO position collection is invalid")
        position_ids = [decode_broker_position_identity_v2(row) for row in rows]
        if len(position_ids) != len(set(position_ids)):
            raise RuntimeError("DEMO position identities are duplicated")
        pending_collections: dict[str, tuple[Mapping[str, Any], ...]] = {}
        order_identities: dict[str, tuple[BrokerOrderIdentityV2, ...]] = {}
        for name in ("orders", "ordersForOpen"):
            collection = root.get(name, [])
            if not isinstance(collection, list) or not all(
                isinstance(row, Mapping) for row in collection
            ):
                raise RuntimeError(f"DEMO {name} collection is invalid")
            identities = tuple(decode_broker_order_identity_v2(row) for row in collection)
            for row in collection:
                _strict_pending_position_references(row)
            broker_ids = [value.order_id for value in identities if value.order_id is not None]
            reference_ids = [
                value.client_reference_id
                for value in identities
                if value.client_reference_id is not None
            ]
            if len(broker_ids) != len(set(broker_ids)) or len(reference_ids) != len(
                set(reference_ids)
            ):
                raise RuntimeError(f"DEMO {name} order identities are duplicated")
            pending_collections[name] = tuple(collection)
            order_identities[name] = identities
        orders_broker_ids = {
            value.order_id for value in order_identities["orders"] if value.order_id is not None
        }
        open_broker_ids = {
            value.order_id
            for value in order_identities["ordersForOpen"]
            if value.order_id is not None
        }
        orders_references = {
            value.client_reference_id
            for value in order_identities["orders"]
            if value.client_reference_id is not None
        }
        open_references = {
            value.client_reference_id
            for value in order_identities["ordersForOpen"]
            if value.client_reference_id is not None
        }
        if (orders_broker_ids & open_broker_ids) or (orders_references & open_references):
            raise RuntimeError("DEMO order collections overlap")
        pending = (*pending_collections["orders"], *pending_collections["ordersForOpen"])
        canonical = json.dumps(root, sort_keys=True, separators=(",", ":"), default=str)
        return (
            tuple(rows),
            pending,
            hashlib.sha256(canonical.encode()).hexdigest(),
        )

    def _order_lookup(self, command: Any, order: Any) -> Mapping[str, Any] | None:
        lookup = getattr(self.client, "order_lookup", None)
        if not callable(lookup):
            return None
        response = None
        broker_order_id = str(order.broker_order_id or "").strip()
        if broker_order_id.isdigit() and int(broker_order_id) > 0:
            response = lookup(order_id=broker_order_id)
        if response is None or response.status_code == 404:
            response = lookup(reference_id=command.client_order_id)
        if response.status_code == 404:
            return {}
        if not response.ok or not isinstance(response.body, Mapping):
            raise RuntimeError("DEMO order lookup is unavailable")
        payload = response.body
        expected_action = "close" if command.reduce_only else "open"
        if payload.get("action") != expected_action:
            raise RuntimeError("broker order action differs from local command")
        identity = decode_broker_order_identity_v2(payload)
        if broker_order_id and identity.order_id != broker_order_id:
            raise RuntimeError("broker order lookup broker identity mismatch")
        client_reference_id = str(command.client_order_id or "").strip()
        if (
            identity.client_reference_id is not None
            and identity.client_reference_id != client_reference_id
        ):
            raise RuntimeError("broker order lookup client reference mismatch")
        asset = payload.get("asset")
        if not isinstance(asset, Mapping):
            raise RuntimeError("broker order lookup asset is invalid")
        instrument_id = _strict_instrument_identity(asset, label="broker order lookup")
        if instrument_id != self.config.symbols[command.symbol]:
            raise RuntimeError("broker order lookup instrument mismatch")
        _strict_symbol(asset, expected=command.symbol, label="broker order lookup")
        return payload

    def _close_information(self, order_id: str, command: Any) -> Mapping[str, Any] | None:
        getter = getattr(self.client, "close_order_information", None)
        if not callable(getter) or not order_id.isdigit():
            return None
        response = getter(order_id)
        if response.status_code == 404:
            return {}
        if not response.ok or not isinstance(response.body, Mapping):
            raise RuntimeError("DEMO close-order truth is unavailable")
        payload = response.body
        identity = decode_broker_order_identity_v2(payload)
        if identity.order_id != order_id:
            raise RuntimeError("close-order lookup broker identity mismatch")
        client_reference_id = str(command.client_order_id or "").strip()
        if (
            identity.client_reference_id is not None
            and identity.client_reference_id != client_reference_id
        ):
            raise RuntimeError("close-order lookup client reference mismatch")
        instrument_id = _strict_instrument_identity(payload, label="close-order lookup")
        if instrument_id != self.config.symbols[command.symbol]:
            raise RuntimeError("close-order lookup instrument mismatch")
        _strict_symbol(payload, expected=command.symbol, label="close-order lookup")
        return payload

    def _existing_costs(self, order_command_id: str) -> Decimal:
        getter = getattr(self.store, "fills_for_order", None)
        if not callable(getter):
            return Decimal("0")
        return sum(
            (fill.fee_usd + fill.financing_usd for fill in getter(order_command_id)),
            Decimal("0"),
        )

    @staticmethod
    def _terminal_status(status: str) -> bool:
        return status in {"filled", "completed", "executed", "closed"}

    @staticmethod
    def _pending_status(status: str) -> bool:
        return status in {
            "",
            "created",
            "pending",
            "accepted",
            "open",
            "partiallyfilled",
            "inprogress",
        }

    def _resolve_absent_terminal(
        self,
        command: Any,
        order: Any,
        *,
        now: datetime,
        snapshot_hash: str,
        detail: str,
    ) -> bool:
        if order.status is OrderStatus.PARTIALLY_FILLED:
            self._manual_review(
                command.order_command_id,
                now=now,
                snapshot_hash=snapshot_hash,
                detail="partially filled order ended without complete terminal economics",
            )
            return True
        if order.status in {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}:
            self.kernel.mark_unknown(
                command.order_command_id,
                at=now,
                reason=detail,
            )
        self.kernel.reconcile_unknown(
            command.order_command_id,
            at=now,
            found=False,
            broker_snapshot_hash=snapshot_hash,
            detail=detail,
        )
        return True

    def _lookup_open_fill(
        self,
        command: Any,
        order: Any,
        lookup: Mapping[str, Any],
        *,
        now: datetime,
        snapshot_hash: str,
    ) -> bool | None:
        if not lookup:
            return None
        status = _status_name(lookup)
        if status in {"rejected", "cancelled", "canceled", "expired", "failed"}:
            return self._resolve_absent_terminal(
                command,
                order,
                now=now,
                snapshot_hash=snapshot_hash,
                detail=f"broker order lookup resolved {status}",
            )
        executions = lookup.get("positionExecutions", [])
        if not isinstance(executions, list) or not all(
            isinstance(item, Mapping) for item in executions
        ):
            raise RuntimeError("broker OPEN executions are invalid")
        if not executions:
            return False if self._pending_status(status) else None
        try:
            position_ids = {decode_broker_position_identity_v2(item) for item in executions}
        except ValueError as exc:
            raise RuntimeError("broker OPEN execution position identity is invalid") from exc
        if len(position_ids) != 1:
            return None
        total_quantity = Decimal("0")
        total_notional = Decimal("0")
        event_times: list[datetime] = []
        for execution in executions:
            opening = execution.get("openingData")
            if not isinstance(opening, Mapping):
                return None
            quantity = _finite_decimal(opening, ("units", "contracts"))
            price = _finite_decimal(opening, ("avgPrice", "rate", "price"))
            event_time = _strict_evidence_timestamp(
                opening,
                ("executionTime", "openTime"),
                fallback=lookup,
                fallback_alias="lastUpdate",
                label="broker OPEN execution",
            )
            if quantity is None or price is None or event_time is None:
                return None
            total_quantity += quantity
            total_notional += quantity * price
            event_times.append(event_time)
        if total_quantity <= order.filled_quantity:
            return False
        previous_notional = order.filled_quantity * (order.average_fill_price or Decimal("0"))
        delta_quantity = total_quantity - order.filled_quantity
        delta_price = (total_notional - previous_notional) / delta_quantity
        total_cost = _finite_decimal(
            lookup,
            ("totalCosts",),
            allow_zero=True,
        )
        if total_cost is None:
            return None
        prior_cost = self._existing_costs(command.order_command_id)
        if total_cost < prior_cost:
            return None
        delta_cost = total_cost - prior_cost
        final = self._terminal_status(status)
        broker_order_id = decode_broker_order_identity_v2(lookup).order_id
        if broker_order_id is None:
            raise RuntimeError("broker OPEN fill lacks broker order identity")
        broker_position_id = next(iter(position_ids))
        seed = (
            f"broker-order-lookup:{command.order_command_id}:{broker_order_id}:"
            f"{total_quantity}:{total_notional}:{total_cost}:{status}"
        )
        self.kernel.apply_fill(
            Fill(
                fill_id="fill-" + hashlib.sha256(seed.encode()).hexdigest()[:24],
                order_command_id=command.order_command_id,
                client_order_id=command.client_order_id,
                broker_order_id=broker_order_id,
                broker_position_id=broker_position_id,
                symbol=command.symbol,
                side=command.side,
                quantity=delta_quantity,
                price=delta_price,
                fee_usd=delta_cost,
                financing_usd=Decimal("0"),
                event_time=max(event_times),
                processing_time=now,
                idempotency_key=seed,
                broker_reported_fees_usd=total_cost,
                broker_costs_source="order_lookup.totalCosts",
            ),
            final=final,
            broker_snapshot_hash=snapshot_hash,
        )
        return True

    def _lookup_close_fill(
        self,
        command: Any,
        order: Any,
        lookup: Mapping[str, Any],
        *,
        now: datetime,
        snapshot_hash: str,
    ) -> bool | None:
        if not lookup:
            return None
        status = _status_name(lookup)
        if status in {"rejected", "cancelled", "canceled", "expired", "failed"}:
            return self._resolve_absent_terminal(
                command,
                order,
                now=now,
                snapshot_hash=snapshot_hash,
                detail=f"broker close lookup resolved {status}",
            )
        broker_order_id = decode_broker_order_identity_v2(lookup).order_id
        if broker_order_id is None:
            raise RuntimeError("broker CLOSE fill lacks broker order identity")
        close = self._close_information(broker_order_id, command)
        if close is None or not close:
            return False if self._pending_status(status) else None
        positions = close.get("positions", [])
        if not isinstance(positions, list) or not all(
            isinstance(item, Mapping) for item in positions
        ):
            raise RuntimeError("broker close positions are invalid")
        try:
            decoded_positions = [
                (decode_broker_position_identity_v2(item), item) for item in positions
            ]
        except ValueError as exc:
            raise RuntimeError("broker close position identity is invalid") from exc
        decoded_ids = [identity for identity, _ in decoded_positions]
        if len(decoded_ids) != len(set(decoded_ids)):
            raise RuntimeError("broker close position identities are duplicated")
        matches = [
            item
            for identity, item in decoded_positions
            if identity == str(command.broker_position_id)
        ]
        if len(matches) != 1:
            return None
        row = matches[0]
        if any(name in row for name in ("isBuy", "side", "direction")):
            _strict_broker_side(row, label="broker CLOSE execution")
        total_quantity = _finite_decimal(row, ("units",))
        price = _finite_decimal(row, ("rate",))
        event_time = _strict_evidence_timestamp(
            row,
            ("occurred",),
            fallback=close,
            fallback_alias="requestOccurred",
            label="broker CLOSE execution",
        )
        total_cost = _finite_decimal(lookup, ("totalCosts",), allow_zero=True)
        if total_quantity is None or price is None or event_time is None or total_cost is None:
            return None
        if total_quantity <= order.filled_quantity:
            return False
        expected_quantity = command.quantity
        if expected_quantity is None or total_quantity > expected_quantity:
            return None
        delta_quantity = total_quantity - order.filled_quantity
        prior_cost = self._existing_costs(command.order_command_id)
        if total_cost < prior_cost:
            return None
        delta_cost = total_cost - prior_cost
        final = self._terminal_status(status) and total_quantity == expected_quantity
        seed = (
            f"broker-close-lookup:{command.order_command_id}:{broker_order_id}:"
            f"{total_quantity}:{price}:{total_cost}:{status}"
        )
        self.kernel.apply_fill(
            Fill(
                fill_id="fill-" + hashlib.sha256(seed.encode()).hexdigest()[:24],
                order_command_id=command.order_command_id,
                client_order_id=command.client_order_id,
                broker_order_id=broker_order_id,
                broker_position_id=str(command.broker_position_id),
                symbol=command.symbol,
                side=command.side,
                quantity=delta_quantity,
                price=price,
                fee_usd=delta_cost,
                financing_usd=Decimal("0"),
                event_time=event_time,
                processing_time=now,
                idempotency_key=seed,
                broker_reported_fees_usd=total_cost,
                broker_costs_source="close_order+order_lookup.totalCosts",
            ),
            final=final,
            exit_reason=ExitReason(command.reduce_exit_reason),
            broker_snapshot_hash=snapshot_hash,
        )
        return True

    def _matches(
        self, command: Any, order: Any, rows: tuple[Mapping[str, Any], ...]
    ) -> tuple[Mapping[str, Any], ...]:
        instrument_id = self.config.symbols[command.symbol]
        expected_position_id = command.broker_position_id or order.broker_position_id
        # A symbol-only match can bind an order to an unrelated pre-existing
        # position. Broker position identity is mandatory for automatic fills.
        if not expected_position_id:
            return ()
        return tuple(
            row
            for row in rows
            if _instrument_id(row) == instrument_id
            and (expected_position_id is None or _position_id(row) == str(expected_position_id))
        )

    @staticmethod
    def _pending_mentions(command: Any, order: Any, rows: tuple[Mapping[str, Any], ...]) -> bool:
        broker_order_id = str(order.broker_order_id or "").strip()
        client_reference_id = str(command.client_order_id or "").strip()
        position_id = str(command.broker_position_id or order.broker_position_id or "").strip()
        for row in rows:
            identity = decode_broker_order_identity_v2(row)
            if (
                broker_order_id
                and identity.order_id is not None
                and broker_order_id == identity.order_id
            ) or (
                client_reference_id
                and identity.client_reference_id is not None
                and client_reference_id == identity.client_reference_id
            ):
                return True
            row_position_ids = _strict_pending_position_references(row)
            if position_id and position_id in row_position_ids:
                return True
        return False

    def _manual_review(
        self,
        order_command_id: str,
        *,
        now: datetime,
        snapshot_hash: str,
        detail: str,
    ) -> None:
        order = self.store.broker_order(order_command_id)
        if order.status in {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}:
            self.kernel.mark_unknown(
                order_command_id,
                at=now,
                reason="broker truth remained ambiguous after grace",
            )
        self.kernel.reconcile_unknown(
            order_command_id,
            at=now,
            found=None,
            broker_snapshot_hash=snapshot_hash,
            detail=detail,
        )
        self.store.set_trading_state(
            "LOCKED",
            actor="v2-reconciliation",
            reason=detail,
            at=now,
        )

    def _reconcile_open(
        self,
        command: Any,
        order: Any,
        matches: tuple[Mapping[str, Any], ...],
        pending: tuple[Mapping[str, Any], ...],
        *,
        now: datetime,
        snapshot_hash: str,
    ) -> bool:
        try:
            lookup = self._order_lookup(command, order)
            resolved = (
                None
                if lookup is None
                else self._lookup_open_fill(
                    command,
                    order,
                    lookup,
                    now=now,
                    snapshot_hash=snapshot_hash,
                )
            )
        except (RuntimeError, ValueError) as exc:
            self._manual_review(
                command.order_command_id,
                now=now,
                snapshot_hash=snapshot_hash,
                detail=f"OPEN broker evidence failed strict validation: {type(exc).__name__}",
            )
            return True
        if lookup is not None:
            if resolved is not None:
                return resolved
            if order.last_update_at is not None and now - order.last_update_at < self.grace:
                return False
            self._manual_review(
                command.order_command_id,
                now=now,
                snapshot_hash=snapshot_hash,
                detail="OPEN lookup lacks exact terminal quantity, price, costs, or timestamp",
            )
            return True
        # An exact position may already expose a partial market fill while the
        # broker still advertises the order as pending. Wait for terminal broker
        # truth so the cumulative position is never projected as a final fill
        # and then counted again on the next snapshot.
        if self._pending_mentions(command, order, pending):
            return False
        # A position snapshot is not fill evidence: it does not provide exact
        # broker event time, fee or financing. Never manufacture zeros/current
        # time merely to make local projection look reconciled.
        if order.last_update_at is not None and now - order.last_update_at < self.grace:
            return False
        self._manual_review(
            command.order_command_id,
            now=now,
            snapshot_hash=snapshot_hash,
            detail="open command broker truth is incomplete or ambiguous",
        )
        return True

    def _reconcile_close(
        self,
        command: Any,
        order: Any,
        matches: tuple[Mapping[str, Any], ...],
        pending: tuple[Mapping[str, Any], ...],
        *,
        now: datetime,
        snapshot_hash: str,
    ) -> bool:
        try:
            lookup = self._order_lookup(command, order)
            resolved = (
                None
                if lookup is None
                else self._lookup_close_fill(
                    command,
                    order,
                    lookup,
                    now=now,
                    snapshot_hash=snapshot_hash,
                )
            )
        except (RuntimeError, ValueError) as exc:
            self._manual_review(
                command.order_command_id,
                now=now,
                snapshot_hash=snapshot_hash,
                detail=f"CLOSE broker evidence failed strict validation: {type(exc).__name__}",
            )
            return True
        if lookup is not None:
            if resolved is not None:
                return resolved
            if order.last_update_at is not None and now - order.last_update_at < self.grace:
                return False
            self._manual_review(
                command.order_command_id,
                now=now,
                snapshot_hash=snapshot_hash,
                detail="CLOSE lookup lacks exact terminal quantity, price, costs, or timestamp",
            )
            return True
        if order.last_update_at is not None and now - order.last_update_at < self.grace:
            return False
        if self._pending_mentions(command, order, pending):
            return False
        local = [
            position
            for position in self.store.positions(command.portfolio_id, open_only=True)
            if position.symbol == command.symbol
            and (
                command.broker_position_id is None
                or position.broker_position_id == command.broker_position_id
            )
        ]
        if len(matches) == 1 and len(local) == 1:
            broker_quantity = _decimal(matches[0], ("units", "quantity", "unitsOwned", "netUnits"))
            if broker_quantity == local[0].quantity:
                if order.status is OrderStatus.ACKNOWLEDGED:
                    self.kernel.mark_unknown(
                        command.order_command_id,
                        at=now,
                        reason="ACK close remained unchanged at broker after grace",
                    )
                self.kernel.reconcile_unknown(
                    command.order_command_id,
                    at=now,
                    found=False,
                    broker_snapshot_hash=snapshot_hash,
                    detail="broker position quantity is unchanged; close was absent",
                )
                return True
        self._manual_review(
            command.order_command_id,
            now=now,
            snapshot_hash=snapshot_hash,
            detail="close outcome lacks an exact broker fill price/quantity",
        )
        return True

    @staticmethod
    def _broker_side(row: Mapping[str, Any]) -> Side | None:
        try:
            return _strict_broker_side(row, label="broker position")
        except RuntimeError:
            return None

    def _history(self, earliest: datetime) -> tuple[Mapping[str, Any], ...]:
        getter = getattr(self.client, "trading_history", None)
        if not callable(getter):
            return ()
        rows: list[Mapping[str, Any]] = []
        page_hashes: list[str] = []
        seen_trade_ids: set[str] = set()
        seen_order_ids: set[str] = set()
        seen_references: set[str] = set()
        page_size = 1000
        for page in range(1, 101):
            response = getter(min_date=earliest.date(), page=page, page_size=page_size)
            if not response.ok:
                raise RuntimeError("DEMO trading history is unavailable")
            body = response.body
            if isinstance(body, Mapping):
                aliases = [name for name in ("items", "history", "trades") if name in body]
                if not aliases:
                    raise RuntimeError("DEMO trading history collection is missing")
                collections = [body[name] for name in aliases]
                if any(collection != collections[0] for collection in collections[1:]):
                    raise RuntimeError("DEMO trading history collections conflict")
                body = collections[0]
            if not isinstance(body, list) or not all(isinstance(item, Mapping) for item in body):
                raise RuntimeError("DEMO trading history shape is invalid")
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
            page_hashes.append(hashlib.sha256(canonical.encode()).hexdigest())
            for item in body:
                trade_id, order_identity, _ = _strict_history_identities(item)
                order_id = None if order_identity is None else order_identity.order_id
                reference_id = (
                    None if order_identity is None else order_identity.client_reference_id
                )
                if (
                    (trade_id is not None and trade_id in seen_trade_ids)
                    or (order_id is not None and order_id in seen_order_ids)
                    or (reference_id is not None and reference_id in seen_references)
                ):
                    raise RuntimeError("DEMO trading history identities are duplicated")
                if trade_id is not None:
                    seen_trade_ids.add(trade_id)
                if order_id is not None:
                    seen_order_ids.add(order_id)
                if reference_id is not None:
                    seen_references.add(reference_id)
                rows.append(dict(item))
            if len(body) < page_size:
                evidence = hashlib.sha256(":".join(page_hashes).encode()).hexdigest()
                self.store.state_set(
                    "v2_reconciliation_history_evidence",
                    json.dumps(
                        {"pages": page, "rows": len(rows), "sha256": evidence},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                return tuple(rows)
        raise RuntimeError("DEMO trading history exceeded the bounded pagination cap")

    @staticmethod
    def _history_exit_reason(row: Mapping[str, Any]) -> ExitReason:
        raw = str(row.get("closeReason", row.get("reason", ""))).strip().upper()
        exact = {
            "STOP_LOSS": ExitReason.STOP_LOSS,
            "TAKE_PROFIT": ExitReason.TAKE_PROFIT,
            "MANUAL": ExitReason.BROKER_RECONCILIATION,
        }
        return exact.get(raw, ExitReason.UNCLASSIFIED_BROKER)

    def _project_history_close(
        self,
        position: PositionState,
        row: Mapping[str, Any],
        *,
        now: datetime,
        snapshot_hash: str,
    ) -> bool:
        trade_id, order_identity, history_position_id = _strict_history_identities(row)
        if history_position_id != str(position.broker_position_id):
            raise RuntimeError("broker history position identity mismatch")
        quantity = _finite_decimal(row, ("units",))
        close_price = _finite_decimal(row, ("closeRate",))
        net_pnl = _finite_decimal(
            row,
            ("netProfit",),
            allow_zero=True,
            absolute=False,
        )
        reported_fees = _finite_decimal(row, ("fees",), allow_zero=True)
        event_time = _timestamp(row.get("closeTimestamp"))
        instrument_id = _strict_instrument_identity(row, label="broker history")
        side = _strict_broker_side(row, label="broker history")
        if (
            quantity is None
            or close_price is None
            or net_pnl is None
            or reported_fees is None
            or event_time is None
            or instrument_id != self.config.symbols[position.symbol]
            or side is not position.side
            or abs(quantity - position.quantity) > Decimal("0.00000001")
        ):
            return False
        gross = quantity * position.side.direction * (close_price - position.entry_price)
        residual_cost = gross - position.fees_accrued - position.financing_accrued - net_pnl
        tolerance = Decimal("0.000001")
        if residual_cost < -tolerance:
            return False
        residual_cost = max(Decimal("0"), residual_cost)
        closing_fee = min(reported_fees, residual_cost)
        financing = residual_cost - closing_fee
        broker_order_id = None if order_identity is None else order_identity.order_id
        if broker_order_id is None:
            broker_order_id = trade_id
        if broker_order_id is None:
            raise RuntimeError("broker history lacks an exact order/trade identity")
        fill_identity = (
            f"history:{position.broker_position_id}:{event_time.isoformat()}:"
            f"{quantity}:{close_price}:{net_pnl}"
        )
        reason = self._history_exit_reason(row)
        command = self.kernel.create_broker_reconciliation_close_command(
            position,
            now=now,
            reason=reason,
            broker_snapshot_hash=snapshot_hash,
            broker_order_id=broker_order_id,
            fill_identity=fill_identity,
            units=quantity,
        )
        self.kernel.apply_fill(
            Fill(
                fill_id="fill-" + hashlib.sha256(fill_identity.encode()).hexdigest()[:24],
                order_command_id=command.order_command_id,
                client_order_id=command.client_order_id,
                broker_order_id=broker_order_id,
                broker_position_id=position.broker_position_id,
                symbol=position.symbol,
                side=command.side,
                quantity=quantity,
                price=close_price,
                fee_usd=closing_fee,
                financing_usd=financing,
                event_time=event_time,
                processing_time=now,
                idempotency_key=fill_identity,
                broker_reported_net_pnl_usd=net_pnl,
                broker_reported_fees_usd=reported_fees,
                broker_costs_source="trading_history.netProfit+fees",
            ),
            final=True,
            exit_reason=reason,
            broker_snapshot_hash=snapshot_hash,
        )
        return True

    def _monitor_broker_positions(
        self,
        rows: tuple[Mapping[str, Any], ...],
        *,
        now: datetime,
        snapshot_hash: str,
    ) -> tuple[int, tuple[str, ...]]:
        if not callable(getattr(self.client, "trading_history", None)):
            return 0, ()
        local = self.store.positions(open_only=True)
        local_by_broker = {
            str(position.broker_position_id): position
            for position in local
            if position.broker_position_id is not None
        }
        broker_by_id = {decode_broker_position_identity_v2(row): row for row in rows}
        drift: list[str] = []
        projected = 0
        missing = [
            position
            for broker_id, position in local_by_broker.items()
            if broker_id not in broker_by_id
        ]
        try:
            history = (
                self._history(min(item.entry_event_time for item in missing)) if missing else ()
            )
        except (RuntimeError, ValueError) as exc:
            return 0, (f"invalid_broker_history:{type(exc).__name__}",)
        for position in missing:
            try:
                matches = [
                    item
                    for item in history
                    if _strict_history_identities(item)[2] == str(position.broker_position_id)
                ]
                projected_exact = len(matches) == 1 and self._project_history_close(
                    position,
                    matches[0],
                    now=now,
                    snapshot_hash=snapshot_hash,
                )
            except (RuntimeError, ValueError):
                projected_exact = False
                drift.append(f"invalid_broker_history:{position.position_id}")
            if projected_exact:
                projected += 1
            else:
                drift.append(f"missing_local_position_truth:{position.position_id}")

        remaining_local = self.store.positions(open_only=True)
        remaining_ids = {
            str(position.broker_position_id)
            for position in remaining_local
            if position.broker_position_id is not None
        }
        for broker_id in sorted(set(broker_by_id) - remaining_ids):
            drift.append(f"unbound_broker_position:{broker_id}")
        for position in remaining_local:
            broker_id = str(position.broker_position_id or "")
            row = broker_by_id.get(broker_id)
            if row is None:
                continue
            quantity = _finite_decimal(row, ("units", "quantity", "unitsOwned", "netUnits"))
            entry = _finite_decimal(row, ("openRate", "averageOpenRate", "entryPrice", "price"))
            side = self._broker_side(row)
            if _instrument_id(row) != self.config.symbols[position.symbol]:
                drift.append(f"instrument_mismatch:{position.position_id}")
            if side is None or side is not position.side:
                drift.append(f"side_mismatch:{position.position_id}")
            if quantity is None or abs(quantity - position.quantity) > Decimal("0.00000001"):
                drift.append(f"quantity_mismatch:{position.position_id}")
            if entry is None or abs(entry - position.entry_price) > max(
                Decimal("0.00000001"), position.entry_price * Decimal("0.0005")
            ):
                drift.append(f"entry_mismatch:{position.position_id}")
        return projected, tuple(sorted(set(drift)))

    def run_once(self) -> int:
        now = datetime.now(UTC)
        try:
            rows, pending, snapshot_hash = self._portfolio()
        except Exception as exc:
            self.store.set_trading_state(
                "LOCKED",
                actor="v2-reconciliation",
                reason=f"invalid broker portfolio snapshot: {type(exc).__name__}",
                at=now,
            )
            self.store.heartbeat(
                "v2-reconciliation",
                "error",
                {
                    "phase": "portfolio_validation",
                    "error_type": type(exc).__name__,
                    "broker_snapshot_valid": False,
                    "fills_or_positions_mutated": False,
                    "trading_state": "LOCKED",
                    "real_money": False,
                },
                at=now,
            )
            raise RuntimeError("DEMO portfolio snapshot failed strict validation") from exc
        reconciled = 0
        orders = self.store.broker_orders_by_status(
            (
                OrderStatus.ACKNOWLEDGED.value,
                OrderStatus.PARTIALLY_FILLED.value,
                OrderStatus.UNKNOWN.value,
            )
        )
        for order in orders:
            command = self.store.order_command(order.order_command_id)
            matches = self._matches(command, order, rows)
            changed = (
                self._reconcile_close(
                    command,
                    order,
                    matches,
                    pending,
                    now=now,
                    snapshot_hash=snapshot_hash,
                )
                if command.reduce_only
                else self._reconcile_open(
                    command,
                    order,
                    matches,
                    pending,
                    now=now,
                    snapshot_hash=snapshot_hash,
                )
            )
            reconciled += int(changed)
        external_closes, drift = self._monitor_broker_positions(
            rows,
            now=now,
            snapshot_hash=snapshot_hash,
        )
        reconciled += external_closes
        if drift:
            self.store.set_trading_state(
                "LOCKED",
                actor="v2-reconciliation",
                reason="economic broker/local drift: " + ",".join(drift)[:400],
                at=now,
            )
        trading_state = self.store.state_get("trading_state", "LOCKED")
        self.store.heartbeat(
            "v2-reconciliation",
            "healthy" if trading_state == "ACTIVE" else "halted",
            {
                "orders_examined": len(orders),
                "orders_reconciled": reconciled,
                "broker_position_count": len(rows),
                "broker_pending_order_count": len(pending),
                "external_closes_projected": external_closes,
                "economic_drift": list(drift),
                "broker_snapshot_hash": snapshot_hash,
                "trading_state": trading_state,
                "real_money": False,
            },
            at=now,
        )
        return reconciled

    def run_forever(self, interval_seconds: int = 10) -> None:
        if interval_seconds < 1:
            raise ValueError("reconciliation interval must be positive")
        ready()
        while True:
            try:
                self.run_once()
                watchdog()
            except Exception as exc:
                self.store.heartbeat(
                    "v2-reconciliation",
                    "error",
                    {"error_type": type(exc).__name__, "real_money": False},
                )
                print(
                    f"V2_RECONCILIATION_ERROR={type(exc).__name__}",
                    flush=True,
                )
            time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only eToro DEMO broker reconciliation for runtime v2"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = load_config_v2(args.config)
    client = EtoroPublicApiDemoClientV2()
    client.verify_isolated_demo_read_scope()
    store = PostgresRuntimeStoreV2.from_dsn(_dsn(config))
    store.require_schema()
    kernel = UnifiedTradingKernel(store, GlobalRiskKernel(config.mandate))
    worker = DemoReconciliationWorkerV2(config, store, kernel, client)
    try:
        if args.once:
            print(f"V2_RECONCILED={worker.run_once()}")
        else:
            worker.run_forever(args.interval)
    finally:
        store.close()


if __name__ == "__main__":
    main()

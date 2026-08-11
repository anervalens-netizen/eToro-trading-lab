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
from .domain_v2 import Fill, OrderStatus
from .etoro_api_current_v2 import EtoroPublicApiDemoClientV2
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


def _position_id(row: Mapping[str, Any]) -> str:
    return str(row.get("positionID", row.get("positionId", ""))).strip()


def _instrument_id(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("instrumentID", row.get("instrumentId", 0)) or 0)
    except (TypeError, ValueError):
        return 0


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
        pending: list[Mapping[str, Any]] = []
        for name in ("orders", "ordersForOpen"):
            collection = root.get(name, [])
            if not isinstance(collection, list) or not all(
                isinstance(row, Mapping) for row in collection
            ):
                raise RuntimeError(f"DEMO {name} collection is invalid")
            pending.extend(collection)
        canonical = json.dumps(root, sort_keys=True, separators=(",", ":"), default=str)
        return (
            tuple(rows),
            tuple(pending),
            hashlib.sha256(canonical.encode()).hexdigest(),
        )

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
        order_ids = {
            str(value).strip()
            for value in (order.broker_order_id, command.client_order_id)
            if str(value or "").strip()
        }
        position_id = str(command.broker_position_id or order.broker_position_id or "").strip()
        for row in rows:
            row_order_ids = {
                str(row.get(name, "")).strip()
                for name in (
                    "orderID",
                    "orderId",
                    "id",
                    "clientOrderID",
                    "clientOrderId",
                    "requestID",
                    "requestId",
                    "xRequestID",
                    "xRequestId",
                )
                if str(row.get(name, "")).strip()
            }
            if order_ids & row_order_ids:
                return True
            row_position_ids = {_position_id(row)}
            for name in ("positionIDs", "positionIds"):
                values = row.get(name, [])
                if isinstance(values, list):
                    row_position_ids.update(str(value).strip() for value in values)
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
        if order.status is OrderStatus.ACKNOWLEDGED:
            self.kernel.mark_unknown(
                order_command_id,
                at=now,
                reason="ACK broker truth remained ambiguous after grace",
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
        # An exact position may already expose a partial market fill while the
        # broker still advertises the order as pending. Wait for terminal broker
        # truth so the cumulative position is never projected as a final fill
        # and then counted again on the next snapshot.
        if self._pending_mentions(command, order, pending):
            return False
        if len(matches) == 1:
            row = matches[0]
            quantity = _decimal(row, ("units", "quantity", "unitsOwned", "netUnits"))
            price = _decimal(row, ("openRate", "averageOpenRate", "entryPrice", "price"))
            position_id = _position_id(row)
            if quantity is not None and price is not None and position_id:
                seed = (
                    f"broker-reconcile:{command.order_command_id}:{position_id}:{quantity}:{price}"
                )
                self.kernel.apply_fill(
                    Fill(
                        fill_id="fill-" + hashlib.sha256(seed.encode()).hexdigest()[:24],
                        order_command_id=command.order_command_id,
                        client_order_id=command.client_order_id,
                        broker_order_id=order.broker_order_id,
                        broker_position_id=position_id,
                        symbol=command.symbol,
                        side=command.side,
                        quantity=quantity,
                        price=price,
                        fee_usd=Decimal("0"),
                        financing_usd=Decimal("0"),
                        event_time=now,
                        processing_time=now,
                        idempotency_key=seed,
                    ),
                    final=True,
                    broker_snapshot_hash=snapshot_hash,
                )
                return True
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

    def run_once(self) -> int:
        now = datetime.now(UTC)
        rows, pending, snapshot_hash = self._portfolio()
        reconciled = 0
        orders = self.store.broker_orders_by_status(
            (OrderStatus.ACKNOWLEDGED.value, OrderStatus.UNKNOWN.value)
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
        trading_state = self.store.state_get("trading_state", "LOCKED")
        self.store.heartbeat(
            "v2-reconciliation",
            "healthy" if trading_state == "ACTIVE" else "halted",
            {
                "orders_examined": len(orders),
                "orders_reconciled": reconciled,
                "broker_position_count": len(rows),
                "broker_pending_order_count": len(pending),
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

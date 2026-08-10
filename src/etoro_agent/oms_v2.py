from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from .domain_v2 import BrokerOrder, Fill, OrderCommand, OrderStatus, TERMINAL_ORDER_STATES, ZERO, utc


_ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.RISK_APPROVED, OrderStatus.REJECTED, OrderStatus.EXPIRED}),
    OrderStatus.RISK_APPROVED: frozenset({OrderStatus.SUBMITTING, OrderStatus.REJECTED, OrderStatus.EXPIRED}),
    OrderStatus.SUBMITTING: frozenset({OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.UNKNOWN}),
    OrderStatus.ACKNOWLEDGED: frozenset({OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.UNKNOWN}),
    OrderStatus.PARTIALLY_FILLED: frozenset({OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.UNKNOWN}),
    OrderStatus.UNKNOWN: frozenset({OrderStatus.RECONCILED_FILLED, OrderStatus.RECONCILED_ABSENT, OrderStatus.MANUAL_REVIEW}),
    OrderStatus.RECONCILED_ABSENT: frozenset(),
    OrderStatus.RECONCILED_FILLED: frozenset(),
    OrderStatus.MANUAL_REVIEW: frozenset(),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}


class OrderStateError(ValueError):
    pass


class OrderManagementSystem:
    """Pure deterministic OMS. Persistence/transport are adapters around this state machine."""

    version = "oms-v2.0"

    @staticmethod
    def create(command: OrderCommand) -> BrokerOrder:
        return BrokerOrder(
            order_command_id=command.order_command_id,
            client_order_id=command.client_order_id,
            status=OrderStatus.CREATED,
            last_update_at=command.created_at,
        )

    @staticmethod
    def transition(order: BrokerOrder, target: OrderStatus, at: datetime, **changes: object) -> BrokerOrder:
        if target not in _ALLOWED[order.status]:
            raise OrderStateError(f"invalid order transition {order.status.value}->{target.value}")
        timestamp = utc(at)
        values: dict[str, object] = {"status": target, "last_update_at": timestamp, **changes}
        if target is OrderStatus.SUBMITTING and order.submitted_at is None:
            values["submitted_at"] = timestamp
        if target is OrderStatus.ACKNOWLEDGED and order.acknowledged_at is None:
            values["acknowledged_at"] = timestamp
        return replace(order, **values)

    def risk_approve(self, order: BrokerOrder, at: datetime) -> BrokerOrder:
        return self.transition(order, OrderStatus.RISK_APPROVED, at)

    def begin_submit(self, order: BrokerOrder, at: datetime) -> BrokerOrder:
        return self.transition(order, OrderStatus.SUBMITTING, at)

    def acknowledge(
        self,
        order: BrokerOrder,
        at: datetime,
        *,
        broker_order_id: str,
        broker_position_id: str | None = None,
    ) -> BrokerOrder:
        if not broker_order_id.strip():
            raise ValueError("broker_order_id is required for ACK")
        return self.transition(
            order,
            OrderStatus.ACKNOWLEDGED,
            at,
            broker_order_id=broker_order_id,
            broker_position_id=broker_position_id,
            acknowledged_at=utc(at),
        )

    def mark_unknown(self, order: BrokerOrder, at: datetime, reason: str) -> BrokerOrder:
        if order.status not in {OrderStatus.SUBMITTING, OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}:
            raise OrderStateError("UNKNOWN is valid only after a possible network send")
        return self.transition(order, OrderStatus.UNKNOWN, at, failure_reason=reason[:500])

    def reject(self, order: BrokerOrder, at: datetime, reason: str) -> BrokerOrder:
        return self.transition(order, OrderStatus.REJECTED, at, failure_reason=reason[:500])

    def apply_fill(
        self,
        order: BrokerOrder,
        fill: Fill,
        *,
        expected_quantity: Decimal | None,
        is_final: bool = False,
    ) -> BrokerOrder:
        if order.status in TERMINAL_ORDER_STATES:
            raise OrderStateError("cannot apply fill to terminal order")
        if fill.order_command_id != order.order_command_id or fill.client_order_id != order.client_order_id:
            raise OrderStateError("fill/order identity mismatch")
        old_qty = order.filled_quantity
        new_qty = old_qty + fill.quantity
        if expected_quantity is not None and new_qty > expected_quantity:
            raise OrderStateError("fill exceeds expected order quantity")
        old_notional = old_qty * (order.average_fill_price or ZERO)
        average = (old_notional + fill.quantity * fill.price) / new_qty
        target = (
            OrderStatus.FILLED
            if is_final or (expected_quantity is not None and new_qty == expected_quantity)
            else OrderStatus.PARTIALLY_FILLED
        )
        if order.status is OrderStatus.SUBMITTING and target is OrderStatus.PARTIALLY_FILLED:
            # A fill itself proves broker acceptance even if no separate ACK event arrived.
            pass
        return self.transition(
            order,
            target,
            fill.processing_time,
            broker_order_id=fill.broker_order_id or order.broker_order_id,
            broker_position_id=fill.broker_position_id or order.broker_position_id,
            filled_quantity=new_qty,
            average_fill_price=average,
        )

    def reconcile_unknown(
        self,
        order: BrokerOrder,
        at: datetime,
        *,
        found: bool | None,
        broker_order_id: str | None = None,
        broker_position_id: str | None = None,
    ) -> BrokerOrder:
        if order.status is not OrderStatus.UNKNOWN:
            raise OrderStateError("only UNKNOWN orders can be reconciled through this path")
        if found is True:
            return self.transition(
                order,
                OrderStatus.RECONCILED_FILLED,
                at,
                broker_order_id=broker_order_id or order.broker_order_id,
                broker_position_id=broker_position_id or order.broker_position_id,
            )
        if found is False:
            return self.transition(order, OrderStatus.RECONCILED_ABSENT, at)
        return self.transition(order, OrderStatus.MANUAL_REVIEW, at)
